"""
Backtest Runner — the core simulation engine.
Iterates bar-by-bar over historical data, feeds the strategy,
simulates order fills with slippage and commission, and records trades.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import pandas as pd
from loguru import logger

from app.engines.strategy_engine.base_strategy import BaseStrategy, TradeSignal, SignalType, ExitReason
from app.engines.backtest_engine.data_handler import DataHandler
from app.engines.backtest_engine.metrics import BacktestMetricsResult, calculate_metrics


TICK_VALUES = {
    # Mini contracts
    "ES":  12.50,   # $12.50/tick ($50/point, 0.25 tick)
    "NQ":  5.00,    # $5.00/tick ($20/point, 0.25 tick)
    "RTY": 5.00,
    "YM":  5.00,
    # Micro contracts — 1/10 the tick value of the mini
    "MES": 1.25,
    "MNQ": 0.50,
    "M2K": 0.50,
    "MYM": 0.50,
}

TICK_SIZES = {
    "ES":  0.25,  "NQ":  0.25,  "RTY": 0.10,  "YM":  1.0,
    "MES": 0.25,  "MNQ": 0.25,  "M2K": 0.10,  "MYM": 1.0,
}

# When the account is too small to risk even 1 mini contract within the
# user's risk budget, the runner auto-substitutes the micro variant.
# Same setup, 1/10 the notional, lets a $1k account participate.
MINI_TO_MICRO = {
    "ES":  "MES",
    "NQ":  "MNQ",
    "RTY": "M2K",
    "YM":  "MYM",
}


@dataclass
class SimulatedTrade:
    instrument: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    contracts: int
    entry_time: datetime
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    pnl: float = 0.0
    pnl_ticks: float = 0.0
    commission: float = 0.0
    slippage: float = 0.0
    net_pnl: float = 0.0
    is_winner: bool = False
    exit_reason: str = ""
    conditions_snapshot: dict = field(default_factory=dict)


@dataclass
class BacktestConfig:
    instrument: str
    start_date: datetime
    end_date: datetime
    primary_timeframe: str
    all_timeframes: list[str]
    initial_capital: float = 100_000.0
    commission_per_side: float = 2.25   # per contract
    slippage_ticks: int = 1
    # Fraction of *current* account equity risked on each trade. Without this,
    # contract count is fixed and every account size produces identical P&L.
    risk_per_trade_pct: float = 1.0
    # Position sizing base. When False (default) every trade is sized off the
    # INITIAL capital, so contract count scales linearly with account size and
    # all ratio metrics (win rate, drawdown %, avg R, PF, monthly shape) are
    # identical across account sizes — only dollar P&L scales. When True, sizing
    # compounds off current equity (opt-in; intentionally makes results
    # account-size-path-dependent).
    compounding: bool = False
    # Hard cap on contracts regardless of equity — protects against
    # huge size on tight stops.
    max_contracts_cap: int = 100
    # Move stop to entry once price reaches this fraction of risk in our favor.
    # 1.0 = move to BE at 1R. 0 = disable (default).
    # NOTE: enabling this can dramatically reduce effective WR — trades that
    # would have hit TP get stopped at BE on retracements. The user's earlier
    # backtests (91% WR / 85% effective) were run with this OFF; the 0.5
    # default added in ecba592 caused regression to 86% / 68%. Default
    # restored to OFF; opt-in by setting on the run config.
    breakeven_at_r: float = 0.0
    # Apex-style trailing drawdown ($ from the equity peak). When the
    # current drawdown from peak crosses `half_size_drawdown_pct` of this
    # threshold, the runner halves the next trade's contract size. Set
    # to 0 to disable (default — keep behaviour unchanged for non-prop accounts).
    trailing_drawdown: float = 0.0
    # Halve size once we've consumed this fraction of the drawdown buffer.
    # 0.5 = halve at 50% consumed, which matches Apex's "soft warning zone".
    half_size_drawdown_pct: float = 0.5
    # Daily loss limit ($) — once today's realized P&L is at or below
    # -daily_loss_limit, the runner stops opening new trades until the day
    # rolls over. This mirrors Apex Eval's $1,000 / day limit on a 50K.
    # Set 0 to disable.
    daily_loss_limit: float = 0.0


class BacktestRunner:

    def __init__(self, strategy: BaseStrategy, data_handler: DataHandler, config: BacktestConfig, progress_callback=None):
        self.strategy = strategy
        self.data_handler = data_handler
        self.config = config
        self._open_trade: Optional[SimulatedTrade] = None
        self._completed_trades: list[SimulatedTrade] = []
        self._current_date: Optional[datetime] = None
        self._progress_callback = progress_callback
        # Running equity — updated as each trade closes. Drives the position
        # sizer so a $1k account and a $100k account produce different results.
        self._equity: float = float(config.initial_capital)
        # Equity peak (high-water mark) for trailing-drawdown enforcement.
        self._equity_peak: float = float(config.initial_capital)
        self._skipped_too_small: int = 0
        self._half_size_count: int = 0
        # Today's realized P&L (closed trades only). Reset at each day boundary.
        self._daily_pnl: float = 0.0
        self._daily_loss_lockouts: int = 0

    def run(self) -> BacktestMetricsResult:
        instrument = self.config.instrument
        tick_size  = TICK_SIZES.get(instrument, 0.25)
        tick_value = TICK_VALUES.get(instrument, 12.50)

        # Build all timeframes
        self.data_handler.build_timeframes(self.config.all_timeframes)
        self.data_handler.filter_date_range(
            pd.Timestamp(self.config.start_date.replace(tzinfo=None)).tz_localize("UTC"),
            pd.Timestamp(self.config.end_date.replace(tzinfo=None)).tz_localize("UTC"),
        )

        primary_bars = self.data_handler.get_timeframe_bars(self.config.primary_timeframe)
        logger.info(f"Starting backtest: {len(primary_bars)} primary bars ({self.config.primary_timeframe})")

        # Prices are already scaled to futures levels by local_cache

        self.strategy.reset_daily_counters()

        total_bars = len(primary_bars)
        # Iterate directly over the index — .iterrows() materializes a Series
        # per bar and the row data isn't used here (only timestamp is). On
        # 98k 1m bars this cuts ~100-150 sec of iteration overhead. Also
        # defer the .iloc[i] row materialization until we actually need it
        # (only when there's an open trade for SL/TP exit checks).
        _index = primary_bars.index
        for i, timestamp in enumerate(_index):
            # Report progress every 50 bars
            if self._progress_callback and i % 50 == 0:
                pct = 40.0 + (i / total_bars * 55.0)
                self._progress_callback(round(pct, 1))

            # Reset daily counters at day boundary
            if self._current_date != timestamp.date():
                self._current_date = timestamp.date()
                self._daily_pnl = 0.0
                self.strategy.reset_daily_counters()

            # ── Manage open trade exits (check SL/TP on each bar) ─────────────
            if self._open_trade:
                current_bar = primary_bars.iloc[i]
                self._check_exits(current_bar, timestamp, tick_size, tick_value)

            # ── Daily loss-limit lockout (Apex Eval $1k/day style) ────────────
            if self.config.daily_loss_limit > 0 and self._daily_pnl <= -self.config.daily_loss_limit:
                if not self._open_trade:
                    self._daily_loss_lockouts += 1
                continue

            # ── Only look for new signals if no open trade ────────────────────
            if not self._open_trade:
                bars = self.data_handler.get_bars_up_to(timestamp, self.config.all_timeframes)
                signal: Optional[TradeSignal] = self.strategy.on_bar(bars)

                if signal and signal.signal != SignalType.NONE:
                    entry = self._apply_slippage(signal.entry_price, signal.signal.value, tick_size)

                    # Risk-based sizing with auto-micro fallback. If the account
                    # is too small to risk even 1 mini, we recompute against the
                    # micro variant (1/10 notional). Returns the actual contract
                    # and its tick math so trades route to the right symbol.
                    contracts, traded_inst, traded_tick_size, traded_tick_value = self._pick_contract_size_with_micro(
                        entry, signal.stop_loss, instrument, signal.contracts
                    )
                    if contracts < 1:
                        self._skipped_too_small += 1
                        continue

                    self._open_trade = SimulatedTrade(
                        instrument=traded_inst,
                        direction=signal.signal.value,
                        entry_price=entry,
                        stop_loss=signal.stop_loss,
                        take_profit=signal.take_profit,
                        contracts=contracts,
                        entry_time=timestamp.to_pydatetime(),
                        conditions_snapshot=signal.metadata,
                    )
                    _risk_dollars = abs(entry - signal.stop_loss) / traded_tick_size * traded_tick_value * contracts
                    _risk_pct = (_risk_dollars / self._equity * 100.0) if self._equity > 0 else 0.0
                    logger.debug(
                        f"  ENTRY {signal.signal.value.upper()} {traded_inst} @ {entry:.2f} | "
                        f"SL={signal.stop_loss:.2f} TP={signal.take_profit:.2f} | "
                        f"size={contracts} | risk=${_risk_dollars:,.0f} ({_risk_pct:.2f}%) | "
                        f"balance=${self._equity:,.0f}"
                    )

        # Close any trade still open at end of data
        if self._open_trade:
            last_bar = primary_bars.iloc[-1]
            last_ts  = primary_bars.index[-1]
            self._force_close_trade(float(last_bar["close"]), last_ts.to_pydatetime(), tick_size, tick_value)

        # Build metrics
        trade_dicts = [
            {
                "entry_time":  t.entry_time,
                "exit_time":   t.exit_time,
                "net_pnl":     t.net_pnl,
                "is_winner":   t.is_winner,
                "exit_reason": t.exit_reason,
            }
            for t in self._completed_trades
        ]
        metrics = calculate_metrics(trade_dicts, self.config.initial_capital)

        logger.info(f"Backtest complete: {metrics.total_trades} trades | WR={metrics.win_rate:.1%} | PF={metrics.profit_factor:.2f} | Net P&L=${metrics.net_profit:,.0f}")
        return metrics

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _apply_slippage(self, price: float, direction: str, tick_size: float) -> float:
        slip = self.config.slippage_ticks * tick_size
        return price + slip if direction == "long" else price - slip

    def _pick_contract_size(
        self, entry: float, stop: float, tick_size: float, tick_value: float, strategy_cap: int
    ) -> int:
        """Risk a fixed % of *current* equity per trade. Position size is
        determined by stop distance:
            risk_$ = equity * risk_pct / 100
            $/contract = (|entry - stop| / tick_size) * tick_value + commissions
            contracts = floor(risk_$ / $/contract)
        Bounded by the strategy's own max_contracts and the runner's hard cap.
        Returns 0 if the account is too small to risk one contract — caller
        must skip the trade."""
        stop_dist_ticks = abs(entry - stop) / tick_size
        if stop_dist_ticks <= 0:
            return 0  # invalid geometry — skip regardless of account size
        # Per-contract worst-case loss = stop distance + round-trip commission
        loss_per_contract = stop_dist_ticks * tick_value + (self.config.commission_per_side * 2)
        if loss_per_contract <= 0:
            return 0
        # Size off a FIXED base (initial capital) by default so contract count
        # scales linearly with account size and the trade set / R-multiples are
        # identical across sizes. Compounding (off current equity) is opt-in.
        sizing_base = self._equity if self.config.compounding else self.config.initial_capital
        risk_dollars = sizing_base * (self.config.risk_per_trade_pct / 100.0)
        if risk_dollars <= 0:
            return 0
        # round (not floor) avoids the systematic under-risk bias that pinned
        # small accounts at 1 contract; floor of 1 means every valid signal is
        # taken (eligibility never depends on account size).
        raw = round(risk_dollars / loss_per_contract)
        contracts = max(1, raw)
        contracts = min(contracts, strategy_cap, self.config.max_contracts_cap)
        contracts = max(1, contracts)  # caps never zero out a valid trade

        # Apex-style trailing-drawdown half-size rule. Once we've consumed
        # `half_size_drawdown_pct` of the trailing-drawdown buffer (drawdown
        # from peak), we halve every subsequent trade's contract size. This
        # cushions the account before the hard limit and keeps it alive
        # through losing streaks.
        td = self.config.trailing_drawdown
        if td > 0 and contracts > 0:
            drawdown_from_peak = self._equity_peak - self._equity
            warning_threshold = td * self.config.half_size_drawdown_pct
            if drawdown_from_peak >= warning_threshold:
                contracts = max(1, contracts // 2)
                self._half_size_count += 1

        return contracts

    def _pick_contract_size_with_micro(self, entry: float, stop: float,
                                        configured_instrument: str, strategy_cap: int):
        """Try sizing on the configured (usually mini) symbol. If that gives
        zero contracts because the account can't afford even one, fall back to
        the micro variant. Returns (contracts, instrument, tick_size, tick_value)."""
        ts = TICK_SIZES.get(configured_instrument, 0.25)
        tv = TICK_VALUES.get(configured_instrument, 12.50)
        n = self._pick_contract_size(entry, stop, ts, tv, strategy_cap)
        if n >= 1:
            return n, configured_instrument, ts, tv
        # Fall back to micro if one exists for this symbol
        micro = MINI_TO_MICRO.get(configured_instrument)
        if not micro:
            return 0, configured_instrument, ts, tv
        ts_m = TICK_SIZES.get(micro, 0.25)
        tv_m = TICK_VALUES.get(micro, 1.25)
        n_m = self._pick_contract_size(entry, stop, ts_m, tv_m, strategy_cap * 10)
        return n_m, micro, ts_m, tv_m

    def _check_exits(self, bar: pd.Series, timestamp: pd.Timestamp, tick_size: float, tick_value: float):
        t = self._open_trade
        # Override with the open trade's actual instrument tick math —
        # critical when the trade was sized on a micro (MNQ/MES/etc) while
        # the strategy's configured instrument is the mini.
        tick_size = TICK_SIZES.get(t.instrument, tick_size)
        tick_value = TICK_VALUES.get(t.instrument, tick_value)
        hit_tp = hit_sl = False

        # ── Break-even management ────────────────────────────────────────
        # Once price moves `breakeven_at_r` × initial-risk in our favor,
        # slide the stop to entry. Trades that initially work and then
        # reverse exit at $0 instead of -1R — biggest WR booster in the
        # engine because it turns losers-that-tried into break-evens.
        be_r = self.config.breakeven_at_r
        if be_r > 0 and not getattr(t, "_be_moved", False):
            initial_risk = abs(t.entry_price - t.stop_loss)
            if t.direction == "long":
                trigger = t.entry_price + initial_risk * be_r
                if bar["high"] >= trigger:
                    t.stop_loss = t.entry_price
                    t._be_moved = True
            else:
                trigger = t.entry_price - initial_risk * be_r
                if bar["low"] <= trigger:
                    t.stop_loss = t.entry_price
                    t._be_moved = True

        if t.direction == "long":
            if bar["low"] <= t.stop_loss:
                hit_sl = True
            elif bar["high"] >= t.take_profit:
                hit_tp = True
        else:
            if bar["high"] >= t.stop_loss:
                hit_sl = True
            elif bar["low"] <= t.take_profit:
                hit_tp = True

        if hit_tp or hit_sl:
            exit_price = t.take_profit if hit_tp else t.stop_loss
            exit_price = self._apply_slippage(exit_price, "short" if t.direction == "long" else "long", tick_size)
            # If stop was moved to break-even and we hit it, count as a BE exit
            # rather than a SL exit — keeps the stats honest.
            reason = ExitReason.TP_HIT
            if hit_sl:
                if getattr(t, "_be_moved", False) and abs(exit_price - t.entry_price) < tick_size * 2:
                    # Stop was moved to entry and triggered there — flat exit
                    # (only commission paid). Tagged distinctly so we can
                    # split BE from real losses in metrics.
                    reason = ExitReason.BREAKEVEN
                else:
                    reason = ExitReason.SL_HIT
            self._close_trade(exit_price, timestamp.to_pydatetime(), tick_size, tick_value, reason)

    def _close_trade(self, exit_price: float, exit_time: datetime, tick_size: float, tick_value: float, reason: ExitReason):
        t = self._open_trade
        if t is None:
            return
        # Use the actual traded instrument's tick math, not the caller's
        # (which is set from the strategy's configured symbol; might differ
        # when this trade was auto-substituted to a micro).
        tick_size = TICK_SIZES.get(t.instrument, tick_size)
        tick_value = TICK_VALUES.get(t.instrument, tick_value)

        t.exit_price  = exit_price
        t.exit_time   = exit_time
        t.exit_reason = reason.value

        if t.direction == "long":
            t.pnl_ticks = (exit_price - t.entry_price) / tick_size
        else:
            t.pnl_ticks = (t.entry_price - exit_price) / tick_size

        t.pnl = t.pnl_ticks * tick_value * t.contracts
        t.commission = self.config.commission_per_side * 2 * t.contracts  # round trip
        t.net_pnl  = t.pnl - t.commission
        # Break-even exits count as wins per user preference (stop moved to entry — risk neutralized)
        t.is_winner = (t.net_pnl > 0) or (reason == ExitReason.BREAKEVEN)

        self.strategy.record_trade_result(t.net_pnl)
        self._completed_trades.append(t)
        self._equity += t.net_pnl  # roll equity forward so next sizer is accurate
        self._daily_pnl += t.net_pnl  # for daily-loss-limit lockout
        if self._equity > self._equity_peak:
            self._equity_peak = self._equity  # track high-water for trailing DD
        self._open_trade = None
        _dd = self._equity_peak - self._equity
        _dd_pct = (_dd / self._equity_peak * 100.0) if self._equity_peak > 0 else 0.0
        logger.debug(
            f"  EXIT {reason.value} @ {exit_price:.2f} | size={t.contracts} | "
            f"Net P&L=${t.net_pnl:,.2f} | balance=${self._equity:,.0f} | "
            f"peak=${self._equity_peak:,.0f} | drawdown=${_dd:,.0f} ({_dd_pct:.2f}%)"
        )

    def _force_close_trade(self, exit_price: float, exit_time: datetime, tick_size: float, tick_value: float):
        self._close_trade(exit_price, exit_time, tick_size, tick_value, ExitReason.SESSION_END)

    @property
    def completed_trades(self) -> list[SimulatedTrade]:
        return self._completed_trades
