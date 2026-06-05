"""Options live trading — real-money counterpart to OptionsPaperTrader.

Same external contract: `on_signal()` to open a position, `on_spot_tick()`
to mark and exit. The difference is the trade actually fills through
TradierBroker against a real (or sandbox) brokerage account, and prices
come from Tradier's live quote endpoint (with greeks, IV, NBBO bid/ask)
instead of Black-Scholes synthetics.

Position-sizing math is identical to paper:
    risk_dollars = equity * risk_per_trade_pct / 100
    loss_per_contract = entry_premium * 100 * stop_loss_pct / 100 + 2 * commission
    contracts = floor(risk_dollars / loss_per_contract)

Lifecycle of one trade:
    1. signal arrives → pull live quote for picked contract → size position
    2. place buy_to_open via broker → poll until filled (or timeout/reject)
    3. record entry fill price (real, not modelled)
    4. on each spot tick: pull live mid for the contract; if mark <= stop_premium
       or mark >= target_premium, place sell_to_close
    5. wait for close fill, record P&L

Anything that goes wrong (rejection, partial fill, broker outage) is logged
and the position is marked errored — we don't blind-flip-flop orders.
"""
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, date, timezone, timedelta as _td
from typing import Optional
from loguru import logger

from app.engines.options.polygon_options import OptionContract
from app.engines.options.strike_picker import pick_strike
from app.engines.live_trading.tradier import TradierBroker, _is_option_symbol
from app.engines.live_trading.broker_base import OrderRequest, OrderSide, OrderType, OrderStatus


@dataclass
class OptionLivePosition:
    underlying: str
    direction: str
    contract: OptionContract
    contracts: int
    entry_premium: float
    entry_spot: float
    entry_time: datetime
    stop_premium: float
    target_premium: float
    open_order_id: str
    close_order_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class OptionLiveResult:
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
    gross_pnl: float
    commission: float
    net_pnl: float
    is_winner: bool
    exit_reason: str
    metadata: dict = field(default_factory=dict)


