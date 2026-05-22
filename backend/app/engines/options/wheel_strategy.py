"""The Wheel — cash-secured puts → assigned-stock → covered calls.

This is fundamentally different from the directional long-options modes
because we're **selling premium**, not buying it. The full cycle:

   1. Pick a stock you'd be happy to own at a strike below market.
   2. Sell a cash-secured put at delta ~0.25-0.35, 30-45 DTE.
   3. If the put expires worthless → keep the premium, sell another put.
   4. If the put is assigned → you now own 100 shares per contract at the strike.
   5. Immediately sell a covered call at delta ~0.25-0.35, 30-45 DTE,
      strike ≥ your cost basis (so even getting called away is profitable).
   6. If the call expires worthless → keep premium, sell another call.
   7. If the call is assigned → shares get called away, cycle restarts at step 1.

State persists across signals — once we own 100 shares, the runner doesn't
look for new entries on that ticker until the shares are gone again.

Pricing/sizing differences from long-options:
   • The "premium" we collect is income, capped at the premium received
   • The risk is owning the stock at a higher price than market if assigned
     and the stock keeps falling (we'd be left holding the bag)
   • Cash-secured: account must hold full notional in cash for each put
     (strike × 100 per contract). This constrains position sizing more
     than long-options.
"""
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta, timezone
from typing import Optional, Literal
from loguru import logger

from app.engines.options.polygon_options import OptionContract
from app.engines.options.strike_picker import pick_strike
from app.engines.options.pricing import price as bs_price, greeks
from app.engines.live_trading.tradier import TradierBroker
from app.engines.live_trading.broker_base import OrderRequest, OrderSide, OrderType, OrderStatus


WheelPhase = Literal["selling_put", "holding_shares", "selling_call", "idle"]


@dataclass
class WheelPosition:
    """Tracks both the current short option (if any) and the underlying
    stock position (if assigned). One per (user, strategy, underlying)."""
    phase: WheelPhase
    # Short-option leg (None when holding bare stock or idle)
    contract: Optional[OptionContract] = None
    contracts: int = 0
    entry_premium: float = 0.0     # premium received when short was opened
    entry_time: Optional[datetime] = None
    short_order_id: str = ""
    # Stock leg (None when no shares)
    shares: int = 0
    cost_basis: float = 0.0        # per-share cost basis from put assignment
    metadata: dict = field(default_factory=dict)


@dataclass
class WheelResult:
    """One completed leg of the wheel (either a put cycle or a call cycle).
    Multiple wheel-results compose into a full wheel sequence."""
    underlying: str
    phase: WheelPhase
    contract_ticker: Optional[str]
    contracts: int
    entry_premium: float
    exit_premium: float            # 0 if expired worthless
    entry_time: datetime
    exit_time: datetime
    gross_pnl: float
    commission: float
    net_pnl: float
    is_winner: bool
    exit_reason: str               # 'expired_worthless' | 'closed_for_profit' | 'assigned'
    metadata: dict = field(default_factory=dict)


