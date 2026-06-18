"""
Live Trading Engine.
Wraps the broker adapter and handles real-money order execution,
position management, and risk enforcement.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, date, timezone, timedelta as _td
from typing import Optional
from loguru import logger

from app.engines.strategy_engine.base_strategy import BaseStrategy, SignalType, ExitReason
from app.engines.live_trading.broker_base import BrokerBase, OrderRequest, OrderSide, OrderType

TICK_SIZES  = {"ES": 0.25, "NQ": 0.25, "RTY": 0.10, "YM": 1.0}
TICK_VALUES = {"ES": 12.50, "NQ": 5.00, "RTY": 5.00, "YM": 5.00}
MINI_TO_MICRO = {"ES": "MES", "NQ": "MNQ", "RTY": "M2K", "YM": "MYM"}
TICK_SIZES.update({"MES": 0.25, "MNQ": 0.25, "M2K": 0.10, "MYM": 1.0})
TICK_VALUES.update({"MES": 1.25, "MNQ": 0.50, "M2K": 0.50, "MYM": 0.50})



@dataclass
class LivePosition:
    instrument: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    contracts: int
    entry_time: datetime
    broker_order_id: str = ""
    sl_order_id: str = ""
    tp_order_id: str = ""
    metadata: dict = field(default_factory=dict)


class LiveTrader:
    """
    Live trading engine that:
    - Receives signals from a strategy
    - Places actual orders via the broker
    - Places SL/TP bracket orders automatically
    - Monitors fill confirmations
    - Enforces all risk controls including kill switch
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        broker: BrokerBase,
        instrument: str,
        commission_per_side: float = 2.25,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        broker_account_id: Optional[str] = None,
    ):
        self.strategy   = strategy
        self.broker     = broker
        self.instrument = instrument.upper()
        self.commission = commission_per_side
        # Identifiers used by the shared entry-guard + downstream DB writes.
        # Previously the live runner passed session_id but __init__ didn't
        # accept it → constructor raised. We accept them all here.
        self.session_id  = session_id
        self.user_id     = user_id
        self.strategy_id = strategy_id
        self.broker_account_id = broker_account_id

        self._position: Optional[LivePosition] = None
        self._is_running: bool = False
        self._kill_switch: bool = False
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0
        self._current_date: Optional[date] = None
        self._bars_buffer: dict[str, list] = {}
        # Wall-clock instant trading went live. Bars closing before this are
        # catch-up/replayed history and must never open a real-money position.
        self._session_started_at: Optional[datetime] = None

    async def start(self):
        if not self.broker.is_connected:
            connected = await self.broker.connect()
            if not connected:
                raise RuntimeError("Failed to connect to broker")

        self._is_running = True
        self.strategy.reset_daily_counters()
        self._session_started_at = datetime.now(timezone.utc)
        logger.info(f"[LiveTrader] session_started_at={self._session_started_at.isoformat()}")

        # Subscribe to market data
        await self.broker.subscribe_quotes(self.instrument, self.on_tick)
        tfs = [self.strategy.config.primary_timeframe, self.strategy.config.execution_timeframe] + self.strategy.config.higher_timeframes
        for tf in set(tfs):
            await self.broker.subscribe_bars(self.instrument, tf, lambda bar, t=tf: asyncio.create_task(self.on_bar(t, bar)))

        logger.info(f"[LiveTrader] LIVE TRADING STARTED | {self.instrument} | Strategy: {self.strategy.config.name}")

    async def stop(self):
        self._is_running = False
        if self._position:
            logger.warning("[LiveTrader] Stopping with open position. Consider closing manually.")
        await self.broker.disconnect()
        logger.info("[LiveTrader] Stopped")

    def trigger_kill_switch(self):
        self._kill_switch = True
        self.strategy.trigger_kill_switch()
        logger.warning("[LiveTrader] ⚠️  KILL SWITCH ACTIVATED — all trading halted.")

    async def on_bar(self, timeframe: str, bar: dict):
        if not self._is_running or self._kill_switch:
            return

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

        ts = bar.get("timestamp", datetime.utcnow())
        today = ts.date() if isinstance(ts, datetime) else ts
        if self._current_date != today:
            self._current_date = today
            self._daily_pnl    = 0.0
            self._daily_trades = 0
            self.strategy.reset_daily_counters()

        # Never OPEN a real-money position on a bar that closed before this
        # session went live. On startup the broker/data feed can replay recent
        # historical bars; those must seed indicators only, never trade.
        _bar_ts = bar.get("timestamp")
        if self._session_started_at is not None and _bar_ts is not None:
            try:
                import pandas as _pd
                _bts = _pd.Timestamp(_bar_ts)
                if _bts.tzinfo is None:
                    _bts = _bts.tz_localize("UTC")
                if _bts.to_pydatetime() < (self._session_started_at - _td(seconds=90)):
                    logger.info(f"[LiveTrader] SKIP entry — bar ts={_bts.isoformat()} predates session_start={self._session_started_at.isoformat()} (catch-up bar, seed-only)")
                    return
            except Exception:
                pass

        if not self._position and self.strategy.check_risk_controls():
            signal = self.strategy.on_bar(bars_dict)
            if signal and signal.signal != SignalType.NONE:
                # ── Overtrade guard (cooldown / max-trades / max-positions / dup) ──
                # Same rules as paper. For live we pass an in-memory snapshot
                # built from self._position — sibling instruments in the same
                # session would each have their own LiveTrader instance, so
                # any caller wanting cross-instrument enforcement should pass
                # in a session-level position dict instead.
                try:
                    from app.engines.entry_guard import can_enter
                    snap = []
                    if self._position:
                        snap.append({"session_id": str(self.session_id or ""),
                                      "instrument": self._position.instrument})
                    decision = await can_enter(
                        session_id=str(self.session_id) if self.session_id else "",
                        strategy_id=str(self.strategy_id) if self.strategy_id else "",
                        instrument=self.instrument,
                        direction=signal.signal.value,
                        mode="live",
                        open_positions_snapshot=snap,
                        bar_time=bar.get("timestamp"),  # GUARD-BARCLOCK-V1
                        entry_price=getattr(signal, "entry_price", None),
                    )
                    if not decision.allowed:
                        return
                except Exception as _ge:
                    logger.error(f"[LiveTrader] entry-guard error (failing open): {_ge}")
                await self._execute_entry(signal)

    async def on_tick(self, tick: dict):
        if not self._is_running or self._kill_switch or not self._position:
            return
        # Tick-level exit check (belt and suspenders on top of bracket orders)
        p = self._position
        price = tick["price"]
        if p.direction == "long":
            if price <= p.stop_loss:
                logger.info(f"[LiveTrader] Tick SL reached @ {price:.2f}")
                await self._cancel_bracket_and_close(price, ExitReason.SL_HIT)
            elif price >= p.take_profit:
                # Bug #12 fix: also exit on tick-level TP. Belt and suspenders
                # in case the broker-side bracket TP order silently rejects
                # or cancels (Tradovate has done this on partial fills).
                logger.info(f"[LiveTrader] Tick TP reached @ {price:.2f}")
                await self._cancel_bracket_and_close(price, ExitReason.TP_HIT)
        else:
            if price >= p.stop_loss:
                logger.info(f"[LiveTrader] Tick SL reached @ {price:.2f}")
                await self._cancel_bracket_and_close(price, ExitReason.SL_HIT)
            elif price <= p.take_profit:
                logger.info(f"[LiveTrader] Tick TP reached @ {price:.2f}")
                await self._cancel_bracket_and_close(price, ExitReason.TP_HIT)

    def _pick_contract_size(self, entry, stop, tick_size, tick_value, cap):
        """Risk-based size via the shared unified_size module (#136).

        Sizes by the user's account risk settings when wired through
        (``_risk_per_trade_usd`` / ``_risk_per_trade_pct`` / ``_equity``,
        set by the live runner from the BrokerAccount). When NONE of those
        are present we fall back to the historical conservative default of
        $200 fixed risk per trade so behaviour is unchanged for unwired
        sessions. Caller handles a 0 return (mini->micro fallback)."""
        if entry == stop or tick_size <= 0 or tick_value <= 0:
            return 0
        from app.core.sizing import unified_size

        risk_usd = getattr(self, "_risk_per_trade_usd", None)
        risk_pct = getattr(self, "_risk_per_trade_pct", None)
        equity   = getattr(self, "_equity", None)
        # Conservative fallback: when the account risk settings aren't wired
        # through, use the legacy $200 fixed-risk default (unchanged behaviour).
        if risk_usd is None and risk_pct is None and equity is None:
            risk_usd = getattr(self, "session_risk_per_trade", 200.0)

        res = unified_size(
            entry_price=entry,
            stop_loss=stop,
            point_value=tick_value / tick_size,
            commission_per_unit=0.0,
            max_units=cap,
            risk_per_trade_usd=risk_usd,
            risk_per_trade_pct=risk_pct,
            account_equity=equity,
        )
        return max(0, res.final_size)

    def _pick_contract_size_with_micro(self, entry, stop, configured_instrument, cap):
        """Mirror of paper_trader._pick_contract_size_with_micro — sizes on
        the configured (mini) symbol; falls back to the micro variant when
        the account can't afford even one mini, with 10x the contract cap."""
        ts = TICK_SIZES.get(configured_instrument, 0.25)
        tv = TICK_VALUES.get(configured_instrument, 12.50)
        n = self._pick_contract_size(entry, stop, ts, tv, cap)
        if n >= 1:
            return n, configured_instrument, ts, tv
        micro = MINI_TO_MICRO.get(configured_instrument)
        if not micro:
            return 0, configured_instrument, ts, tv
        ts_m = TICK_SIZES.get(micro, 0.25)
        tv_m = TICK_VALUES.get(micro, 1.25)
        n_m = self._pick_contract_size(entry, stop, ts_m, tv_m, cap * 10)
        return n_m, micro, ts_m, tv_m

    async def route_external_signal(self, signal, source_signal_id=None):
        """ROUTING (#156): enter an EXTERNAL (email) signal on this live session.
        Goes through _execute_entry, which carries the Phase E auto-trade guard,
        so an ineligible user can never place. Returns (entered, reason)."""
        try:
            if not getattr(self, "_is_running", False) or getattr(self, "_kill_switch", False):
                return False, "session_not_running"
            if self._position:
                return False, "already_in_position"
            self._routed_source_signal_id = source_signal_id
            await self._execute_entry(signal)
            return (self._position is not None), ("entered" if self._position else "blocked_or_open_failed")
        except Exception as e:
            return False, f"error:{type(e).__name__}"

    async def _execute_entry(self, signal):
        # PHASE-E-GUARD: hard backstop — never auto-place a live trade unless the
        # user is fully-automated (tier_5) + signed the agreement + trading_enabled.
        from app.core.auto_trade_guard import auto_trade_allowed
        _ok, _why = await auto_trade_allowed(
            self.user_id, getattr(self, "broker_account_id", None),
            context={"instrument": self.instrument, "strategy_id": str(self.strategy_id),
                     "session_id": str(self.session_id), "kind": "live_auto_entry"})
        if not _ok:
            logger.warning(f"[LiveTrader] AUTO-TRADE BLOCKED on {self.instrument}: {_why} — not placing (audited)")
            return
        side = OrderSide.BUY if signal.signal == SignalType.LONG else OrderSide.SELL

        # Bug #11 fix: risk-based sizing rather than trusting signal.contracts
        # (which is just strategy.max_contracts). Falls back to micros when
        # the account can't afford a mini.
        strategy_cap = max(1, int(getattr(signal, "contracts", 1) or 1))
        contracts, traded_instrument, _ts, _tv = self._pick_contract_size_with_micro(
            signal.entry_price, signal.stop_loss, self.instrument, strategy_cap,
        )
        if contracts < 1:
            logger.warning(f"[LiveTrader] Rejected trade — account can't afford even 1 micro on {self.instrument} @ {signal.entry_price} (SL={signal.stop_loss})")
            return
        # If we fell back to micros, swap the traded instrument
        if traded_instrument != self.instrument:
            logger.info(f"[LiveTrader] Sized down to {traded_instrument} (account can't afford {self.instrument})")

        # Place market entry order
        entry_order = await self.broker.place_order(OrderRequest(
            instrument=traded_instrument,
            side=side,
            quantity=contracts,
            order_type=OrderType.MARKET,
        ))
        logger.info(f"[LiveTrader] Entry order placed: {entry_order.broker_order_id}")

        # Place bracket SL order
        sl_side  = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
        sl_order = await self.broker.place_order(OrderRequest(
            instrument=traded_instrument,
            side=sl_side,
            quantity=contracts,
            order_type=OrderType.STOP,
            stop_price=signal.stop_loss,
        ))

        # Place bracket TP order
        tp_order = await self.broker.place_order(OrderRequest(
            instrument=traded_instrument,
            side=sl_side,
            quantity=contracts,
            order_type=OrderType.LIMIT,
            price=signal.take_profit,
        ))

        self._position = LivePosition(
            instrument=traded_instrument,
            direction=signal.signal.value,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            contracts=contracts,
            entry_time=datetime.utcnow(),
            broker_order_id=entry_order.broker_order_id,
            sl_order_id=sl_order.broker_order_id,
            tp_order_id=tp_order.broker_order_id,
            metadata=signal.metadata,
        )
        logger.info(f"[LiveTrader] LIVE POSITION OPEN | {side.value.upper()} {signal.contracts}x {self.instrument} @ {signal.entry_price:.2f}")
        logger.info(
            f"[paper-runner] sid={self.session_id} ENTERED inst={traded_instrument} "
            f"dir={signal.signal.value} entry={signal.entry_price:.2f} contracts={contracts} mode=live"
        )

    async def _cancel_bracket_and_close(self, price: float, reason: ExitReason):
        p = self._position
        if not p:
            return
        # Cancel remaining bracket orders
        if p.sl_order_id:
            await self.broker.cancel_order(p.sl_order_id)
        if p.tp_order_id:
            await self.broker.cancel_order(p.tp_order_id)

        # Place closing market order
        close_side = OrderSide.SELL if p.direction == "long" else OrderSide.BUY
        await self.broker.place_order(OrderRequest(
            instrument=self.instrument,
            side=close_side,
            quantity=p.contracts,
            order_type=OrderType.MARKET,
        ))

        tick_size  = TICK_SIZES.get(self.instrument, 0.25)
        tick_value = TICK_VALUES.get(self.instrument, 12.50)
        if p.direction == "long":
            pnl_ticks = (price - p.entry_price) / tick_size
        else:
            pnl_ticks = (p.entry_price - price) / tick_size

        net_pnl = (pnl_ticks * tick_value * p.contracts) - (self.commission * 2 * p.contracts)
        self._daily_pnl    += net_pnl
        self._daily_trades += 1
        self.strategy.record_trade_result(net_pnl)

        logger.info(f"[LiveTrader] POSITION CLOSED | {reason.value} @ {price:.2f} | Net PnL: ${net_pnl:,.2f}")
        self._position = None

        # Check if daily loss limit hit
        if self.strategy.config.max_daily_loss and self._daily_pnl <= -abs(self.strategy.config.max_daily_loss):
            self.trigger_kill_switch()
