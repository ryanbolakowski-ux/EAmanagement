"""Phase E — hard execution-time backstop. No UNATTENDED automatic live trade
may be placed unless the user is on the fully-automated package (tier_5), has
accepted the Fully Automated Trading Agreement, AND the broker account has
trading_enabled. Every BLOCK is audited (EVENT_AUTO_TRADE_BLOCKED).

This is the enforcement of "non-tier_5 never auto-trades": the agreement +
email-code checks (Phase D) gate ACTIVATION; this guards the actual place_order.
Fail-CLOSED — if eligibility can't be verified, the trade is NOT placed.
"""
import logging

_log = logging.getLogger("theta.auto_trade_guard")


async def auto_trade_allowed(user_id, broker_account_id, *, context: dict | None = None) -> tuple[bool, str]:
    """Return (allowed, reason). Audits a BLOCK. Fail-closed on any error."""
    try:
        from app.database import async_session_factory
        from sqlalchemy import select
        from app.models.user import User, BrokerAccount
        from app.core.packages import is_fully_automated_tier, tier_value
        from app.api.routes.legal import has_current_ack
        from app.api.routes.security import audit_log, EVENT_AUTO_TRADE_BLOCKED

        async with async_session_factory() as db:
            user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none() if user_id else None
            acct = (await db.execute(select(BrokerAccount).where(BrokerAccount.id == broker_account_id))).scalar_one_or_none() if broker_account_id else None

            reason = None
            if user is None:
                reason = "user_not_found"
            elif not is_fully_automated_tier(user):
                reason = f"not_fully_automated_package(tier={tier_value(user)})"
            elif not await has_current_ack(db, user.id, "fully_automated_trading"):
                reason = "fully_automated_trading_agreement_not_accepted"
            elif broker_account_id is None or acct is None:
                # Fail-CLOSED: an unattended live trade must target a real,
                # trading-enabled account; a missing/unknown id is a BLOCK.
                reason = "no_broker_account"
            elif not getattr(acct, "trading_enabled", False):
                reason = "account_trading_enabled_off"

            if reason:
                try:
                    await audit_log(db, user_id, EVENT_AUTO_TRADE_BLOCKED,
                                    {"reason": reason, **(context or {})}, None)
                    await db.commit()
                except Exception:
                    pass
                _log.warning(f"[auto-trade-guard] BLOCKED user={user_id} acct={broker_account_id} reason={reason}")
                return False, reason
            return True, "ok"
    except Exception as _e:
        # Fail-CLOSED for live money: never auto-place when we can't verify.
        _log.warning(f"[auto-trade-guard] verify error: {_e}; blocking conservatively")
        return False, f"guard_error:{type(_e).__name__}"