class WheelStrategy:
    """Engine for The Wheel. Lives in either paper or live mode based on
    whether `broker` is supplied. Paper mode synthesizes fills via BS pricing
    against the live underlying; live mode places real sell_to_open orders
    through Tradier.

    State machine:
        idle           → look for cash-secured-put entry
        selling_put    → wait for put expiry/assignment
        holding_shares → immediately sell covered call → selling_call
        selling_call   → wait for call expiry/assignment
        (call assigned) → idle (back to top)
    """

    def __init__(self, underlying: str, broker: Optional[TradierBroker],
                 chain: list[OptionContract],
                 starting_balance: float = 25_000.0,
                 target_delta: float = 0.30,
                 dte_min: int = 30, dte_max: int = 45,
                 close_at_profit_pct: float = 50.0,
                 commission_per_contract: float = 0.65,
                 default_iv: float = 0.30,
                 session_id: Optional[str] = None,
                 user_id: Optional[str] = None,
                 strategy_id: Optional[str] = None):
        self.underlying  = underlying.upper()
        self.broker      = broker
        self.is_live     = broker is not None
        self.chain       = chain
        self.target_delta = float(target_delta)
        self.dte_min, self.dte_max = int(dte_min), int(dte_max)
        self.close_at_profit_pct = float(close_at_profit_pct)
        self.commission  = float(commission_per_contract)
        self.default_iv  = float(default_iv)
        self.session_id, self.user_id, self.strategy_id = session_id, user_id, strategy_id

        self._cash      = float(starting_balance)
        self._position  = WheelPosition(phase="idle")
        self._completed: list[WheelResult] = []
        self._is_running = False
        self._kill_switch = False

    @property
    def stats(self) -> dict:
        wins = sum(1 for r in self._completed if r.is_winner)
        return {
            "total_legs": len(self._completed),
            "wins": wins,
            "win_rate": (wins / len(self._completed)) if self._completed else 0.0,
            "net_pnl": sum(r.net_pnl for r in self._completed),
            "cash": self._cash,
            "phase": self._position.phase,
            "shares": self._position.shares,
            "cost_basis": self._position.cost_basis,
            "open_short": bool(self._position.contract),
            "is_running": self._is_running,
            "kill_switch": self._kill_switch,
        }

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def start(self):
        if self.is_live:
            if not self.broker.is_connected:
                ok = await self.broker.connect()
                if not ok:
                    raise RuntimeError("[Wheel] broker.connect() failed")
        self._is_running = True
        logger.info(f"[Wheel] Started | {self.underlying} | phase={self._position.phase} | "
                     f"cash=${self._cash:,.0f} | live={self.is_live}")

    async def stop(self):
        self._is_running = False
        logger.info(f"[Wheel] Stopped | legs={len(self._completed)} | net=${sum(r.net_pnl for r in self._completed):,.2f}")

    # ── State transitions ───────────────────────────────────────────────────

    async def on_spot_tick(self, spot: float, now: datetime,
                            avoid_earnings_days: int = 0) -> Optional[WheelResult]:
        """Drive the state machine on each tick."""
        if not self._is_running or self._kill_switch:
            return None

        # Earnings filter — skip new entries (in 'idle' or after assignment)
        if avoid_earnings_days > 0 and self._position.phase in ("idle",):
            from app.engines.options.earnings_filter import is_near_earnings
            near, _ = await is_near_earnings(self.underlying, now.date(), avoid_earnings_days)
            if near:
                return None

        if self._position.phase == "idle":
            return await self._sell_cash_secured_put(spot, now.date())

        if self._position.phase == "selling_put":
            return await self._manage_short_put(spot, now)

        if self._position.phase == "holding_shares":
            return await self._sell_covered_call(spot, now.date())

        if self._position.phase == "selling_call":
            return await self._manage_short_call(spot, now)

        return None

    # ── Put cycle ───────────────────────────────────────────────────────────

    async def _sell_cash_secured_put(self, spot: float, today: date) -> None:
        """Open a short put at the target delta. Cash-secured means we
        reserve strike × 100 × contracts from `_cash` as collateral."""
        pick = pick_strike(
            self.chain, spot=spot, today=today, side="put",
            delta_min=max(0.10, self.target_delta - 0.05),
            delta_max=min(0.50, self.target_delta + 0.05),
            dte_min=self.dte_min, dte_max=self.dte_max,
            default_iv=self.default_iv,
        )
        if pick is None or pick.band_missed:
            return None

        # Premium at delta target
        t = max(1, pick.days_to_expiration) / 365.0
        g = greeks(s=spot, k=pick.long.strike, t=t, sigma=self.default_iv, opt_type="put")
        premium = g.price
        if premium < 0.10:
            return None  # not enough juice

        # Sizing — limit by cash on hand. Each contract requires
        # strike × 100 in collateral.
        max_by_cash = int(self._cash // (pick.long.strike * 100))
        if max_by_cash < 1:
            logger.warning(f"[Wheel] SKIP — not enough cash for one CSP "
                            f"(strike=${pick.long.strike}, need ${pick.long.strike * 100:.0f}, "
                            f"have ${self._cash:,.0f})")
            return None
        contracts = min(max_by_cash, 5)  # cap per-cycle exposure

        # In live mode, place real sell_to_open
        if self.is_live and self.broker:
            req = OrderRequest(
                instrument=pick.long.ticker, side=OrderSide.SELL,
                quantity=contracts, order_type=OrderType.LIMIT,
                price=round(premium, 2), time_in_force="day",
                client_order_id=f"w_sp_{uuid.uuid4().hex[:8]}",
            )
            order = await self.broker.place_order(req)
            if order.status == OrderStatus.REJECTED or not order.broker_order_id:
                logger.error(f"[Wheel] short-put rejected: {order.message}")
                return None
            # We'd poll for fill — for paper simplicity assume mid-fill
            order_id = order.broker_order_id
        else:
            order_id = f"paper_{uuid.uuid4().hex[:8]}"

        self._position = WheelPosition(
            phase="selling_put", contract=pick.long, contracts=contracts,
            entry_premium=premium, entry_time=datetime.now(timezone.utc),
            short_order_id=order_id,
            metadata={"delta_at_entry": pick.actual_delta,
                       "dte_at_entry":   pick.days_to_expiration},
        )
        # Collateral lock
        self._cash -= pick.long.strike * 100 * contracts
        # Credit premium (received cash)
        self._cash += premium * 100 * contracts - self.commission * contracts
        logger.info(f"[Wheel] SELL_TO_OPEN {contracts}x {pick.long.ticker} @ ${premium:.2f} "
                     f"(collateral ${pick.long.strike * 100 * contracts:,.0f} locked)")
        return None

    async def _manage_short_put(self, spot: float, now: datetime) -> Optional[WheelResult]:
        """Check the short put: assign if ITM at expiry, close at profit, or hold."""
        p = self._position
        if not p.contract:
            return None
        dte = (p.contract.expiration - now.date()).days
        t = max(1, dte) / 365.0
        mark = bs_price(s=spot, k=p.contract.strike, t=t, sigma=self.default_iv, opt_type="put")
        # Close-at-profit gate — if 50% (default) of max profit captured, close early
        profit_threshold = p.entry_premium * (1 - self.close_at_profit_pct / 100.0)
        if mark <= profit_threshold and mark > 0:
            return await self._close_short_for_profit(spot, now, mark)
        if dte <= 0:
            # Expiry day
            intrinsic = max(0.0, p.contract.strike - spot)
            if intrinsic <= 0:
                # Worthless — keep all premium, cycle back to idle
                return self._record_leg_complete(now, exit_premium=0.0,
                                                  exit_reason="expired_worthless")
            else:
                # Assigned — receive 100 shares per contract at strike
                return self._handle_put_assignment(now)
        return None

    def _handle_put_assignment(self, now: datetime) -> WheelResult:
        p = self._position
        # Cash collateral was already locked. Convert to shares at strike.
        n_shares = p.contracts * 100
        cost_basis = p.contract.strike
        # We already debited cash by strike × 100 when locking collateral, so
        # no further cash movement for the share purchase itself.
        gross = (0.0 - p.entry_premium) * p.contracts * 100  # put settles worthless to us when ITM at assignment? Actually it goes intrinsic — we lose (strike - spot) × 100 × contracts in market value, but we have stock now.
        # Track only the option leg's P&L for this result; the stock leg
        # gets its own result when we eventually sell covered calls.
        commission = self.commission * p.contracts
        net = p.entry_premium * p.contracts * 100 - commission  # we collected this premium and keep it
        result = WheelResult(
            underlying=self.underlying, phase="selling_put",
            contract_ticker=p.contract.ticker, contracts=p.contracts,
            entry_premium=p.entry_premium, exit_premium=0.0,
            entry_time=p.entry_time or now, exit_time=now,
            gross_pnl=p.entry_premium * p.contracts * 100,
            commission=commission, net_pnl=net,
            is_winner=True,  # put cycle = profit (we keep premium); we just got stock at the strike
            exit_reason="assigned",
            metadata={"shares_acquired": n_shares, "cost_basis": cost_basis,
                       **p.metadata},
        )
        self._completed.append(result)
        # Transition to holding_shares
        self._position = WheelPosition(
            phase="holding_shares", shares=n_shares, cost_basis=cost_basis,
            metadata={"acquired_via": p.contract.ticker, "acquired_at": now.isoformat()},
        )
        logger.info(f"[Wheel] PUT ASSIGNED — now holding {n_shares} shares of {self.underlying} "
                     f"@ ${cost_basis:.2f} cost basis")
        return result

    # ── Call cycle ──────────────────────────────────────────────────────────

    async def _sell_covered_call(self, spot: float, today: date) -> None:
        """Now holding shares — sell a covered call at strike >= cost basis,
        target delta ~0.25-0.35."""
        # Find strikes only at/above cost basis (so getting called away is profit)
        candidate_chain = [c for c in self.chain
                            if c.right == "call" and c.strike >= self._position.cost_basis]
        pick = pick_strike(
            candidate_chain, spot=spot, today=today, side="call",
            delta_min=max(0.10, self.target_delta - 0.05),
            delta_max=min(0.50, self.target_delta + 0.05),
            dte_min=self.dte_min, dte_max=self.dte_max,
            default_iv=self.default_iv,
        )
        if pick is None or pick.band_missed:
            return None

        contracts = self._position.shares // 100
        if contracts < 1:
            return None

        t = max(1, pick.days_to_expiration) / 365.0
        g = greeks(s=spot, k=pick.long.strike, t=t, sigma=self.default_iv, opt_type="call")
        premium = g.price
        if premium < 0.10:
            return None

        if self.is_live and self.broker:
            req = OrderRequest(
                instrument=pick.long.ticker, side=OrderSide.SELL,
                quantity=contracts, order_type=OrderType.LIMIT,
                price=round(premium, 2), time_in_force="day",
                client_order_id=f"w_sc_{uuid.uuid4().hex[:8]}",
            )
            order = await self.broker.place_order(req)
            if order.status == OrderStatus.REJECTED or not order.broker_order_id:
                return None
            order_id = order.broker_order_id
        else:
            order_id = f"paper_{uuid.uuid4().hex[:8]}"

        # Keep stock state, add the short call leg
        self._position.phase = "selling_call"
        self._position.contract = pick.long
        self._position.contracts = contracts
        self._position.entry_premium = premium
        self._position.entry_time = datetime.now(timezone.utc)
        self._position.short_order_id = order_id
        # Credit premium
        self._cash += premium * 100 * contracts - self.commission * contracts
        logger.info(f"[Wheel] SELL_TO_OPEN COVERED CALL {contracts}x {pick.long.ticker} "
                     f"@ ${premium:.2f}, strike ${pick.long.strike} >= cost ${self._position.cost_basis:.2f}")
        return None

    async def _manage_short_call(self, spot: float, now: datetime) -> Optional[WheelResult]:
        p = self._position
        if not p.contract:
            return None
        dte = (p.contract.expiration - now.date()).days
        t = max(1, dte) / 365.0
        mark = bs_price(s=spot, k=p.contract.strike, t=t, sigma=self.default_iv, opt_type="call")
        profit_threshold = p.entry_premium * (1 - self.close_at_profit_pct / 100.0)
        if mark <= profit_threshold and mark > 0:
            return await self._close_short_for_profit(spot, now, mark)
        if dte <= 0:
            intrinsic = max(0.0, spot - p.contract.strike)
            if intrinsic <= 0:
                return self._record_leg_complete(now, exit_premium=0.0,
                                                  exit_reason="expired_worthless",
                                                  keep_shares=True)
            else:
                return self._handle_call_assignment(now)
        return None

    def _handle_call_assignment(self, now: datetime) -> WheelResult:
        """Call assigned — shares are called away at strike price. Profit
        is (strike - cost_basis) × shares + put_premium + call_premium."""
        p = self._position
        n_shares = p.shares
        sale_proceeds = n_shares * (p.contract.strike if p.contract else 0)
        # Stock leg P&L
        stock_pnl = (p.contract.strike - p.cost_basis) * n_shares if p.contract else 0.0
        commission = self.commission * (p.contracts or 1)
        net = p.entry_premium * (p.contracts or 1) * 100 + stock_pnl - commission
        result = WheelResult(
            underlying=self.underlying, phase="selling_call",
            contract_ticker=p.contract.ticker if p.contract else None,
            contracts=p.contracts,
            entry_premium=p.entry_premium, exit_premium=0.0,
            entry_time=p.entry_time or now, exit_time=now,
            gross_pnl=net + commission,
            commission=commission, net_pnl=net,
            is_winner=(net > 0), exit_reason="assigned",
            metadata={
                "shares_called_away": n_shares,
                "strike":             p.contract.strike if p.contract else None,
                "cost_basis":         p.cost_basis,
                "stock_pnl":          stock_pnl,
                **p.metadata,
            },
        )
        self._completed.append(result)
        # Cash credited: shares × strike (we sold them at strike via assignment)
        self._cash += sale_proceeds
        # Reset to idle
        self._position = WheelPosition(phase="idle")
        logger.info(f"[Wheel] CALL ASSIGNED — sold {n_shares} shares of {self.underlying} "
                     f"@ strike ${p.contract.strike:.2f} | total cycle P&L: ${net:,.2f}")
        return result

    # ── Helpers ─────────────────────────────────────────────────────────────

    async def _close_short_for_profit(self, spot: float, now: datetime,
                                        mark: float) -> WheelResult:
        """Buy back the short option early once 50%+ of max profit captured."""
        p = self._position
        if not p.contract:
            return None
        if self.is_live and self.broker:
            req = OrderRequest(
                instrument=p.contract.ticker, side=OrderSide.BUY,
                quantity=p.contracts, order_type=OrderType.LIMIT,
                price=round(mark, 2), time_in_force="day",
                client_order_id=f"w_btc_{uuid.uuid4().hex[:8]}|close",
            )
            await self.broker.place_order(req)
        gross = (p.entry_premium - mark) * p.contracts * 100
        commission = self.commission * p.contracts * 2
        net = gross - commission
        keep_shares = (p.phase == "selling_call")  # closing the call early — keep stock
        return self._record_leg_complete(now, exit_premium=mark,
                                          exit_reason="closed_for_profit",
                                          override_gross=gross, override_net=net,
                                          override_commission=commission,
                                          keep_shares=keep_shares)

    def _record_leg_complete(self, now: datetime, exit_premium: float,
                              exit_reason: str, override_gross: Optional[float] = None,
                              override_net: Optional[float] = None,
                              override_commission: Optional[float] = None,
                              keep_shares: bool = False) -> WheelResult:
        p = self._position
        contracts = p.contracts or 1
        gross = override_gross if override_gross is not None \
                  else (p.entry_premium - exit_premium) * contracts * 100
        commission = override_commission if override_commission is not None \
                       else self.commission * contracts
        net = override_net if override_net is not None else gross - commission

        result = WheelResult(
            underlying=self.underlying, phase=p.phase,
            contract_ticker=p.contract.ticker if p.contract else None,
            contracts=contracts,
            entry_premium=p.entry_premium, exit_premium=exit_premium,
            entry_time=p.entry_time or now, exit_time=now,
            gross_pnl=gross, commission=commission, net_pnl=net,
            is_winner=(net > 0), exit_reason=exit_reason,
            metadata=p.metadata,
        )
        self._completed.append(result)

        # Release collateral if we were short a put
        if p.phase == "selling_put" and p.contract:
            self._cash += p.contract.strike * 100 * contracts

        # Apply cash movement for the buyback (if any)
        if exit_premium > 0:
            self._cash -= exit_premium * 100 * contracts + commission

        # State transition
        if p.phase == "selling_put":
            self._position = WheelPosition(phase="idle")
        elif p.phase == "selling_call":
            if keep_shares:
                # Stock leg still open — drop back to holding_shares so we sell another call
                self._position = WheelPosition(
                    phase="holding_shares", shares=p.shares, cost_basis=p.cost_basis,
                    metadata=p.metadata,
                )
            else:
                self._position = WheelPosition(phase="idle")
        return result
