"""Options paper trading.

Mirrors the futures paper trader's behaviour:
  • Receive directional signals from the underlying-based strategy
  • Translate each signal into an option contract via the strike picker
  • Mark-to-model the position with Black-Scholes against the live underlying
  • Close at the user's R:R target (in premium %), at the stop, or at expiration

The "fill price" is the BS theoretical at signal time. That's the best we
can do without live IV from Polygon's snapshot endpoint — for paper this is
fine; for live we'd swap in the broker's actual quote.
"""
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from typing import Optional
from loguru import logger

from app.engines.options.pricing import greeks, price as bs_price
from app.engines.options.polygon_options import OptionContract
from app.engines.options.strike_picker import pick_strike, StrikePick

# Risk-free rate assumed for all pricing (FRED 3-month T-bill, refresh quarterly)
RISK_FREE_RATE = 0.045


@dataclass
class OptionPaperPosition:
    underlying: str
    direction: str             # 'long' or 'short' (long call, long put, etc.)
    contract: OptionContract
    short_leg: Optional[OptionContract]  # for verticals
    contracts: int             # number of options (each = 100 underlying shares)
    entry_premium: float       # per share, what we paid
    estimated_iv: float
    entry_time: datetime
    entry_spot: float
    stop_premium: float        # premium below which we exit (loss cap)
    target_premium: float      # premium above which we exit (profit target)
    metadata: dict = field(default_factory=dict)


@dataclass
class OptionPaperResult:
    underlying: str
    contract_ticker: str
    direction: str
    contracts: int
    entry_premium: float
    exit_premium: float
    entry_spot: float
    exit_spot: float
    entry_time: datetime
    exit_time: datetime
    gross_pnl: float           # (exit-entry) * contracts * 100
    commission: float
    net_pnl: float
    is_winner: bool
    exit_reason: str           # 'target' / 'stop' / 'expiration' / 'manual'
    metadata: dict = field(default_factory=dict)