class OptionsLiveTrader:
    """Live options trader. Holds at most one open position at a time per
    underlying — same constraint as the paper trader, mirrors futures live."""

    def __init__(self, underlying: str, broker: TradierBroker,
                 chain: list[OptionContract],
                 starting_balance: float = 10_000.0,
                 risk_per_trade_pct: float = 1.5,
                 commission_per_contract: float = 0.65,
                 stop_loss_premium_pct: float = 50.0,
                 target_premium_pct: float = 100.0,
                 daily_loss_pct_kill: float = 5.0,
                 fill_timeout_sec: int = 30,
                 session_id: Optional[str] = None,
                 user_id: Optional[str] = None,
                 strategy_id: Optional[str] = None):
        self.underlying = underlying.upper()
        self.broker     = broker
        self.chain      = chain
        self.commission = commission_per_contract
        self.session_id, self.user_id, self.strategy_id = session_id, user_id, strategy_id

        # Risk state
        self._starting_balance     = float(starting_balance)
        self._equity               = float(starting_balance)
        self._risk_per_trade_pct   = float(risk_per_trade_pct)
        self._stop_loss_pct        = float(stop_loss_premium_pct)
        self._target_pct           = float(target_premium_pct)
        self._daily_loss_pct_kill  = float(daily_loss_pct_kill)
        self._fill_timeout         = int(fill_timeout_sec)

        self._position: Optional[OptionLivePosition] = None
        self._completed: list[OptionLiveResult] = []
        self._daily_pnl: float = 0.0
        self._current_date: Optional[date] = None
        self._kill_switch: bool = False
        self._is_running: bool = False
        # Wall-clock instant trading went live. Signals whose source bar closed
        # before this are catch-up/replayed history and must never open a
        # real-money position. Set in start().
        self._session_started_at: Optional[datetime] = None

    # ── Sizing — same math as paper ─────────────────────────────────────────

    def _size_position(self, entry_premium: float) -> int:
        if entry_premium <= 0:
            return 0
        loss_per = entry_premium * 100 * (self._stop_loss_pct / 100.0) + (self.commission * 2)
        risk_dollars = self._equity * (self._risk_per_trade_pct / 100.0)
        if loss_per <= 0 or risk_dollars <= 0:
            return 0
        return max(0, int(risk_dollars // loss_per))

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def start(self):
        if not self.broker.is_connected:
            ok = await self.broker.connect()
            if not ok:
                raise RuntimeError("[OptionsLive] broker.connect() failed — refusing to start")
        self._is_running = True
        self._session_started_at = datetime.now(timezone.utc)
        logger.info(f"[OptionsLive] Started | {self.underlying} | account={self.broker.account_id} | "
                     f"equity=${self._equity:,.0f} | session_started_at={self._session_started_at.isoformat()}")

    async def stop(self):
        self._is_running = False
        logger.info(f"[OptionsLive] Stopped | {self.underlying} | trades={len(self._completed)} | "
                     f"day=${self._daily_pnl:,.2f}")

    # ── Helpers for live quote pulls ────────────────────────────────────────

    async def _live_mid(self, contract_ticker: str) -> Optional[float]:
        """Pull a fresh mid-quote for one option from Tradier."""
        # Tradier wants the OCC ticker without the `O:` prefix
        sym = contract_ticker[2:] if contract_ticker.startswith("O:") else contract_ticker
        quotes = await self.broker.get_quotes([sym])
        q = quotes.get(sym)
        if not q:
            return None
        bid = q.get("bid")
        ask = q.get("ask")
        if bid and ask and bid > 0 and ask > 0:
            return (float(bid) + float(ask)) / 2.0
        last = q.get("last")
        return float(last) if last else None

    async def _wait_for_fill(self, order_id: str) -> Optional[float]:
        """Poll the broker until the order fills or the timeout trips. Returns
        the average fill price or None on timeout/rejection."""
        for _ in range(self._fill_timeout):
            status = await self.broker.get_order_status(order_id)
            if status.status == OrderStatus.FILLED:
                return status.filled_price
            if status.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
                logger.error(f"[OptionsLive] order {order_id} ended: {status.status.value} ({status.message})")
                return None
            await asyncio.sleep(1)
        logger.error(f"[OptionsLive] order {order_id} not filled within {self._fill_timeout}s — cancelling")
        await self.broker.cancel_order(order_id)
        return None

    # ── Signal → live entry ─────────────────────────────────────────────────

    async def on_signal(self, side: str, spot: float, today: date,
                         delta_band: tuple[float, float] = (0.30, 0.50),
                         dte_band: tuple[int, int] = (30, 60),
                         prefer_itm: bool = False,
                         spread_width: Optional[int] = None,
                         default_iv: float = 0.30,
                         bar_ts=None) -> Optional[OptionLivePosition]:
        if not self._is_running or self._kill_switch or self._position:
            return None

        # Never OPEN a real-money position on a signal whose source bar closed
        # before this session went live (catch-up/replayed history seeds the
        # indicator only). belt-and-suspenders consistent with paper traders.
        if self._session_started_at is not None and bar_ts is not None:
            try:
                import pandas as _pd
                _bts = _pd.Timestamp(bar_ts)
                if _bts.tzinfo is None:
                    _bts = _bts.tz_localize("UTC")
                if _bts.to_pydatetime() < (self._session_started_at - _td(seconds=90)):
                    logger.info(f"[OptionsLive] SKIP entry — bar ts={_bts.isoformat()} predates session_start={self._session_started_at.isoformat()} (catch-up bar, seed-only)")
                    return None
            except Exception:
                pass

        option_side = "call" if side in ("long", "bullish") else "put"
        pick = pick_strike(
            self.chain, spot=spot, today=today, side=option_side,
            delta_min=delta_band[0], delta_max=delta_band[1],
            dte_min=dte_band[0], dte_max=dte_band[1],
            default_iv=default_iv, prefer_itm=prefer_itm,
            spread_width=spread_width,
        )
        if pick is None or pick.band_missed:
            logger.warning(f"[OptionsLive] SKIP — no contract in band for {self.underlying} {option_side}")
            return None

        # Live mid-quote for the picked contract
        mid = await self._live_mid(pick.long.ticker)
        if not mid or mid < 0.05:
            logger.warning(f"[OptionsLive] SKIP — no live quote for {pick.long.ticker} (mid={mid})")
            return None

        contracts = self._size_position(mid)
        if contracts < 1:
            logger.warning(f"[OptionsLive] SKIP — account can't afford 1 contract (mid=${mid:.2f}, "
                            f"equity ${self._equity:,.0f})")
            return None

        # Place the buy_to_open order — limit at mid, day, broker fills inside the spread
        client_id = f"o_open_{uuid.uuid4().hex[:8]}"
        req = OrderRequest(
            instrument=pick.long.ticker, side=OrderSide.BUY, quantity=contracts,
            order_type=OrderType.LIMIT, price=round(mid, 2),
            time_in_force="day", client_order_id=client_id,
        )
        order = await self.broker.place_order(req)
        if order.status == OrderStatus.REJECTED or not order.broker_order_id:
            logger.error(f"[OptionsLive] open order rejected: {order.message}")
            return None

        fill_price = await self._wait_for_fill(order.broker_order_id)
        if fill_price is None:
            return None

        stop_premium   = fill_price * (1 - self._stop_loss_pct / 100.0)
        target_premium = fill_price * (1 + self._target_pct    / 100.0)

        self._position = OptionLivePosition(
            underlying=self.underlying, direction=option_side,
            contract=pick.long, contracts=contracts, entry_premium=fill_price,
            entry_spot=spot, entry_time=datetime.now(timezone.utc),
            stop_premium=stop_premium, target_premium=target_premium,
            open_order_id=order.broker_order_id,
            metadata={
                "delta_at_entry": pick.actual_delta,
                "dte_at_entry":   pick.days_to_expiration,
                "pick_reason":    pick.reason,
                "broker":         "tradier",
                "broker_account": self.broker.account_id,
            },
        )
        logger.info(f"[OptionsLive] OPEN {option_side.upper()} {contracts}x "
                     f"{pick.long.ticker} @ ${fill_price:.2f} | stop ${stop_premium:.2f} "
                     f"target ${target_premium:.2f}")
        return self._position

    # ── Live mark + exit ────────────────────────────────────────────────────

    async def on_spot_tick(self, spot: float, now: datetime) -> Optional[OptionLiveResult]:
        if not self._position or self._kill_switch:
            return None
        p = self._position

        if self._current_date != now.date():
            self._current_date = now.date()
            self._daily_pnl = 0.0

        # Pull the live mid for our contract
        mark = await self._live_mid(p.contract.ticker)
        if mark is None:
            return None

        if mark <= p.stop_premium:
            return await self._close_position(spot, now, "stop")
        if mark >= p.target_premium:
            return await self._close_position(spot, now, "target")
        # Expiration fallback — close on the day-of (or earlier when DTE = 0)
        dte = (p.contract.expiration - now.date()).days
        if dte <= 0:
            return await self._close_position(spot, now, "expiration")
        return None

    async def _close_position(self, spot: float, now: datetime, reason: str) -> Optional[OptionLiveResult]:
        p = self._position
        if not p:
            return None

        # Re-fetch mid for the closing leg
        mid = await self._live_mid(p.contract.ticker)
        if mid is None:
            mid = p.target_premium if reason == "target" else p.stop_premium

        client_id = f"o_close_{uuid.uuid4().hex[:8]}|close"
        req = OrderRequest(
            instrument=p.contract.ticker, side=OrderSide.SELL, quantity=p.contracts,
            order_type=OrderType.LIMIT, price=round(mid, 2),
            time_in_force="day", client_order_id=client_id,
        )
        order = await self.broker.place_order(req)
        if order.status == OrderStatus.REJECTED or not order.broker_order_id:
            logger.error(f"[OptionsLive] close order REJECTED: {order.message} — position left open!")
            return None

        fill = await self._wait_for_fill(order.broker_order_id)
        if fill is None:
            # Couldn't close — leave the position open and let the next tick try again
            return None

        gross = (fill - p.entry_premium) * p.contracts * 100
        commission = self.commission * p.contracts * 2
        net = gross - commission

        result = OptionLiveResult(
            underlying=p.underlying, contract_ticker=p.contract.ticker,
            direction=p.direction, contracts=p.contracts,
            entry_premium=p.entry_premium, exit_premium=fill,
            entry_spot=p.entry_spot, exit_spot=spot,
            entry_time=p.entry_time, exit_time=now,
            gross_pnl=gross, commission=commission, net_pnl=net,
            is_winner=(net > 0), exit_reason=reason,
            metadata={**p.metadata, "close_order_id": order.broker_order_id},
        )
        self._completed.append(result)
        self._daily_pnl += net
        self._equity    += net
        logger.info(f"[OptionsLive] CLOSE {reason} @ ${fill:.2f} | Net ${net:,.2f} | "
                     f"equity ${self._equity:,.0f}")
        self._position = None

        loss_cap = self._starting_balance * (self._daily_loss_pct_kill / 100.0)
        if loss_cap > 0 and self._daily_pnl <= -loss_cap:
            logger.error(f"[OptionsLive] DAILY LOSS LIMIT — kill switch tripped")
            self._kill_switch = True

        return result

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
