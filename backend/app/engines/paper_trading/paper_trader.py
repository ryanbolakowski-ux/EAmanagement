import os
"""
Paper Trading Engine.
Uses real-time market data feeds but simulates order fills.
Tracks PnL, open positions, and session metrics identically to live trading.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, date, timezone, timedelta as _td
from typing import Optional
from loguru import logger
import redis as redis_lib

from app.engines.strategy_engine.base_strategy import BaseStrategy, TradeSignal, SignalType, ExitReason

# Shared connection used by every PaperTrader to coordinate signal locks across
# sibling traders running on the same (user, strategy, instrument). Without this,
# multiple sessions on the same setup all open identical trades simultaneously.
_redis = redis_lib.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"), decode_responses=True, db=0)
_SIGNAL_LOCK_TTL = 600  # seconds — matches typical max position holding window
# Post-start settle window: ignore entry signals for this many seconds after a
# strategy is started, so it doesn't jump into a setup already in progress at
# activation. Exits + indicator seeding are unaffected.
_ENTRY_SETTLE_SECONDS = 120

TICK_VALUES = {
    "ES":  12.50, "NQ":  5.00, "RTY": 5.00, "YM":  5.00,
    "MES": 1.25,  "MNQ": 0.50, "M2K": 0.50, "MYM": 0.50,
}
TICK_SIZES = {
    "ES":  0.25, "NQ":  0.25, "RTY": 0.10, "YM":  1.0,
    "MES": 0.25, "MNQ": 0.25, "M2K": 0.10, "MYM": 1.0,
}
# When the paper account is too small to risk even one mini, fall back
# to the 1/10-notional micro variant. Without this every signal would
# either size to 100 contracts (old bug) or be rejected for an account
# under ~$50k.
MINI_TO_MICRO = {"ES": "MES", "NQ": "MNQ", "RTY": "M2K", "YM": "MYM"}


@dataclass
class PaperPosition:
    instrument: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    contracts: int
    entry_time: datetime
    metadata: dict = field(default_factory=dict)


@dataclass
class PaperTradeResult:
    instrument: str
    direction: str
    entry_price: float
    exit_price: float
    contracts: int
    entry_time: datetime
    exit_time: datetime
    pnl: float
    commission: float
    net_pnl: float
    is_winner: bool
    exit_reason: str
    metadata: dict = field(default_factory=dict)


class PaperTrader:
    """
    Paper trading engine that:
    - Subscribes to real-time tick/bar data
    - Calls strategy.on_bar() or strategy.on_tick() to get signals
    - Simulates fills immediately at market price
    - Monitors SL/TP and closes positions accordingly
    - Enforces risk controls (daily loss, max trades, kill switch)
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        instrument: str,
        commission_per_side: float = 2.25,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        starting_balance: float = 10_000.0,
        risk_per_trade_pct: float = 1.0,
        max_contracts_cap: int = 20,
        daily_loss_pct_kill: float = 3.0,
    ):
        self.strategy    = strategy
        self.instrument  = instrument.upper()
        self.commission  = commission_per_side
        self.session_id  = session_id
        self.user_id     = user_id
        self.strategy_id = strategy_id

        self._position: Optional[PaperPosition] = None
        self._last_price: float = 0.0
        self._completed_trades: list[PaperTradeResult] = []
        self._is_running: bool = False
        self._current_date: Optional[date] = None
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0
        self._kill_switch: bool = False

        # ── Risk-based sizing state ────────────────────────────────────
        # Without this every signal opens `strategy.max_contracts` contracts
        # regardless of account size — that bug cost the user $87k in a day.
        # We now size each trade off current equity (starting balance + rolling
        # P&L) using the same stop-distance math as the backtest engine.
        self._starting_balance: float = float(starting_balance)
        self._equity: float = float(starting_balance)
        self._risk_per_trade_pct: float = float(risk_per_trade_pct)
        self._max_contracts_cap: int = int(max_contracts_cap)
        self._daily_loss_pct_kill: float = float(daily_loss_pct_kill)

        self._bars_buffer: dict[str, list] = {}  # timeframe -> list of bar dicts
        # When True, on_bar updates the buffer but does NOT evaluate signals.
        # The runner sets this during the activation catch-up burst so we don't
        # retroactively fire on bars that printed minutes-to-hours ago.
        self._warmup: bool = False
        # Wall-clock instant the trader was activated. Bars whose CLOSE timestamp
        # predates this are catch-up/replayed history and must never open a
        # position (belt-and-suspenders on top of _warmup). Set in start().
        self._session_started_at: Optional[datetime] = None

    # ─────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    async def start(self):
        self._is_running = True
        self.strategy.reset_daily_counters()
        self._session_started_at = datetime.now(timezone.utc)
        logger.info(f"[PaperTrader] Started | {self.instrument} | Strategy: {self.strategy.config.name} | session_started_at={self._session_started_at.isoformat()}")

    async def stop(self):
        self._is_running = False
        if self._position:
            logger.warning("[PaperTrader] Stopping with open position — position left open in DB.")
        logger.info(f"[PaperTrader] Stopped | Trades: {len(self._completed_trades)} | PnL: ${self._daily_pnl:,.2f}")

    # ─────────────────────────────────────────────────────────────────────────
    # Data feed handlers (called by the data feed layer)
    # ─────────────────────────────────────────────────────────────────────────

    async def on_bar(self, timeframe: str, bar: dict):
        """Called when a new bar closes on any subscribed timeframe."""
        if not self._is_running or self._kill_switch:
            return

        # Track last price for unrealized PnL
        if "close" in bar:
            self._last_price = float(bar["close"])

        # Buffer bar
        if timeframe not in self._bars_buffer:
            self._bars_buffer[timeframe] = []
        self._bars_buffer[timeframe].append(bar)
        if len(self._bars_buffer[timeframe]) > 500:
            self._bars_buffer[timeframe] = self._bars_buffer[timeframe][-500:]

        import pandas as pd
        bars_dict = {
            tf: pd.DataFrame(bars).set_index("timestamp")
            for tf, bars in self._bars_buffer.items()
            if bars
        }

        # Reset daily counters at day boundary
        ts = bar["timestamp"]
        if hasattr(ts, "date"):
            today = ts.date()
            if self._current_date != today:
                self._current_date = today
                self._daily_pnl    = 0.0
                self._daily_trades = 0
                self.strategy.reset_daily_counters()

        # Manage open position SL/TP
        if self._position:
            await self._check_position_exits(bar)

        # Never OPEN a position on a bar that closed before this session was
        # activated. The data feed can replay recent historical 1-min bars as
        # "live" on startup; those must seed indicators only, never trade.
        # (Existing-position exit management above still runs so a position can close.)
        _bar_ts = bar.get("timestamp")
        if self._session_started_at is not None and _bar_ts is not None:
            try:
                import pandas as _pd
                _bts = _pd.Timestamp(_bar_ts)
                if _bts.tzinfo is None:
                    _bts = _bts.tz_localize("UTC")
                # grace = 90s so the bar that's mid-formation at activation still counts
                if _bts.to_pydatetime() < (self._session_started_at - _td(seconds=90)):
                    logger.info(f"[PaperTrader] SKIP entry — bar ts={_bts.isoformat()} predates session_start={self._session_started_at.isoformat()} (catch-up bar, seed-only)")
                    return
            except Exception:
                pass

        # During warmup we only want to seed the buffer with recent bars; we
        # must not open positions on stale signals (e.g. an FVG that printed
        # 30 minutes ago). The runner clears this flag once the buffer has
        # caught up to live data.
        if self._warmup:
            return

        # ── Post-start settle window ──────────────────────────────────────
        # Do NOT open a position in the first _ENTRY_SETTLE_SECONDS after the
        # session activates. Lets a setup FORM on post-start data instead of
        # entering one that was already mid-move at start. (Exit management and
        # buffer seeding above still run.)
        if self._session_started_at is not None:
            _elapsed = (datetime.now(timezone.utc) - self._session_started_at).total_seconds()
            if _elapsed < _ENTRY_SETTLE_SECONDS:
                if not getattr(self, "_settle_logged", False):
                    logger.info(f"[PaperTrader] settle window — no new entries for the first {_ENTRY_SETTLE_SECONDS}s after start ({self.instrument})")
                    self._settle_logged = True
                return

        # Look for entry signal
        if not self._position and self.strategy.check_risk_controls():
            signal = self.strategy.on_bar(bars_dict)
            if signal and signal.signal != SignalType.NONE:
                # ── Overtrade guard (cooldown / max-trades / max-positions / dup) ──
                # Centralized rules so paper/live/options-paper enforce identically.
                # Reads strategy.cooldown_min, strategy.max_trades_per_day,
                # strategy.max_open_positions. Open-position checks use an
                # in-memory snapshot of sibling traders in the same session
                # (the paper runner only persists CLOSED trades, so a DB query
                # on status='open' would always return zero).
                try:
                    from app.engines.entry_guard import can_enter
                    from app.engines.paper_trading.runner import _active_traders as _at_map
                    snap = []
                    sid_str = str(self.session_id) if self.session_id else ""
                    for _k, _tr in list(_at_map.items()):
                        if not _k.startswith(sid_str + ":") and _k != sid_str:
                            continue
                        _pos = getattr(_tr, "_position", None)
                        if _pos:
                            snap.append({"session_id": sid_str,
                                          "instrument": getattr(_pos, "instrument", "")})
                    decision = await can_enter(
                        session_id=sid_str,
                        strategy_id=str(self.strategy_id) if self.strategy_id else "",
                        instrument=self.instrument,
                        direction=signal.signal.value,
                        mode="paper",
                        open_positions_snapshot=snap,
                        bar_time=bar.get("timestamp"),  # PAPER-TRADER-GUARD-BARCLOCK-V1
                        entry_price=getattr(signal, "entry_price", None),
                    )
                    if not decision.allowed:
                        # Guard already logged the reason; release any lock we may have
                        return
                except Exception as _ge:
                    logger.error(f"[PaperTrader] entry-guard error (failing open): {_ge}")

                # Cross-session signal lock: only one trader for this user's
                # (strategy, instrument) can hold a position at a time. If
                # another runner already opened the same setup we skip silently.
                if not self._acquire_signal_lock():
                    logger.debug(f"[PaperTrader] {self.instrument} signal skipped — sibling trader already active")
                    return
                await self._open_position(signal, bar["timestamp"])

    async def on_tick(self, tick: dict):
        """Called on each incoming tick. Used for tighter exit management."""
        if not self._is_running or self._kill_switch or not self._position:
            return
        await self._check_position_exits_on_tick(tick)

    # ─────────────────────────────────────────────────────────────────────────
    # Position management
    # ─────────────────────────────────────────────────────────────────────────

    def _signal_lock_key(self) -> Optional[str]:
        if not (self.user_id and self.strategy_id):
            return None
        return f"signal_lock:{self.user_id}:{self.strategy_id}:{self.instrument}"

    def _acquire_signal_lock(self) -> bool:
        """Try to grab the (user, strategy, instrument) lock. Returns False if
        another trader already holds it — caller should skip the signal."""
        key = self._signal_lock_key()
        if not key:
            return True  # local-only/test mode: don't block
        try:
            ok = _redis.set(key, self.session_id or "anon", nx=True, ex=_SIGNAL_LOCK_TTL)
            return bool(ok)
        except Exception:
            return True  # fail-open if Redis is unreachable

    def _release_signal_lock(self) -> None:
        key = self._signal_lock_key()
        if not key:
            return
        try:
            # Only delete if we still own it — guard against expired locks
            owner = _redis.get(key)
            if owner == (self.session_id or "anon"):
                _redis.delete(key)
        except Exception:
            pass

    # ── Risk-based contract sizing ───────────────────────────────────────
    def _pick_contract_size(self, entry: float, stop: float,
                            tick_size: float, tick_value: float,
                            strategy_cap: int) -> int:
        """Return the number of contracts to trade given the stop distance and
        the user's risk budget. Mirrors the backtest engine's sizing so paper
        and backtest stay consistent. Zero means the account is too small to
        risk even one contract — caller should fall back to micro or skip."""
        stop_dist_ticks = abs(entry - stop) / tick_size
        if stop_dist_ticks <= 0:
            return 0
        loss_per_contract = stop_dist_ticks * tick_value + (self.commission * 2)
        if loss_per_contract <= 0:
            return 0
        risk_dollars = self._equity * (self._risk_per_trade_pct / 100.0)
        if risk_dollars <= 0:
            return 0
        raw = int(risk_dollars // loss_per_contract)
        return max(0, min(raw, strategy_cap, self._max_contracts_cap))

    def _pick_contract_size_with_micro(self, entry: float, stop: float,
                                        configured_instrument: str,
                                        strategy_cap: int):
        """Size on the configured (mini) symbol; if the account can't afford
        even one mini, fall back to the micro variant at 10× the cap.
        Returns (contracts, instrument, tick_size, tick_value)."""
        ts = TICK_SIZES.get(configured_instrument, 0.25)
        tv = TICK_VALUES.get(configured_instrument, 12.50)
        n = self._pick_contract_size(entry, stop, ts, tv, strategy_cap)
        if n >= 1:
            return n, configured_instrument, ts, tv
        micro = MINI_TO_MICRO.get(configured_instrument)
        if not micro:
            return 0, configured_instrument, ts, tv
        ts_m = TICK_SIZES.get(micro, 0.25)
        tv_m = TICK_VALUES.get(micro, 1.25)
        n_m = self._pick_contract_size(entry, stop, ts_m, tv_m, strategy_cap * 10)
        return n_m, micro, ts_m, tv_m

    async def route_external_signal(self, signal, source_signal_id=None):
        """ROUTING (#156): enter an EXTERNAL (email) signal into this paper
        session through the same guards as on_bar. Returns (entered, reason).
        The signal-lock/entry-guard make this dedup-safe vs the trader's own
        on_bar, so routing can't double-enter."""
        from datetime import datetime as _dt, timezone as _tz
        try:
            if not getattr(self, "_is_running", False) or getattr(self, "_kill_switch", False):
                return False, "session_not_running"
            if getattr(self, "_warmup", False):
                return False, "warmup"
            if self._position:
                return False, "already_in_position"
            if getattr(self, "_session_started_at", None) is not None:
                if (_dt.now(_tz.utc) - self._session_started_at).total_seconds() < _ENTRY_SETTLE_SECONDS:
                    return False, "settle_window"
            if not self.strategy.check_risk_controls():
                return False, "risk_controls"
            try:
                from app.engines.entry_guard import can_enter
                d = await can_enter(session_id=str(self.session_id or ""), strategy_id=str(self.strategy_id or ""),
                                    instrument=self.instrument, direction=signal.signal.value, mode="paper",
                                    open_positions_snapshot=[])
                if not d.allowed:
                    return False, f"entry_guard:{getattr(d, 'reason', 'blocked')}"
            except Exception as _ge:
                logger.error(f"[PaperTrader] route entry-guard error (fail-open): {_ge}")
            if not self._acquire_signal_lock():
                return False, "signal_locked_sibling"
            self._routed_source_signal_id = source_signal_id
            await self._open_position(signal, _dt.now(_tz.utc))
            return (self._position is not None), ("entered" if self._position else "open_failed")
        except Exception as e:
            return False, f"error:{type(e).__name__}"

    async def _open_position(self, signal: TradeSignal, timestamp):
        # Recompute size from current equity & stop distance — never trust
        # signal.contracts (which is just strategy.max_contracts).
        strategy_cap = max(1, int(self.strategy.config.max_contracts or 1))
        contracts, traded_instrument, _ts, _tv = self._pick_contract_size_with_micro(
            signal.entry_price, signal.stop_loss, self.instrument, strategy_cap,
        )
        if contracts < 1:
            logger.warning(
                f"[Paper] SKIP signal on {self.instrument} — account too small "
                f"to risk {self._risk_per_trade_pct}% (equity ${self._equity:,.0f})"
            )
            self._release_signal_lock()
            return
        # Bug fix: use bar timestamp (deterministic, bar-aligned) instead of
        # datetime.now() which causes millisecond-apart entry/exit times
        # when SL/TP triggers on the same bar.
        _entry_ts = timestamp if isinstance(timestamp, datetime) else datetime.now(timezone.utc)
        if getattr(_entry_ts, "tzinfo", None) is None:
            _entry_ts = _entry_ts.replace(tzinfo=timezone.utc)
        self._position = PaperPosition(
            instrument=traded_instrument,
            direction=signal.signal.value,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            contracts=contracts,
            entry_time=_entry_ts,
            metadata={**(signal.metadata or {}), "sized_from_equity": self._equity,
                     "configured_instrument": self.instrument,
                     "traded_instrument": traded_instrument},
        )
        logger.info(
            f"[Paper] OPEN {signal.signal.value.upper()} {contracts}x {traded_instrument} "
            f"@ {signal.entry_price:.2f} | SL={signal.stop_loss:.2f} | TP={signal.take_profit:.2f} "
            f"| equity=${self._equity:,.0f} risk={self._risk_per_trade_pct}%"
        )
        # Audit line matching the [paper-runner] decision log format so
        # an operator can see the full ALLOWED → ENTERED chain in one grep.
        logger.info(
            f"[paper-runner] sid={self.session_id} ENTERED inst={traded_instrument} "
            f"dir={signal.signal.value} entry={signal.entry_price:.2f} contracts={contracts}"
        )
        # Issue 2: trade-creation-source + signal linkage for audit/grep.
        logger.info(
            f"[trade-source] mode=paper session={self.session_id} "
            f"strategy_id={self.strategy_id} inst={traded_instrument} "
            f"dir={signal.signal.value} source=strategy_signal "
            f"source_signal_id={getattr(signal, 'source_signal_id', None)}"
        )

        # Email a signal receipt to the user — they want to know when paper
        # fires so they can mirror on prop-firm accounts (where algo execution
        # is banned, paper acts as their signal stream).
        try:
            from app.database import async_session_factory as _asf
            from app.models.user import User as _U
            from app.services.email import send_trade_receipt_email
            from app.engines.options.premarket_scheduler import (
                _claim_session_slot as _claim_sess,
                _claim_daily_slot as _claim_day,
                _current_session_label as _sess_label,
            )
            from sqlalchemy import select as _sel
            import asyncio as _asyncio
            async def _send():
                # Atomic email cap before sending — paper signals share the
                # same 1-per-(user, instrument-family, session) + 8/day budget
                sess = _sess_label()
                if not await _claim_sess(str(self.user_id), traded_instrument, sess):
                    logger.info(f"[Paper] CAP-HIT {traded_instrument} for user — already signaled {sess} session")
                    return
                if not await _claim_day(str(self.user_id), max_per_day=8):
                    logger.info(f"[Paper] CAP-HIT {traded_instrument} for user — daily cap (8) reached")
                    return
                async with _asf() as db:
                    u = (await db.execute(_sel(_U).where(_U.id == self.user_id))).scalar_one_or_none()
                if u and u.email:
                    send_trade_receipt_email(
                        to=u.email, username=u.username or "",
                        ticker=traded_instrument,
                        direction=signal.signal.value,
                        entry=signal.entry_price,
                        stop=signal.stop_loss,
                        target=signal.take_profit,
                        contracts=contracts,
                        reason=(signal.metadata or {}).get("note") or self.strategy.config.name,
                        strategy_name=self.strategy.config.name,
                        mode="paper",
                    )
            _asyncio.create_task(_send())
        except Exception as e:
            logger.warning(f"[Paper] signal email skipped: {e}")

    async def _check_position_exits(self, bar: dict):
        p = self._position
        if not p:
            return
        # Bug fix: don't allow exit on the same bar as entry. In reality
        # you can't enter at one bar's close and have SL/TP trigger before
        # the next tick prints. Without this guard, SL+TP both trigger on
        # the entry bar producing 0-second hold trades.
        bar_ts = bar.get("timestamp")
        if isinstance(bar_ts, datetime) and isinstance(p.entry_time, datetime):
            try:
                if bar_ts <= p.entry_time:
                    return
            except Exception:
                pass
        hit_tp = hit_sl = False
        if p.direction == "long":
            if bar["low"] <= p.stop_loss:
                hit_sl = True
            elif bar["high"] >= p.take_profit:
                hit_tp = True
        else:
            if bar["high"] >= p.stop_loss:
                hit_sl = True
            elif bar["low"] <= p.take_profit:
                hit_tp = True

        if hit_tp or hit_sl:
            exit_price = p.take_profit if hit_tp else p.stop_loss
            reason = ExitReason.TP_HIT if hit_tp else ExitReason.SL_HIT
            await self._close_position(exit_price, bar["timestamp"], reason)

    async def _check_position_exits_on_tick(self, tick: dict):
        p = self._position
        if not p:
            return
        price = tick["price"]
        hit_tp = hit_sl = False
        if p.direction == "long":
            if price <= p.stop_loss:
                hit_sl = True
            elif price >= p.take_profit:
                hit_tp = True
        else:
            if price >= p.stop_loss:
                hit_sl = True
            elif price <= p.take_profit:
                hit_tp = True

        if hit_tp or hit_sl:
            exit_price = p.take_profit if hit_tp else p.stop_loss
            reason = ExitReason.TP_HIT if hit_tp else ExitReason.SL_HIT
            await self._close_position(exit_price, tick["timestamp"], reason)

    async def _close_position(self, exit_price: float, exit_time, reason: ExitReason):
        p = self._position
        if not p:
            return

        # Bug #13 fix: use p.instrument (the actual traded symbol, which may
        # be a micro after fallback) not self.instrument (the configured one).
        tick_size  = TICK_SIZES.get(p.instrument, 0.25)
        tick_value = TICK_VALUES.get(p.instrument, 12.50)

        if p.direction == "long":
            pnl_ticks = (exit_price - p.entry_price) / tick_size
        else:
            pnl_ticks = (p.entry_price - exit_price) / tick_size

        pnl       = pnl_ticks * tick_value * p.contracts
        commission = self.commission * 2 * p.contracts
        net_pnl   = pnl - commission
        is_winner = net_pnl > 0

        result = PaperTradeResult(
            instrument=p.instrument,
            direction=p.direction,
            entry_price=p.entry_price,
            exit_price=exit_price,
            contracts=p.contracts,
            entry_time=p.entry_time,
            exit_time=exit_time if isinstance(exit_time, datetime) else datetime.now(timezone.utc),
            pnl=pnl,
            commission=commission,
            net_pnl=net_pnl,
            is_winner=is_winner,
            exit_reason=reason.value,
            # Issue 3: carry the real SL/TP so the persisted trade (and the
            # chart modal) draw the levels instead of 0.
            metadata={**(p.metadata or {}), "stop_loss": p.stop_loss,
                      "take_profit": p.take_profit},
        )

        self._completed_trades.append(result)
        self._daily_pnl    += net_pnl
        self._daily_trades += 1
        self._equity       += net_pnl   # roll equity for next trade sizing
        self.strategy.record_trade_result(net_pnl)

        logger.info(
            f"[Paper] CLOSE {reason.value} @ {exit_price:.2f} | Net PnL: ${net_pnl:,.2f} "
            f"| {'WIN' if is_winner else 'LOSS'} | equity=${self._equity:,.0f}"
        )
        self._position = None
        self._release_signal_lock()

        # ── Daily-loss circuit breaker ──────────────────────────────
        # If we drop more than `daily_loss_pct_kill` % of starting balance
        # in a single session day, flip the kill switch — no more trades
        # today. Prevents another $87k-style bleed.
        loss_cap = self._starting_balance * (self._daily_loss_pct_kill / 100.0)
        if loss_cap > 0 and self._daily_pnl <= -loss_cap:
            logger.error(
                f"[Paper] DAILY LOSS LIMIT HIT (${self._daily_pnl:,.0f} <= -${loss_cap:,.0f}) "
                f"— killing session for the day."
            )
            self.trigger_kill_switch()

    def trigger_kill_switch(self):
        self._kill_switch = True
        self.strategy.trigger_kill_switch()
        logger.warning("[PaperTrader] KILL SWITCH TRIGGERED — no new trades will be placed.")

    @property
    def stats(self) -> dict:
        trades = self._completed_trades
        total  = len(trades)
        wins   = sum(1 for t in trades if t.is_winner)
        closed_pnl = sum(t.net_pnl for t in trades)
        
        # Calculate unrealized P&L from open position
        unrealized = 0.0
        if self._position and hasattr(self, '_last_price') and self._last_price:
            tick_value = TICK_VALUES.get(self._position.instrument, 5.0)  # #13 fix
            tick_size = 0.25 if self._position.instrument in ('ES', 'NQ', 'YM') else 0.10
            if self._position.direction == 'long':
                unrealized = ((self._last_price - self._position.entry_price) / tick_size) * tick_value * self._position.contracts
            else:
                unrealized = ((self._position.entry_price - self._last_price) / tick_size) * tick_value * self._position.contracts
        
        return {
            "total_trades": total,
            "win_rate":     (wins / total) if total else 0.0,
            "net_pnl":      closed_pnl + unrealized,
            "closed_pnl":   closed_pnl,
            "unrealized_pnl": unrealized,
            "daily_pnl":    self._daily_pnl,
            "is_running":   self._is_running,
            "kill_switch":  self._kill_switch,
            "open_position": bool(self._position),
        }
