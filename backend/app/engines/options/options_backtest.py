"""Options backtest engine.

Replays a directional strategy on an underlying through history, picks an
option contract for each signal, and tracks the position day-by-day using
Polygon's historical options aggs (which the user's plan does include).

Output is written to the same `backtest_runs` / `backtest_trades` /
`backtest_metrics` tables as the futures engine so the existing Backtests
UI renders results without changes.

Per-trade life cycle in the backtest:
  1. signal day D: ICTStrategy fires LONG/SHORT on the underlying's bars
  2. strike picker chooses the contract using:
       • current spot = day-D close
       • IV = solved from the contract's actual day-D historical price
         (round-trip via Black-Scholes — `pricing.implied_vol`)
  3. entry premium = day-D close of the chosen contract (from Polygon aggs)
  4. for each subsequent day until expiration:
       • mark-to-market at that day's close
       • if mark <= stop_premium → exit "stop"
       • if mark >= target_premium → exit "target"
  5. if still open at expiration → exit "expiration" at intrinsic value

Polygon free-tier rate-limit: we batch by trade. Each trade needs:
  • 1 chain query (cached per day×side×DTE-band → ~1 query per signal day)
  • 1 historical-aggs pull for the picked contract (entire holding period)

For 250 trading days × ~2 signals/day = ~500 calls. Polygon caps at 5/min
on free, so this engine self-throttles to 5 RPM and warns the user it'll
take a while. Upgrading to a paid tier removes the throttle.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta, timezone
from typing import Optional
from loguru import logger

import pandas as pd

from app.engines.options.polygon_options import PolygonOptionsClient, OptionContract, OptionBar
from app.engines.options.strike_picker import pick_strike
from app.engines.options.pricing import price as bs_price, implied_vol, greeks
from app.engines.backtest_engine.ict_strategy import ICTStrategy
from app.engines.strategy_engine.base_strategy import StrategyConfig, SignalType


# ── Per-process Polygon throttle. Free tier = 5/min, so we cap at 4 to
#    leave headroom for the contracts endpoint (which we also hit). All
#    backtest API calls go through this gate.
class _RateGate:
    def __init__(self, max_per_minute: int = 4):
        self._tokens = max_per_minute
        self._refill_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = datetime.now(timezone.utc)
            if now >= self._refill_at:
                self._tokens = 4
                self._refill_at = now + timedelta(minutes=1)
            if self._tokens > 0:
                self._tokens -= 1
                return
            # No tokens — sleep until refill
            wait_s = (self._refill_at - now).total_seconds() + 1
        await asyncio.sleep(max(1, wait_s))
        await self.acquire()


_rate_gate = _RateGate(max_per_minute=4)


@dataclass
class OptionBacktestTrade:
    underlying: str
    direction: str
    contract_ticker: str
    strike: float
    expiration: date
    right: str
    contracts: int
    entry_premium: float
    exit_premium: float
    entry_spot: float
    exit_spot: float
    entry_time: datetime
    exit_time: datetime
    stop_premium: float
    target_premium: float
    iv_used: float
    gross_pnl: float
    commission: float
    net_pnl: float
    is_winner: bool
    exit_reason: str
    metadata: dict = field(default_factory=dict)


@dataclass
class OptionBacktestConfig:
    underlying: str
    start_date: date
    end_date: date
    starting_balance: float = 10_000.0
    risk_per_trade_pct: float = 1.5
    commission_per_contract: float = 0.65
    stop_loss_premium_pct: float = 50.0
    target_premium_pct: float = 100.0
    daily_loss_pct_kill: float = 5.0
    # Strike picker params (default from strategy config)
    delta_min: float = 0.30
    delta_max: float = 0.50
    dte_min: int = 30
    dte_max: int = 60
    prefer_itm: bool = False
    spread_width: Optional[int] = None
    # Polygon
    polygon_throttle: bool = True
    # Risk-free + dividend yield used by BS solver
    risk_free_rate: float = 0.045
    dividend_yield: float = 0.0
    # Earnings filter
    avoid_earnings_days: int = 0
    mode: str = ""


class OptionsBacktestEngine:
    """Single-instrument options backtest. One instance = one underlying ×
    one strategy × one date range."""

    def __init__(self, cfg: OptionBacktestConfig, strategy: ICTStrategy,
                 progress_cb=None):
        self.cfg = cfg
        self.strategy = strategy
        self.progress_cb = progress_cb     # called with float in [0, 1]

        self.client = PolygonOptionsClient()
        self.trades: list[OptionBacktestTrade] = []

        # Risk state
        self._equity = float(cfg.starting_balance)
        self._daily_pnl_by_date: dict[date, float] = {}
        self._kill_switch = False

        # Cache: chain by (date, side, dte_min, dte_max)
        self._chain_cache: dict[tuple[str, str], list[OptionContract]] = {}
        # Cache: option aggs by (ticker, start, end)
        self._aggs_cache: dict[str, list[OptionBar]] = {}

    async def _gated_call(self, coro):
        if self.cfg.polygon_throttle:
            await _rate_gate.acquire()
        return await coro

    async def _get_chain(self, signal_date: date, side: str) -> list[OptionContract]:
        key = (signal_date.isoformat(), side)
        if key in self._chain_cache:
            return self._chain_cache[key]
        contracts = await self._gated_call(self.client.list_contracts(
            underlying=self.cfg.underlying, right=side,
            expiration_after=signal_date + timedelta(days=self.cfg.dte_min),
            expiration_before=signal_date + timedelta(days=self.cfg.dte_max),
            limit=250,
        ))
        self._chain_cache[key] = contracts
        return contracts

    async def _get_aggs(self, ticker: str, start: date, end: date) -> list[OptionBar]:
        cache_key = f"{ticker}|{start.isoformat()}|{end.isoformat()}"
        if cache_key in self._aggs_cache:
            return self._aggs_cache[cache_key]
        bars = await self._gated_call(self.client.get_aggs(
            option_ticker=ticker, start=start, end=end, timespan="day", multiplier=1,
        ))
        self._aggs_cache[cache_key] = bars
        return bars

    def _size_position(self, entry_premium: float) -> int:
        if entry_premium <= 0:
            return 0
        loss_per = entry_premium * 100 * (self.cfg.stop_loss_premium_pct / 100.0) \
                    + (self.cfg.commission_per_contract * 2)
        risk_dollars = self._equity * (self.cfg.risk_per_trade_pct / 100.0)
        if loss_per <= 0 or risk_dollars <= 0:
            return 0
        return max(0, int(risk_dollars // loss_per))

    async def _execute_trade(self, signal_date: date, signal_time: datetime,
                              side: str, spot_at_signal: float,
                              underlying_history: pd.DataFrame) -> Optional[OptionBacktestTrade]:
        option_side = "call" if side in ("long", "bullish") else "put"

        # Pull chain for this signal day
        chain = await self._get_chain(signal_date, option_side)
        if not chain:
            return None

        # First pick at default IV — we'll then upgrade to solved IV
        pick = pick_strike(
            chain, spot=spot_at_signal, today=signal_date, side=option_side,
            delta_min=self.cfg.delta_min, delta_max=self.cfg.delta_max,
            dte_min=self.cfg.dte_min, dte_max=self.cfg.dte_max,
            prefer_itm=self.cfg.prefer_itm, spread_width=self.cfg.spread_width,
            default_iv=0.30,
        )
        if pick is None or pick.band_missed:
            return None
        contract = pick.long

        # Pull historical aggs for this contract, from signal day through
        # expiration (or a max 90-day window)
        end_window = min(contract.expiration, signal_date + timedelta(days=90))
        bars = await self._get_aggs(contract.ticker, signal_date, end_window)
        if not bars:
            return None

        # Find the entry bar — first one on or after signal_date
        entry_bar = next((b for b in bars if b.timestamp.date() >= signal_date), None)
        if entry_bar is None or entry_bar.close <= 0.05:
            return None
        entry_premium = entry_bar.close

        # Solve actual IV from the entry price (replaces the default-IV estimate
        # used by the picker — for sizing/exit thresholds this is the real one)
        dte_at_entry = (contract.expiration - entry_bar.timestamp.date()).days
        t = max(1, dte_at_entry) / 365.0
        iv = implied_vol(entry_premium, s=spot_at_signal, k=contract.strike,
                          t=t, r=self.cfg.risk_free_rate, q=self.cfg.dividend_yield,
                          opt_type=option_side)

        contracts = self._size_position(entry_premium)
        if contracts < 1:
            return None

        stop_premium   = entry_premium * (1 - self.cfg.stop_loss_premium_pct / 100.0)
        target_premium = entry_premium * (1 + self.cfg.target_premium_pct    / 100.0)

        # Walk forward bar-by-bar. Note: we use daily bars so exits are end-of-day.
        # That's a deliberate simplification — tighter granularity needs intraday
        # options data which is paid-only on Polygon. For swing-options backtest
        # this is accurate enough.
        exit_bar: Optional[OptionBar] = None
        exit_reason = "expiration"
        for b in bars:
            if b.timestamp.date() <= entry_bar.timestamp.date():
                continue
            # Stop / target on the day's high/low (more realistic than close-only)
            if b.low <= stop_premium:
                exit_bar = OptionBar(timestamp=b.timestamp, open=b.open, high=b.high,
                                      low=b.low, close=stop_premium, volume=b.volume, vwap=b.vwap)
                exit_reason = "stop"
                break
            if b.high >= target_premium:
                exit_bar = OptionBar(timestamp=b.timestamp, open=b.open, high=b.high,
                                      low=b.low, close=target_premium, volume=b.volume, vwap=b.vwap)
                exit_reason = "target"
                break

        if exit_bar is None:
            # Hold to expiration — exit at intrinsic. Approximate the
            # expiration-day underlying close from the underlying_history df.
            exp_row = underlying_history[underlying_history.index.date <= contract.expiration].tail(1)
            exp_spot = float(exp_row["close"].iloc[0]) if not exp_row.empty else spot_at_signal
            intrinsic = max(0.0, (exp_spot - contract.strike)
                              if option_side == "call" else (contract.strike - exp_spot))
            exit_bar = OptionBar(
                timestamp=datetime.combine(contract.expiration, datetime.min.time(),
                                            tzinfo=timezone.utc),
                open=intrinsic, high=intrinsic, low=intrinsic, close=intrinsic,
                volume=0, vwap=intrinsic,
            )

        exit_premium = exit_bar.close
        gross = (exit_premium - entry_premium) * contracts * 100
        commission = self.cfg.commission_per_contract * contracts * 2
        net = gross - commission

        trade = OptionBacktestTrade(
            underlying=self.cfg.underlying, direction=option_side,
            contract_ticker=contract.ticker, strike=contract.strike,
            expiration=contract.expiration, right=contract.right,
            contracts=contracts, entry_premium=entry_premium, exit_premium=exit_premium,
            entry_spot=spot_at_signal,
            exit_spot=float(underlying_history.loc[underlying_history.index <= exit_bar.timestamp, "close"].iloc[-1])
                       if len(underlying_history) else spot_at_signal,
            entry_time=entry_bar.timestamp, exit_time=exit_bar.timestamp,
            stop_premium=stop_premium, target_premium=target_premium,
            iv_used=iv,
            gross_pnl=gross, commission=commission, net_pnl=net,
            is_winner=(net > 0), exit_reason=exit_reason,
            metadata={
                "delta_at_entry": pick.actual_delta,
                "dte_at_entry":   dte_at_entry,
                "pick_reason":    pick.reason,
            },
        )

        # Roll equity + daily-loss kill switch
        self._equity += net
        d = entry_bar.timestamp.date()
        self._daily_pnl_by_date[d] = self._daily_pnl_by_date.get(d, 0.0) + net
        loss_cap = self.cfg.starting_balance * (self.cfg.daily_loss_pct_kill / 100.0)
        if loss_cap > 0 and self._daily_pnl_by_date[d] <= -loss_cap:
            self._kill_switch = True
            logger.warning(f"[OptionsBacktest] daily-loss kill switch on {d}")

        return trade

    async def run(self) -> list[OptionBacktestTrade]:
        """Pull underlying bars once for the whole window, replay through the
        strategy, execute every signal."""
        # Pull underlying 1m bars (for ICTStrategy input). For multi-month
        # ranges we resample down to daily — ICT signals are still meaningful
        # on daily, and 1m aggs over 6+ months would be massive.
        days = (self.cfg.end_date - self.cfg.start_date).days
        interval = "1D" if days > 30 else "5m"

        import httpx
        from app.engines.data_feeds.polygon_feed import POLYGON_API_KEY
        timespan_map = {"5m": ("minute", 5), "15m": ("minute", 15),
                        "1H": ("hour", 1), "1D": ("day", 1)}
        ts, mult = timespan_map.get(interval, ("day", 1))
        url = (f"https://api.polygon.io/v2/aggs/ticker/{self.cfg.underlying}"
                f"/range/{mult}/{ts}/{self.cfg.start_date.isoformat()}/{self.cfg.end_date.isoformat()}"
                f"?adjusted=true&sort=asc&limit=50000&apiKey={POLYGON_API_KEY}")
        await _rate_gate.acquire()
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.get(url)
            r.raise_for_status()
            results = (r.json() or {}).get("results", [])
        if not results:
            logger.error(f"[OptionsBacktest] no underlying bars for {self.cfg.underlying}")
            return []
        df = pd.DataFrame(results)
        df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True)
        df = df.rename(columns={"o": "open", "h": "high", "l": "low",
                                  "c": "close", "v": "volume"}).set_index("timestamp")

        # Replay
        primary_tf = self.strategy.config.primary_timeframe
        exec_tf    = self.strategy.config.execution_timeframe
        warmup = 30
        for i in range(warmup, len(df)):
            if self._kill_switch:
                break
            window = df.iloc[: i + 1]
            bars_dict = {primary_tf: window, exec_tf: window}
            if "1H" in (self.strategy.config.higher_timeframes or []):
                bars_dict["1H"] = window.resample("1h").agg({
                    "open": "first", "high": "max", "low": "min",
                    "close": "last", "volume": "sum",
                }).dropna()
            signal = self.strategy.on_bar(bars_dict)
            if signal and signal.signal != SignalType.NONE:
                signal_dt = window.index[-1].to_pydatetime()
                # Earnings filter — backtest path. We pull the calendar at
                # backtest run time (current upcoming earnings); for true
                # point-in-time correctness we'd need historical earnings,
                # but for swing-options backtests this is the standard
                # approximation everyone uses.
                if self._avoid_earnings_days > 0 and self._mode != "earnings_catalyst":
                    from app.engines.options.earnings_filter import is_near_earnings
                    _near, _ed = await is_near_earnings(self.cfg.underlying, signal_dt.date(), self._avoid_earnings_days)
                    if _near:
                        continue
                spot = float(window["close"].iloc[-1])
                side = "long" if signal.signal == SignalType.LONG else "short"
                trade = await self._execute_trade(
                    signal_date=signal_dt.date(), signal_time=signal_dt,
                    side=side, spot_at_signal=spot,
                    underlying_history=window,
                )
                if trade:
                    self.trades.append(trade)
            if self.progress_cb and i % 20 == 0:
                self.progress_cb((i - warmup) / max(1, len(df) - warmup))

        if self.progress_cb:
            self.progress_cb(1.0)
        return self.trades


# ── Metrics computation — mirrors the futures engine's metrics shape so the
#    same UI renders the results
def compute_options_metrics(trades: list[OptionBacktestTrade], initial_capital: float) -> dict:
    if not trades:
        return {
            "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
            "breakeven_trades": 0, "win_rate": 0.0, "effective_win_rate": 0.0,
            "net_profit": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
            "profit_factor": 0.0, "max_drawdown": 0.0, "max_drawdown_pct": 0.0,
            "sharpe_ratio": None, "sortino_ratio": None,
            "avg_win": 0.0, "avg_loss": 0.0, "avg_rr": 0.0,
            "largest_win": 0.0, "largest_loss": 0.0,
            "avg_trade_duration_minutes": 0.0,
            "equity_curve": [{"t": 0, "equity": initial_capital}],
            "monthly_returns": {},
        }

    net_pnls = [t.net_pnl for t in trades]
    wins   = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p < 0]
    breakevens = [p for p in net_pnls if p == 0]
    gross_profit = sum(wins)
    gross_loss   = abs(sum(losses))
    net_profit   = sum(net_pnls)
    # Unify with the futures/optimizer definition via the canonical helper:
    # win_rate counts break-even scratches as non-losses; effective excludes them.
    from app.engines.backtest_engine.metrics import win_rate_stats
    _wr = win_rate_stats(len(wins) + len(breakevens), len(losses), len(breakevens))
    win_rate     = _wr["win_rate"]
    effective_wr = _wr["effective_win_rate"]

    # Equity curve
    eq = initial_capital
    equity_curve = [{"t": int(trades[0].entry_time.timestamp()), "equity": eq}]
    peak = eq
    max_dd = 0.0
    for t in trades:
        eq += t.net_pnl
        equity_curve.append({"t": int(t.exit_time.timestamp()), "equity": round(eq, 2)})
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)

    # Monthly returns
    monthly: dict[str, float] = {}
    for t in trades:
        key = t.exit_time.strftime("%Y-%m")
        monthly[key] = monthly.get(key, 0.0) + t.net_pnl

    # Durations
    durations = [(t.exit_time - t.entry_time).total_seconds() / 60.0 for t in trades]

    return {
        "total_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "breakeven_trades": len(breakevens),
        "win_rate": round(win_rate, 4),
        "effective_win_rate": round(effective_wr, 4),
        "net_profit": round(net_profit, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0.0,
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd / initial_capital * 100, 2) if initial_capital > 0 else 0.0,
        "sharpe_ratio": None, "sortino_ratio": None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "avg_rr": 0.0,
        "largest_win": round(max(wins), 2) if wins else 0.0,
        "largest_loss": round(min(losses), 2) if losses else 0.0,
        "avg_trade_duration_minutes": round(sum(durations) / len(durations), 1) if durations else 0.0,
        "equity_curve": equity_curve,
        "monthly_returns": monthly,
    }