class OptionsPaperTrader:
    """One trader instance per (user, strategy, underlying). Spawned by the
    options paper runner when an underlying-based directional strategy enters
    "options mode" — instead of buying/selling the underlying, it picks an
    option contract and trades that."""

    def __init__(self, underlying: str, chain: list[OptionContract],
                 starting_balance: float = 10_000.0,
                 risk_per_trade_pct: float = 1.5,
                 commission_per_contract: float = 0.65,
                 stop_loss_premium_pct: float = 50.0,   # exit at -50% premium
                 target_premium_pct: float = 100.0,     # exit at +100% premium
                 daily_loss_pct_kill: float = 5.0,
                 session_id: Optional[str] = None,
                 user_id: Optional[str] = None,
                 strategy_id: Optional[str] = None):
        self.underlying  = underlying.upper()
        self.chain       = chain
        self.commission  = commission_per_contract
        self.session_id  = session_id
        self.user_id     = user_id
        self.strategy_id = strategy_id

        # Risk state — mirrors futures paper trader so behaviour is consistent
        self._starting_balance     = float(starting_balance)
        self._equity               = float(starting_balance)
        self._risk_per_trade_pct   = float(risk_per_trade_pct)
        self._stop_loss_pct        = stop_loss_premium_pct
        self._target_pct           = target_premium_pct
        self._daily_loss_pct_kill  = float(daily_loss_pct_kill)

        self._position: Optional[OptionPaperPosition] = None
        self._completed: list[OptionPaperResult] = []
        self._daily_pnl: float = 0.0
        self._current_date: Optional[date] = None
        self._kill_switch: bool = False
        self._is_running: bool = False

    # ── Sizing ──────────────────────────────────────────────────────────────

    def _size_position(self, entry_premium: float) -> int:
        """Risk-based contract sizing.

        Per-contract max loss ≈ premium × 100 × (stop_loss_pct / 100).
        contracts = floor(equity × risk% / loss_per_contract)
        Bounded to >= 0; caller should skip on 0."""
        if entry_premium <= 0:
            return 0
        loss_per_contract = entry_premium * 100 * (self._stop_loss_pct / 100.0) + (self.commission * 2)
        if loss_per_contract <= 0:
            return 0
        risk_dollars = self._equity * (self._risk_per_trade_pct / 100.0)
        if risk_dollars <= 0:
            return 0
        n = int(risk_dollars // loss_per_contract)
        return max(0, n)

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def start(self):
        self._is_running = True
        logger.info(f"[OptionsPaper] Started | {self.underlying} | "
                     f"chain_size={len(self.chain)} | equity=${self._equity:,.0f}")

    def stop(self):
        self._is_running = False
        logger.info(f"[OptionsPaper] Stopped | trades={len(self._completed)} | "
                     f"PnL=${self._daily_pnl:,.2f} | equity=${self._equity:,.0f}")

    # ── Signal ingress ──────────────────────────────────────────────────────

    def on_signal(self, side: str, spot: float, today: date,
                   delta_band: tuple[float, float] = (0.30, 0.50),
                   dte_band: tuple[int, int] = (30, 60),
                   prefer_itm: bool = False,
                   spread_width: Optional[int] = None,
                   default_iv: float = 0.30) -> Optional[OptionPaperPosition]:
        """Translate a directional signal into an option entry. `side` is
        'long' or 'short' — long bullish → call, long bearish → put. We
        don't currently sell premium in paper (short calls/puts) because
        the user's framework doesn't include naked-short strategies.
        Returns the opened position or None if the trade was skipped."""
        if not self._is_running or self._kill_switch or self._position:
            return None

        # Bullish signal → call; bearish signal → put. (Short selling
        # premium is a separate strategy mode handled by the wheel/spread paths.)
        option_side = "call" if side in ("long", "bullish") else "put"

        pick = pick_strike(
            self.chain, spot=spot, today=today, side=option_side,
            delta_min=delta_band[0], delta_max=delta_band[1],
            dte_min=dte_band[0], dte_max=dte_band[1],
            default_iv=default_iv, prefer_itm=prefer_itm,
            spread_width=spread_width,
        )
        if pick is None or pick.band_missed:
            logger.warning(f"[OptionsPaper] SKIP {self.underlying} {option_side} — no contract in band")
            return None

        # BS price the entry premium
        t = max(1, pick.days_to_expiration) / 365.0
        entry_premium = bs_price(
            s=spot, k=pick.long.strike, t=t, sigma=pick.estimated_iv,
            r=RISK_FREE_RATE, opt_type=option_side,
        )
        if entry_premium <= 0.05:
            logger.warning(f"[OptionsPaper] SKIP — entry premium too small ({entry_premium:.2f})")
            return None

        contracts = self._size_position(entry_premium)
        if contracts < 1:
            logger.warning(f"[OptionsPaper] SKIP — account can't afford 1 contract "
                            f"(premium ${entry_premium*100:.0f}/contract, equity ${self._equity:,.0f})")
            return None

        stop_premium   = entry_premium * (1 - self._stop_loss_pct / 100.0)
        target_premium = entry_premium * (1 + self._target_pct    / 100.0)

        self._position = OptionPaperPosition(
            underlying=self.underlying,
            direction=option_side,
            contract=pick.long,
            short_leg=pick.short,
            contracts=contracts,
            entry_premium=entry_premium,
            estimated_iv=pick.estimated_iv,
            entry_time=datetime.now(timezone.utc),
            entry_spot=spot,
            stop_premium=stop_premium,
            target_premium=target_premium,
            metadata={
                "delta_at_entry": pick.actual_delta,
                "dte_at_entry":   pick.days_to_expiration,
                "pick_reason":    pick.reason,
            },
        )
        logger.info(f"[OptionsPaper] OPEN {option_side.upper()} {contracts}x "
                     f"{pick.long.ticker} @ ${entry_premium:.2f} "
                     f"(delta {pick.actual_delta:+.2f}, {pick.days_to_expiration}DTE) "
                     f"stop ${stop_premium:.2f} target ${target_premium:.2f}")
        return self._position

    # ── Mark-to-model ───────────────────────────────────────────────────────

    def on_spot_tick(self, spot: float, now: datetime) -> Optional[OptionPaperResult]:
        """Re-mark the open position against current spot. Closes if the
        mark hits stop, target, or the expiration day arrives. Returns the
        closed result (or None if still open)."""
        if not self._position or self._kill_switch:
            return None
        p = self._position

        # Day-boundary daily counters reset
        if self._current_date != now.date():
            self._current_date = now.date()
            self._daily_pnl = 0.0

        # Days to expiration as of right now
        dte = (p.contract.expiration - now.date()).days
        if dte <= 0:
            # Force-close at intrinsic value
            mark = max(0.0, (spot - p.contract.strike) if p.direction == "call"
                              else (p.contract.strike - spot))
            return self._close_position(mark, now, "expiration")

        # Mark-to-model against the same IV we used at entry. Real IV will
        # have drifted but we don't have a live quote — this is best-effort.
        t = dte / 365.0
        mark = bs_price(
            s=spot, k=p.contract.strike, t=t, sigma=p.estimated_iv,
            r=RISK_FREE_RATE, opt_type=p.direction,
        )

        if mark <= p.stop_premium:
            return self._close_position(mark, now, "stop")
        if mark >= p.target_premium:
            return self._close_position(mark, now, "target")
        return None

    def _close_position(self, exit_premium: float, exit_time: datetime,
                         reason: str) -> OptionPaperResult:
        p = self._position
        assert p is not None

        gross    = (exit_premium - p.entry_premium) * p.contracts * 100
        commission = self.commission * p.contracts * 2  # round-trip
        net      = gross - commission

        result = OptionPaperResult(
            underlying=p.underlying, contract_ticker=p.contract.ticker,
            direction=p.direction, contracts=p.contracts,
            entry_premium=p.entry_premium, exit_premium=exit_premium,
            entry_spot=p.entry_spot, exit_spot=0.0,  # caller can backfill
            entry_time=p.entry_time, exit_time=exit_time,
            gross_pnl=gross, commission=commission, net_pnl=net,
            is_winner=(net > 0),
            exit_reason=reason, metadata=p.metadata,
        )
        self._completed.append(result)
        self._daily_pnl += net
        self._equity    += net
        logger.info(f"[OptionsPaper] CLOSE {reason} @ ${exit_premium:.2f} | "
                     f"Net ${net:,.2f} | equity ${self._equity:,.0f}")
        self._position = None

        # Daily loss circuit breaker (same as futures paper)
        loss_cap = self._starting_balance * (self._daily_loss_pct_kill / 100.0)
        if loss_cap > 0 and self._daily_pnl <= -loss_cap:
            logger.error(f"[OptionsPaper] DAILY LOSS LIMIT — kill switch tripped")
            self._kill_switch = True

        return result

    # ── Status ──────────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        wins = sum(1 for t in self._completed if t.is_winner)
        return {
            "total_trades": len(self._completed),
            "wins": wins,
            "win_rate": (wins / len(self._completed)) if self._completed else 0.0,
            "net_pnl": sum(t.net_pnl for t in self._completed),
            "daily_pnl": self._daily_pnl,
            "equity": self._equity,
            "open_position": bool(self._position),
            "kill_switch": self._kill_switch,
            "is_running": self._is_running,
        }
