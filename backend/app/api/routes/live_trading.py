from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models.user import User, SubscriptionTier
from app.models.strategy import Strategy
from app.models.user import BrokerAccount
from app.models.trade import TradeSession, TradingMode, Trade, TradeStatus
from app.api.routes.legal import require_current_ack
from app.core.auth import get_current_user, require_live_trading
from app.core.security import encrypt_credentials, decrypt_credentials
from app.api.routes.kyc import require_kyc_verified

router = APIRouter()


async def _account_daily_pnl(db: AsyncSession, account_id) -> float:
    """Sum of net_pnl for trades that closed today (UTC) on this broker account."""
    from datetime import datetime as _dt, timezone as _tz
    from sqlalchemy import func as _func, cast, Date
    today_start = _dt.now(_tz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(_func.coalesce(_func.sum(Trade.net_pnl), 0.0))
        .where(
            Trade.broker_account_id == account_id,
            Trade.status == TradeStatus.CLOSED,
            Trade.exit_time >= today_start,
        )
    )
    return float(result.scalar() or 0.0)


def _to_response(a, daily_pnl: float = 0.0) -> "BrokerAccountResponse":
    daily_limit = None
    if a.profit_target is not None and a.consistency_pct is not None:
        daily_limit = round(float(a.profit_target) * float(a.consistency_pct) / 100.0, 2)
    return BrokerAccountResponse(
        id=str(a.id), account_name=a.account_name, broker=a.broker,
        is_demo=a.is_demo, sandbox_mode=getattr(a, "sandbox_mode", True), is_active=a.is_active,
        trading_enabled=a.trading_enabled,
        profit_target=a.profit_target,
        consistency_pct=a.consistency_pct,
        consistency_locked_at=a.consistency_locked_at.isoformat() if a.consistency_locked_at else None,
        daily_pnl=round(daily_pnl, 2),
        daily_limit=daily_limit,
        created_at=a.created_at.isoformat(),
    )


class AddBrokerAccountRequest(BaseModel):
    account_name: str
    broker: str = "tradovate"
    is_demo: bool = True
    credentials: dict  # {"username": ..., "password": ..., "app_id": ..., "cid": ..., "sec": ...}


class BrokerAccountResponse(BaseModel):
    id: str
    account_name: str
    broker: str
    is_demo: bool
    sandbox_mode: bool = True
    is_active: bool
    trading_enabled: bool
    profit_target: Optional[float] = None
    consistency_pct: Optional[float] = None
    consistency_locked_at: Optional[str] = None
    daily_pnl: Optional[float] = None
    daily_limit: Optional[float] = None
    created_at: str


class TradingEnabledRequest(BaseModel):
    trading_enabled: bool


class ConsistencyRuleRequest(BaseModel):
    profit_target: Optional[float] = None
    consistency_pct: Optional[float] = None  # e.g., 50.0 means 50%


class StartLiveSessionRequest(BaseModel):
    strategy_id: str
    broker_account_id: str
    instrument: str = "ES"
    daily_loss_limit: Optional[float] = None
    max_trades_today: Optional[int] = None


# ─────────────────────────────────────────────────────────────────────────────
# Broker Accounts
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/accounts", response_model=BrokerAccountResponse, status_code=status.HTTP_201_CREATED)
async def add_broker_account(
    data: AddBrokerAccountRequest,
    current_user: User = Depends(require_live_trading),
    db: AsyncSession = Depends(get_db),
):
    # Enforce max account limits based on tier
    existing = await db.execute(
        select(BrokerAccount).where(BrokerAccount.user_id == current_user.id, BrokerAccount.is_active == True)
    )
    account_count = len(existing.scalars().all())

    limits = {
        SubscriptionTier.TIER_1: 1,
        SubscriptionTier.TIER_3: 5,
        SubscriptionTier.TIER_4: 20,
        SubscriptionTier.TIER_5: 999_999,
    }
    max_accounts = limits.get(current_user.subscription_tier, 0)
    if account_count >= max_accounts:
        raise HTTPException(
            status_code=403,
            detail=f"Your tier allows a maximum of {max_accounts} broker accounts.",
        )

    # Validate the credentials with the broker BEFORE saving — better to fail
    # at "Connect" time with a clear error than later when the user tries to
    # start a live session.
    broker_name = (data.broker or "").lower()
    if broker_name in ("tradovate", "tradier", "alpaca"):
        if broker_name == "tradovate":
            from app.engines.live_trading.tradovate import TradovateBroker
            broker = TradovateBroker(data.credentials, is_demo=data.is_demo)
            reject_hint = "Double-check username, password, App ID, CID, and Secret — and that API access is enabled on your Tradovate account."
        elif broker_name == "tradier":
            from app.engines.live_trading.tradier import TradierBroker
            broker = TradierBroker(data.credentials, is_demo=data.is_demo)
            reject_hint = "Double-check your Access Token. Generate one at https://documentation.tradier.com/ → API Access Keys."
        else:  # alpaca
            from app.engines.live_trading.alpaca import AlpacaBroker
            broker = AlpacaBroker(data.credentials, is_demo=data.is_demo)
            reject_hint = "Double-check your Alpaca API Key + Secret. Get them from alpaca.markets → Paper Trading → Generate API Keys."
        try:
            connected = await broker.connect()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"{broker_name.capitalize()} connection error: {e}")
        finally:
            try:
                await broker.disconnect()
            except Exception:
                pass
        if not connected:
            raise HTTPException(status_code=400, detail=reject_hint)

    account = BrokerAccount(
        user_id=current_user.id,
        broker=data.broker,
        account_name=data.account_name,
        encrypted_credentials=encrypt_credentials(data.credentials),
        is_demo=data.is_demo,
    )
    db.add(account)
    await db.flush()

    return _to_response(account, await _account_daily_pnl(db, account.id))


class TestConnectionRequest(BaseModel):
    broker: str = "tradovate"
    is_demo: bool = True
    credentials: dict


@router.post("/accounts/test-connection")
async def test_broker_connection(
    data: TestConnectionRequest,
    current_user: User = Depends(require_live_trading),
):
    """Try the credentials against the broker without saving. Used by the
    'Test Connection' button on the Add-Account modal so users get instant
    feedback on whether their App ID / CID / Secret are correct."""
    broker_name = (data.broker or "").lower()
    if broker_name == "tradovate":
        from app.engines.live_trading.tradovate import TradovateBroker
        broker = TradovateBroker(data.credentials, is_demo=data.is_demo)
        reject_hint = "Double-check username, password, App ID, CID, and Secret."
    elif broker_name == "tradier":
        from app.engines.live_trading.tradier import TradierBroker
        broker = TradierBroker(data.credentials, is_demo=data.is_demo)
        reject_hint = "Double-check your Access Token (and Account Number if you provided one)."
    else:
        raise HTTPException(status_code=400, detail=f"Broker `{broker_name}` is not supported yet.")

    try:
        ok = await broker.connect()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{broker_name.capitalize()} connection error: {e}")
    finally:
        try:
            await broker.disconnect()
        except Exception:
            pass
    if not ok:
        raise HTTPException(
            status_code=400,
            detail=f"{broker_name.capitalize()} rejected the credentials. {reject_hint}",
        )
    return {"status": "ok", "environment": "demo" if data.is_demo else "live", "broker": broker_name}


@router.get("/accounts", response_model=list[BrokerAccountResponse])
async def list_broker_accounts(
    current_user: User = Depends(require_kyc_verified),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(BrokerAccount).where(BrokerAccount.user_id == current_user.id)
    )
    accounts = list(result.scalars().all())
    out = []
    for a in accounts:
        out.append(_to_response(a, await _account_daily_pnl(db, a.id)))
    return out


@router.patch("/accounts/{account_id}/trading-enabled", response_model=BrokerAccountResponse)
async def set_account_trading_enabled(
    account_id: str,
    data: TradingEnabledRequest,
    current_user: User = Depends(require_kyc_verified),
    db: AsyncSession = Depends(get_db),
):
    """Toggle whether the trade engine is allowed to act on this account.

    Off = engine ignores this account (no orders placed, existing positions
    untouched). On = engine resumes normal behavior.
    """
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.id == account_id,
            BrokerAccount.user_id == current_user.id,
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Broker account not found.")

    # Live-trading consent gate — only when *enabling* (turning off doesn't
    # need consent; that always works as a safety release).
    if data.trading_enabled and not account.sandbox_mode:
        await require_current_ack(db, current_user.id, "live_trading_consent")
        await require_current_ack(db, current_user.id, "risk_disclosure")

    account.trading_enabled = data.trading_enabled
    await db.commit()
    return _to_response(account, await _account_daily_pnl(db, account.id))


# ─────────────────────────────────────────────────────────────────────────────
# Live Sessions
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def start_live_session(
    data: StartLiveSessionRequest,
    current_user: User = Depends(require_live_trading),
    db: AsyncSession = Depends(get_db),
):
    # Live-trading consent gate — both consents required to start any
    # real-money session. Options consent additionally required if the
    # strategy is an options strategy (checked below once we have the row).
    await require_current_ack(db, current_user.id, "live_trading_consent")
    await require_current_ack(db, current_user.id, "risk_disclosure")

    # Validate strategy + account ownership
    strat = (await db.execute(
        select(Strategy).where(Strategy.id == data.strategy_id, Strategy.user_id == current_user.id)
    )).scalar_one_or_none()
    if not strat:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    # Options consent gate — applies if this strategy trades options.
    if getattr(strat, "options_mode", None):
        await require_current_ack(db, current_user.id, "options_trading_consent")

    acct = (await db.execute(
        select(BrokerAccount).where(BrokerAccount.id == data.broker_account_id, BrokerAccount.user_id == current_user.id)
    )).scalar_one_or_none()
    if not acct:
        raise HTTPException(status_code=404, detail="Broker account not found.")

    session = TradeSession(
        strategy_id=strat.id,
        user_id=current_user.id,
        broker_account_id=acct.id,
        mode=TradingMode.LIVE,
        is_active=True,
        daily_loss_limit=data.daily_loss_limit,
        max_trades_today=data.max_trades_today,
    )
    db.add(session)
    await db.flush()

    # Bug #5 fix: actually start the live trader. Previously this was a
    # commented-out TODO so the UI showed "session active" but nothing
    # traded. We spawn an asyncio task (no Celery dependency) — mirroring
    # how paper_trading dispatches its runner.
    try:
        from app.engines.live_trading.runner import start_live_session as _start_live
        await db.commit()  # commit session row so the runner can SELECT it
        import asyncio as _asyncio
        instrument = (strat.instruments or ["ES"])[0] if isinstance(strat.instruments, list) else "ES"
        _asyncio.create_task(_start_live(
            session_id=str(session.id),
            strategy_id=str(strat.id),
            user_id=str(current_user.id),
            broker_account_id=str(acct.id),
            instrument=instrument,
        ))
    except Exception as _e:
        from loguru import logger as _lg
        _lg.error(f"[live_trading.start_live_session] failed to dispatch runner: {_e}")
        # Don't fail the request — the session row is committed; the user
        # can retry start from the UI if dispatch failed.

    return {"session_id": str(session.id), "status": "started"}


@router.post("/sessions/{session_id}/kill-switch")
async def trigger_kill_switch(
    session_id: str,
    current_user: User = Depends(require_kyc_verified),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(TradeSession).where(TradeSession.id == session_id, TradeSession.user_id == current_user.id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    session.kill_switch_triggered = True
    session.is_active = False

    # In production: send kill signal to Celery worker
    # revoke_live_trader.delay(session_id)

    return {"status": "kill_switch_triggered", "session_id": session_id}


# ─── Account label + detail ────────────────────────────────────────────────

class AccountLabelUpdate(BaseModel):
    label: str


@router.patch("/accounts/{account_id}/label", response_model=BrokerAccountResponse)
async def set_account_label(
    account_id: str,
    data: AccountLabelUpdate,
    current_user: User = Depends(require_kyc_verified),
    db: AsyncSession = Depends(get_db),
):
    """Rename an account. The label IS the account's display name (account_name)."""
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.id == account_id,
            BrokerAccount.user_id == current_user.id,
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Broker account not found.")
    label = (data.label or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="Label cannot be empty.")
    if len(label) > 100:
        raise HTTPException(status_code=400, detail="Label must be 100 characters or fewer.")
    account.account_name = label
    await db.commit()
    return _to_response(account, await _account_daily_pnl(db, account.id))


@router.get("/accounts/{account_id}/detail")
async def get_live_account_detail(
    account_id: str,
    current_user: User = Depends(require_kyc_verified),
    db: AsyncSession = Depends(get_db),
):
    """Return one live account with its trades and computed metrics."""
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.id == account_id,
            BrokerAccount.user_id == current_user.id,
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Broker account not found.")

    from app.api.routes.paper_trading import _compute_metrics, SessionTradeRow
    trades_result = await db.execute(
        select(Trade)
        .where(Trade.broker_account_id == account.id)
        .order_by(Trade.entry_time.desc().nullslast(), Trade.created_at.desc())
    )
    trades = list(trades_result.scalars().all())
    metrics = _compute_metrics(trades)

    return {
        "account": {
            "id": str(account.id),
            "account_name": account.account_name,
            "broker": account.broker,
            "is_demo": account.is_demo,
            "is_active": account.is_active,
            "trading_enabled": account.trading_enabled,
            "created_at": account.created_at.isoformat(),
        },
        "metrics": metrics.model_dump(),
        "trades": [
            SessionTradeRow(
                id=str(t.id), instrument=t.instrument,
                direction=str(t.direction.value if hasattr(t.direction, 'value') else t.direction),
                status=str(t.status.value if hasattr(t.status, 'value') else t.status),
                entry_price=t.entry_price, exit_price=t.exit_price,
                stop_loss=t.stop_loss, take_profit=t.take_profit,
                contracts=t.contracts, pnl=t.pnl, net_pnl=t.net_pnl,
                entry_time=t.entry_time.isoformat() if t.entry_time else None,
                exit_time=t.exit_time.isoformat() if t.exit_time else None,
                exit_reason=t.exit_reason,
            ).model_dump()
            for t in trades
        ],
    }


@router.patch("/accounts/{account_id}/consistency", response_model=BrokerAccountResponse)
async def set_account_consistency(
    account_id: str,
    data: ConsistencyRuleRequest,
    current_user: User = Depends(require_kyc_verified),
    db: AsyncSession = Depends(get_db),
):
    """Set the per-account daily-consistency rule.

    consistency_pct is the max share of the profit_target the account is allowed
    to make in one day before trading auto-pauses (e.g., 50.0 = 50%). Pass null
    on either field to clear the rule.
    """
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.id == account_id,
            BrokerAccount.user_id == current_user.id,
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Broker account not found.")
    if data.profit_target is not None and data.profit_target <= 0:
        raise HTTPException(status_code=400, detail="profit_target must be positive")
    if data.consistency_pct is not None and not (0 < data.consistency_pct <= 100):
        raise HTTPException(status_code=400, detail="consistency_pct must be between 0 and 100")
    account.profit_target = data.profit_target
    account.consistency_pct = data.consistency_pct
    await db.commit()
    return _to_response(account, await _account_daily_pnl(db, account.id))


async def check_and_apply_consistency(account_id, db: AsyncSession) -> bool:
    """Hook called after a live trade closes. Computes today's P&L for the
    account, checks against the consistency cap, and pauses + emails if exceeded.
    Returns True if the account was just paused by this call.
    """
    result = await db.execute(select(BrokerAccount).where(BrokerAccount.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        return False
    if account.profit_target is None or account.consistency_pct is None:
        return False
    if not account.trading_enabled:
        return False  # already paused

    daily_pnl = await _account_daily_pnl(db, account.id)
    daily_limit = float(account.profit_target) * float(account.consistency_pct) / 100.0
    if daily_pnl < daily_limit:
        return False

    # Pause + record + email
    from datetime import datetime as _dt, timezone as _tz
    account.trading_enabled = False
    account.consistency_locked_at = _dt.now(_tz.utc)
    await db.commit()

    user_result = await db.execute(select(User).where(User.id == account.user_id))
    user = user_result.scalar_one_or_none()
    if user:
        from app.services import email as email_service
        try:
            email_service.send_consistency_hit_email(
                to=user.email,
                username=user.username,
                account_name=account.account_name,
                daily_pnl=daily_pnl,
                daily_limit=daily_limit,
                profit_target=float(account.profit_target),
                consistency_pct=float(account.consistency_pct),
            )
        except Exception:
            pass
    return True


@router.post("/accounts/{account_id}/check-consistency", response_model=BrokerAccountResponse)
async def manual_check_consistency(
    account_id: str,
    current_user: User = Depends(require_kyc_verified),
    db: AsyncSession = Depends(get_db),
):
    """Manual trigger to evaluate the consistency rule right now. Useful for testing
    and gives the user a way to confirm the cap is working without waiting for the
    live engine to close another trade.
    """
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.id == account_id,
            BrokerAccount.user_id == current_user.id,
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Broker account not found.")
    await check_and_apply_consistency(account.id, db)
    # Re-read in case the check just paused it
    await db.refresh(account)
    return _to_response(account, await _account_daily_pnl(db, account.id))


class SandboxToggleRequest(BaseModel):
    sandbox_mode: bool


@router.patch('/accounts/{account_id}/sandbox-mode', response_model=BrokerAccountResponse)
async def set_account_sandbox_mode(
    account_id: str,
    data: SandboxToggleRequest,
    current_user: User = Depends(require_kyc_verified),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(BrokerAccount).where(BrokerAccount.id == account_id, BrokerAccount.user_id == current_user.id)
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail='Account not found')
    account.sandbox_mode = data.sandbox_mode
    await db.commit()
    await db.refresh(account)
    return _to_response(account, await _account_daily_pnl(db, account.id))


# ── Position sizing ────────────────────────────────────────────────────────

class SizingUpdate(BaseModel):
    account_type: str = "cash"               # "cash" | "margin"
    risk_per_trade_usd: float | None = None  # fixed $ risk per trade
    risk_per_trade_pct: float | None = 1.0   # alt: % of equity per trade
    max_position_usd: float | None = None    # hard cap on capital deployed per trade


_starting_equity_col_checked = False
async def _ensure_starting_equity_column(db):
    """Idempotently ensure broker_accounts.starting_equity exists. Captured
    once at link time so the dashboard can reconcile equity vs realized P&L."""
    global _starting_equity_col_checked
    if _starting_equity_col_checked:
        return
    try:
        await db.execute(text(
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS starting_equity DOUBLE PRECISION"
        ))
        await db.commit()
        _starting_equity_col_checked = True
    except Exception as e:
        await db.rollback()
        logger.warning(f"[live-trading] could not ensure starting_equity column: {e}")


@router.get("/accounts/{account_id}/balance")
async def get_account_balance(
    account_id: str,
    current_user: User = Depends(require_kyc_verified),
    db: AsyncSession = Depends(get_db),
):
    """Pull live equity + buying power from the broker. Cached on the
    account row for fast subsequent reads."""
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.id == account_id,
            BrokerAccount.user_id == current_user.id,
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Broker account not found.")

    from app.engines.live_trading.broker_factory import build_broker_from_account
    broker = build_broker_from_account(account)
    if not broker:
        raise HTTPException(status_code=400, detail=f"Unsupported broker: {account.broker}")
    try:
        connected = await broker.connect()
        if not connected:
            raise HTTPException(status_code=502, detail="Broker connection failed.")
        bal = await broker.get_balance()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Broker balance fetch failed: {e}")
    finally:
        try:
            await broker.disconnect()
        except Exception:
            pass

    # Cache for fast UI reads
    from datetime import datetime, timezone
    account.cached_equity = float(bal.get("equity") or 0)
    account.cached_buying_power = float(bal.get("buying_power") or 0)
    account.cached_balance_at = datetime.now(timezone.utc)
    # If we discovered the broker reports a margin account but our row says cash, sync it.
    detected_type = bal.get("account_type", "cash")
    if account.account_type != detected_type:
        account.account_type = detected_type
    await db.commit()

    # Capture starting_equity once (used by /portfolio-summary reconciliation).
    # Tradier sandbox accounts always fund to $100k; for real accounts we
    # capture the first observed equity at link time.
    await _ensure_starting_equity_column(db)
    se_row = (await db.execute(text(
        "SELECT starting_equity FROM broker_accounts WHERE id = :id"
    ), {"id": str(account.id)})).first()
    if not se_row or se_row[0] is None:
        if account.is_demo and (account.broker or "").lower() == "tradier":
            new_se = 100_000.0
        else:
            new_se = float(bal.get("equity") or 0)
        await db.execute(text(
            "UPDATE broker_accounts SET starting_equity = :se WHERE id = :id"
        ), {"se": new_se, "id": str(account.id)})
        await db.commit()
        logger.info(f"[live-trading] captured starting_equity={new_se:.2f} for account={account.id} broker={account.broker} is_demo={account.is_demo}")

    return {
        "equity":       bal.get("equity"),
        "buying_power": bal.get("buying_power"),
        "cash":         bal.get("cash"),
        "account_type": bal.get("account_type"),
        "margin_call":  bal.get("margin_call"),
        "cached_at":    account.cached_balance_at.isoformat() if account.cached_balance_at else None,
    }


@router.patch("/accounts/{account_id}/sizing", response_model=BrokerAccountResponse)
async def update_account_sizing(
    account_id: str,
    data: SizingUpdate,
    current_user: User = Depends(require_kyc_verified),
    db: AsyncSession = Depends(get_db),
):
    """Save the user's position-sizing rules for this broker account."""
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.id == account_id,
            BrokerAccount.user_id == current_user.id,
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Broker account not found.")

    if data.account_type not in ("cash", "margin"):
        raise HTTPException(status_code=400, detail="account_type must be 'cash' or 'margin'.")

    # Validation: at least one risk knob must be set
    if data.risk_per_trade_usd is None and not data.risk_per_trade_pct:
        raise HTTPException(status_code=400, detail="Set either risk_per_trade_usd or risk_per_trade_pct.")
    if data.risk_per_trade_usd is not None and data.risk_per_trade_usd <= 0:
        raise HTTPException(status_code=400, detail="risk_per_trade_usd must be positive.")
    if data.risk_per_trade_pct is not None and (data.risk_per_trade_pct <= 0 or data.risk_per_trade_pct > 50):
        raise HTTPException(status_code=400, detail="risk_per_trade_pct must be between 0 and 50.")
    if data.max_position_usd is not None and data.max_position_usd <= 0:
        raise HTTPException(status_code=400, detail="max_position_usd must be positive.")

    account.account_type = data.account_type
    account.risk_per_trade_usd = data.risk_per_trade_usd
    account.risk_per_trade_pct = data.risk_per_trade_pct
    account.max_position_usd = data.max_position_usd
    await db.commit()
    await db.refresh(account)
    return BrokerAccountResponse(
        id=str(account.id), broker=account.broker, account_name=account.account_name,
        is_demo=account.is_demo, sandbox_mode=getattr(account, "sandbox_mode", True),
        is_active=account.is_active, trading_enabled=getattr(account, "trading_enabled", True),
        profit_target=account.profit_target, consistency_pct=account.consistency_pct,
        consistency_locked_at=account.consistency_locked_at.isoformat() if account.consistency_locked_at else None,
        created_at=account.created_at.isoformat() if account.created_at else "",
    )


@router.get("/accounts/{account_id}/sizing")
async def get_account_sizing(
    account_id: str,
    current_user: User = Depends(require_kyc_verified),
    db: AsyncSession = Depends(get_db),
):
    """Read back the current sizing settings + cached balance."""
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.id == account_id,
            BrokerAccount.user_id == current_user.id,
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Broker account not found.")
    return {
        "account_type":         getattr(account, "account_type", "cash"),
        "risk_per_trade_usd":   account.risk_per_trade_usd,
        "risk_per_trade_pct":   account.risk_per_trade_pct,
        "max_position_usd":     account.max_position_usd,
        "cached_equity":        account.cached_equity,
        "cached_buying_power":  account.cached_buying_power,
        "cached_balance_at":    account.cached_balance_at.isoformat() if account.cached_balance_at else None,
    }


# ── Portfolio summary (V2 dashboard) ──────────────────────────────────────

@router.get("/portfolio-summary")
async def get_portfolio_summary(
    current_user: User = Depends(require_kyc_verified),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate equity / buying power / PnL across all of the user's
    broker accounts. Returns:
        total_equity, total_buying_power, total_cash
        today_pnl, week_pnl, month_pnl, ytd_pnl
        open_positions_count
        accounts_count, healthy_accounts
        per_account: [{...}]"""
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import text as _t

    # Fetch all the user's broker accounts
    rows = (await db.execute(
        select(BrokerAccount).where(BrokerAccount.user_id == current_user.id, BrokerAccount.is_active == True)
    )).scalars().all()

    total_equity = 0.0
    total_buying_power = 0.0
    total_cash = 0.0
    healthy = 0
    per_account = []
    for a in rows:
        eq = float(a.cached_equity or 0)
        bp = float(a.cached_buying_power or 0)
        total_equity += eq
        total_buying_power += bp
        cached_at = a.cached_balance_at.isoformat() if a.cached_balance_at else None
        is_stale = False
        if a.cached_balance_at:
            is_stale = (datetime.now(timezone.utc) - a.cached_balance_at).total_seconds() > 1800
        else:
            is_stale = True
        if not is_stale:
            healthy += 1
        per_account.append({
            "id":           str(a.id),
            "broker":       a.broker,
            "account_name": a.account_name,
            "is_demo":      a.is_demo,
            "sandbox_mode": getattr(a, "sandbox_mode", True),
            "trading_enabled": getattr(a, "trading_enabled", True),
            "account_type": getattr(a, "account_type", "cash"),
            "equity":       eq,
            "buying_power": bp,
            "cached_at":    cached_at,
            "is_stale":     is_stale,
        })

    # PnL roll-ups from the trades table
    async def _pnl_since(since: datetime) -> float:
        r = await db.execute(_t(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades "
            "WHERE user_id = :uid AND mode = 'live' AND status = 'closed' "
            "AND (exit_time >= :since OR (exit_time IS NULL AND entry_time >= :since))"
        ), {"uid": str(current_user.id), "since": since})
        return float(r.scalar() or 0)

    async def _pnl_breakdown_since(since: datetime) -> dict:
        """Like _pnl_since but returns gross/net/commission so the UI can
        reconcile equity vs realized and surface fees explicitly."""
        r = await db.execute(_t(
            "SELECT COALESCE(SUM(pnl), 0)                       AS gross, "
            "       COALESCE(SUM(COALESCE(net_pnl, pnl)), 0)    AS net, "
            "       COALESCE(SUM(commission), 0)                AS commission, "
            "       COUNT(*)                                    AS n "
            "  FROM trades "
            " WHERE user_id = :uid AND mode = 'live' AND status = 'closed' "
            "   AND (exit_time >= :since OR (exit_time IS NULL AND entry_time >= :since))"
        ), {"uid": str(current_user.id), "since": since})
        row = r.fetchone()
        return {
            "gross":      float(row.gross or 0),
            "net":        float(row.net or 0),
            "commission": float(row.commission or 0),
            "count":      int(row.n or 0),
        }

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=now.weekday())
    month_start = today_start.replace(day=1)
    ytd_start = today_start.replace(month=1, day=1)

    today_pnl = await _pnl_since(today_start)
    # Unrealized P&L from open positions opened today (live mark-to-market)
    today_unrealized_pnl = 0.0
    try:
        import os as _os3, requests as _rq3
        k3 = _os3.environ.get("POLYGON_API_KEY", "")
        open_today = (await db.execute(_t("""
            SELECT instrument, entry_price, contracts FROM trades
             WHERE user_id = :uid AND mode='live' AND status='open'
               AND entry_time >= :since
        """), {"uid": str(current_user.id), "since": today_start})).fetchall()
        for r in open_today:
            try:
                u3 = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{r.instrument}"
                resp = _rq3.get(u3, params={"apiKey": k3}, timeout=3)
                if resp.status_code != 200: continue
                t3 = (resp.json() or {}).get("ticker") or {}
                live = None
                for fld, sub in (("lastTrade","p"),("min","c"),("day","c"),("prevDay","c")):
                    v = (t3.get(fld) or {}).get(sub)
                    if v and float(v) > 0: live = float(v); break
                if live and r.entry_price and r.contracts:
                    today_unrealized_pnl += (live - float(r.entry_price)) * int(r.contracts)
            except Exception: continue
    except Exception: pass
    week_pnl  = await _pnl_since(week_start)
    month_pnl = await _pnl_since(month_start)
    ytd_pnl   = await _pnl_since(ytd_start)

    # Unrealized P&L across ALL open positions (not just today's). Includes
    # both trades.status='open' rows AND open_positions_watch entries (the
    # latter covers tradier_sync positions that don't have trades rows).
    total_unrealized_pnl = 0.0
    try:
        import os as _osU, requests as _rqU
        kU = _osU.environ.get("POLYGON_API_KEY", "")
        # Union: open trades + watch table, deduped by ticker
        positions = (await db.execute(_t("""
            WITH a AS (
              SELECT instrument AS ticker, entry_price, contracts AS qty
                FROM trades
               WHERE user_id = :uid AND mode='live' AND status='open'
            ), b AS (
              SELECT ticker, entry_price, qty
                FROM open_positions_watch
               WHERE user_id = :uid
            )
            SELECT ticker, entry_price, qty FROM a
            UNION
            SELECT ticker, entry_price, qty FROM b
        """), {"uid": str(current_user.id)})).fetchall()
        for p in positions:
            try:
                uU = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{p.ticker}"
                rspU = _rqU.get(uU, params={"apiKey": kU}, timeout=3)
                if rspU.status_code != 200: continue
                tU = (rspU.json() or {}).get("ticker") or {}
                live = None
                for fld, sub in (("lastTrade","p"),("min","c"),("day","c"),("prevDay","c")):
                    v = (tU.get(fld) or {}).get(sub)
                    if v and float(v) > 0: live = float(v); break
                if live and p.entry_price and p.qty:
                    total_unrealized_pnl += (live - float(p.entry_price)) * int(p.qty)
            except Exception:
                pass
    except Exception:
        pass

    # Open positions count (status='open' or 'pending')
    op_row = await db.execute(_t(
        "SELECT COUNT(*) FROM trades WHERE user_id = :uid AND mode = 'live' AND status IN ('open', 'pending')"
    ), {"uid": str(current_user.id)})
    open_positions_count = int(op_row.scalar() or 0)

    # Quick last-7-day equity curve (daily PnL points for sparkline)
    curve_rows = await db.execute(_t("""
        SELECT date_trunc('day', exit_time) AS d, COALESCE(SUM(pnl), 0) AS pnl
          FROM trades
         WHERE user_id = :uid AND mode = 'live' AND status = 'closed'
           AND exit_time >= :since
         GROUP BY d ORDER BY d
    """), {"uid": str(current_user.id), "since": now - timedelta(days=14)})
    curve = [{"d": r[0].date().isoformat() if r[0] else None, "pnl": float(r[1] or 0)} for r in curve_rows.fetchall()]

    # === RECONCILIATION ===
    # Explicitly explain: starting_equity → realized_net + open_unrealized → current equity.
    # Any non-zero unexplained_gap is almost always broker-side closes that did not write
    # a trade row (e.g. flatten_all), un-recorded fees, slippage vs recorded entry/exit,
    # or a stale cached_equity.
    ytd_breakdown = await _pnl_breakdown_since(ytd_start)
    se_rows = (await db.execute(_t(
        "SELECT id, starting_equity FROM broker_accounts "
        "WHERE user_id = :uid AND is_active = true"
    ), {"uid": str(current_user.id)})).all()
    se_map = {str(r[0]): (float(r[1]) if r[1] is not None else None) for r in se_rows}
    starting_equity = 0.0
    for a in rows:
        se = se_map.get(str(a.id))
        if se is not None:
            starting_equity += se
        elif a.is_demo and (a.broker or "").lower() == "tradier":
            starting_equity += 100_000.0  # Tradier sandbox default funding
        else:
            starting_equity += float(a.cached_equity or 0)
    equity_delta     = total_equity - starting_equity
    reconciled_delta = ytd_breakdown["net"] + total_unrealized_pnl
    unexplained_gap  = equity_delta - reconciled_delta
    reconciliation = {
        "starting_equity":    round(starting_equity, 2),
        "realized_ytd_net":   round(ytd_breakdown["net"], 2),
        "realized_ytd_gross": round(ytd_breakdown["gross"], 2),
        "commission_ytd":     round(ytd_breakdown["commission"], 2),
        "unrealized_open":    round(total_unrealized_pnl, 2),
        "equity_now":         round(total_equity, 2),
        "equity_delta":       round(equity_delta, 2),
        "reconciled_delta":   round(reconciled_delta, 2),
        "unexplained_gap":    round(unexplained_gap, 2),
        "notes": (
            "equity_delta should ~= realized_ytd_net + unrealized_open. "
            "A non-zero unexplained_gap usually means broker-side closes that "
            "did not write a trade row (e.g. flatten_all), un-recorded fees, "
            "slippage between recorded entry/exit and actual broker fill, "
            "or a stale cached_equity."
        ),
    }
    logger.info(
        f"[portfolio] user={current_user.email} "
        f"eq={total_equity:.2f} start={starting_equity:.2f} "
        f"rlz_net_ytd={ytd_breakdown['net']:.2f} unrlz={total_unrealized_pnl:.2f} "
        f"delta={equity_delta:.2f} gap={unexplained_gap:.2f}"
    )

    return {
        "total_equity":        total_equity,
        "total_buying_power":  total_buying_power,
        "total_cash":          total_cash,
        "today_pnl":           today_pnl,
        "today_unrealized_pnl": round(today_unrealized_pnl, 2),
        "total_unrealized_pnl": round(total_unrealized_pnl, 2),
        "week_pnl":            week_pnl,
        "month_pnl":           month_pnl,
        "ytd_pnl":             ytd_pnl,
        "ytd_total_pnl":       round(ytd_pnl + total_unrealized_pnl, 2),
        "reconciliation":      reconciliation,
        "open_positions_count": open_positions_count,
        "accounts_count":      len(rows),
        "healthy_accounts":    healthy,
        "per_account":         per_account,
        "equity_curve_14d":    curve,
    }


@router.get("/sessions")
async def list_live_sessions(
    current_user: User = Depends(require_kyc_verified),
    db: AsyncSession = Depends(get_db),
):
    """List the user's live trading sessions (active + recently stopped)."""
    from sqlalchemy import text as _t
    rows = await db.execute(_t("""
        SELECT ts.id, ts.strategy_id, s.name AS strategy_name, ts.broker_account_id,
               ba.account_name AS broker_account_name, ba.broker AS broker,
               ts.instrument, ts.is_active, ts.started_at, ts.ended_at,
               ts.total_trades, ts.net_pnl, ts.daily_loss_limit
          FROM trade_sessions ts
          JOIN strategies s ON s.id = ts.strategy_id
     LEFT JOIN broker_accounts ba ON ba.id = ts.broker_account_id
         WHERE ts.user_id = :uid AND ts.mode = 'live'
         ORDER BY ts.is_active DESC, ts.started_at DESC NULLS LAST
         LIMIT 50
    """), {"uid": str(current_user.id)})
    return [
        {
            "id": str(r[0]),
            "strategy_id": str(r[1]),
            "strategy_name": r[2] or "",
            "broker_account_id": str(r[3]) if r[3] else None,
            "broker_account_name": r[4] or "",
            "broker": r[5] or "",
            "instrument": r[6] or "",
            "is_active": bool(r[7]),
            "started_at": r[8].isoformat() if r[8] else None,
            "ended_at": r[9].isoformat() if r[9] else None,
            "total_trades": int(r[10] or 0),
            "net_pnl": float(r[11] or 0),
            "daily_loss_limit": float(r[12]) if r[12] is not None else None,
        }
        for r in rows.fetchall()
    ]


@router.post("/sessions/{session_id}/pause")
async def pause_live_session(
    session_id: str,
    current_user: User = Depends(require_kyc_verified),
    db: AsyncSession = Depends(get_db),
):
    """Pause a live session — no new orders fire. Existing positions stay open
    and bracket SL/TP remain at the broker. Use kill-switch to flatten."""
    sess = (await db.execute(
        select(TradeSession).where(
            TradeSession.id == session_id,
            TradeSession.user_id == current_user.id,
            TradeSession.mode == TradingMode.LIVE,
        )
    )).scalar_one_or_none()
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found.")
    # We don't have a dedicated paused column on TradeSession; reuse
    # is_active = False to halt the runner. Resume restores it.
    sess.is_active = False
    await db.commit()
    return {"status": "paused", "session_id": session_id}


@router.post("/sessions/{session_id}/resume")
async def resume_live_session(
    session_id: str,
    current_user: User = Depends(require_kyc_verified),
    db: AsyncSession = Depends(get_db),
):
    sess = (await db.execute(
        select(TradeSession).where(
            TradeSession.id == session_id,
            TradeSession.user_id == current_user.id,
            TradeSession.mode == TradingMode.LIVE,
        )
    )).scalar_one_or_none()
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found.")
    sess.is_active = True
    await db.commit()
    try:
        from app.engines.live_trading.runner import start_live_session as _start
        import asyncio as _a
        _a.create_task(_start(
            session_id=str(sess.id), strategy_id=str(sess.strategy_id),
            user_id=str(current_user.id),
            broker_account_id=str(sess.broker_account_id) if sess.broker_account_id else "",
            instrument=sess.instrument or "ES",
        ))
    except Exception:
        pass
    return {"status": "resumed", "session_id": session_id}


@router.get("/unrealized-pnl")
async def get_unrealized_pnl(
    current_user: User = Depends(require_kyc_verified),
    db: AsyncSession = Depends(get_db),
):
    """Return live unrealized P&L for each open position across all live
    sessions. Computed as (current_price - entry_price) * contracts * tick_value.
    For now we mark-to-market via yfinance last-trade (15-min delay)."""
    from sqlalchemy import text as _t
    rows = await db.execute(_t("""
        SELECT id, instrument, direction, contracts, entry_price, stop_loss, take_profit
          FROM trades
         WHERE user_id = :uid AND mode = 'live' AND status IN ('open', 'pending')
    """), {"uid": str(current_user.id)})
    open_trades = rows.fetchall()
    if not open_trades:
        return {"open_count": 0, "total_unrealized": 0.0, "positions": []}

    # Map instrument → multiplier (futures vs stock)
    FUTURES_MULT = {"ES": 50, "NQ": 20, "RTY": 50, "YM": 5, "MES": 5, "MNQ": 2, "M2K": 5, "MYM": 0.5}
    positions = []
    total = 0.0
    try:
        import yfinance as yf
    except Exception:
        yf = None

    for r in open_trades:
        inst = r[1]
        side = r[2]
        qty  = r[3] or 1
        entry = r[4] or 0
        cur_price = entry  # fallback to entry if quote unavailable
        # Use Polygon snapshot (real-time on Stocks Starter for some fields,
        # delayed 15min for last-trade — better than yfinance which gets rate-limited).
        try:
            import os as _os_up, requests as _rq_up
            _k = _os_up.environ.get("POLYGON_API_KEY", "")
            if _k:
                _u = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{inst}"
                _r = _rq_up.get(_u, params={"apiKey": _k}, timeout=3)
                if _r.status_code == 200:
                    _t = (_r.json() or {}).get("ticker") or {}
                    for _fld, _sub in (("lastTrade","p"),("min","c"),("day","c"),("prevDay","c")):
                        _v = (_t.get(_fld) or {}).get(_sub)
                        if _v and float(_v) > 0:
                            cur_price = float(_v); break
        except Exception:
            pass

        mult = FUTURES_MULT.get(inst, 1)  # stocks = $1/point
        per_point = cur_price - entry
        if side == "short":
            per_point = -per_point
        unrealized = per_point * qty * mult
        total += unrealized
        positions.append({
            "trade_id": str(r[0]),
            "instrument": inst,
            "direction": side,
            "contracts": qty,
            "entry_price": entry,
            "current_price": round(cur_price, 4),
            "unrealized_pnl": round(unrealized, 2),
            "stop_loss": r[5],
            "take_profit": r[6],
            "distance_to_stop": round(abs(cur_price - (r[5] or entry)) * mult * qty, 2),
            "distance_to_target": round(abs((r[6] or entry) - cur_price) * mult * qty, 2),
        })
    return {
        "open_count": len(positions),
        "total_unrealized": round(total, 2),
        "positions": positions,
    }


# ── position sizing preview ──────────────────────────────────────────
def compute_stock_position_size(
    *,
    entry_price: float,
    stop_loss: float,
    account_equity: float | None,
    buying_power: float | None,
    account_type: str | None,
    risk_per_trade_usd: float | None,
    risk_per_trade_pct: float | None,
    max_position_usd: float | None,
) -> dict:
    """Compute how many shares to buy given entry/stop and account-level risk
    settings. Pure function — no DB, no I/O. Used by the /sizing-preview
    endpoint and unit-tested in backend/tests/test_position_sizing.py.

    Returns a dict whose `summary` field contains the literal placeholder
    `{TICKER}` so the caller substitutes the symbol.
    """
    # Coerce None equity/BP to 0 so downstream math doesn't blow up.
    eq = float(account_equity) if account_equity is not None else 0.0
    bp = float(buying_power) if buying_power is not None else 0.0
    acct_type = (account_type or "cash").lower()

    # Edge: bad entry price.
    if entry_price is None or entry_price <= 0:
        return {
            "error": "entry_price must be > 0",
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "final_shares": 0,
            "summary": "Invalid entry price.",
        }

    # Edge: entry == stop → undefined position size.
    risk_per_share = abs(float(entry_price) - float(stop_loss))
    if risk_per_share == 0:
        return {
            "error": "entry and stop_loss are equal — risk-per-share is zero",
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "risk_per_share": 0.0,
            "final_shares": 0,
            "summary": "Entry and stop are equal — define a stop to size the trade.",
        }

    # Pick risk model: explicit USD wins over %, else default to 1%.
    if risk_per_trade_usd is not None and float(risk_per_trade_usd) > 0:
        risk_model = "usd"
        risk_dollars_target = float(risk_per_trade_usd)
    elif risk_per_trade_pct is not None and float(risk_per_trade_pct) > 0:
        risk_model = "pct"
        risk_dollars_target = eq * (float(risk_per_trade_pct) / 100.0)
    else:
        risk_model = "default_pct_1"
        risk_dollars_target = eq * 0.01

    raw_shares = int(risk_dollars_target // risk_per_share)
    raw_notional = raw_shares * float(entry_price)

    # Constraints: max_position_usd then buying_power. Both are applied if
    # they'd reduce share count; we report each one's would-cap and whether
    # it was the binding constraint.
    constraints: list[dict] = []

    if max_position_usd is not None and float(max_position_usd) > 0:
        mp_cap_shares = int(float(max_position_usd) // float(entry_price))
    else:
        mp_cap_shares = None
    constraints.append({
        "name": "max_position_usd",
        "limit_usd": float(max_position_usd) if max_position_usd is not None else None,
        "would_cap_shares_at": mp_cap_shares,
        "applied": False,
    })

    bp_cap_shares = int(bp // float(entry_price)) if bp > 0 else 0
    constraints.append({
        "name": "buying_power",
        "limit_usd": bp,
        "would_cap_shares_at": bp_cap_shares,
        "applied": False,
    })

    final_shares = raw_shares
    if mp_cap_shares is not None and mp_cap_shares < final_shares:
        final_shares = mp_cap_shares
        constraints[0]["applied"] = True
    if bp_cap_shares < final_shares:
        final_shares = bp_cap_shares
        constraints[1]["applied"] = True
    # If max_position_usd already capped us and buying_power matches that cap
    # exactly, treat buying_power as not the binding constraint (max_position
    # already did the work). Only mark applied if it strictly reduced shares.

    if final_shares < 0:
        final_shares = 0

    final_notional = final_shares * float(entry_price)
    actual_dollar_risk = final_shares * risk_per_share

    if final_shares == 0:
        # Build a brief reason from whichever constraint zeroed us out.
        reasons = []
        if mp_cap_shares is not None and mp_cap_shares == 0:
            reasons.append(f"max_position_usd=${float(max_position_usd):,.0f}")
        if bp_cap_shares == 0:
            reasons.append(f"buying_power=${bp:,.0f}")
        if raw_shares == 0:
            reasons.append(
                f"risk target ${risk_dollars_target:,.2f} < risk/share ${risk_per_share:,.2f}"
            )
        reason_str = "; ".join(reasons) if reasons else "constraints zeroed position"
        summary = f"Cannot size a position: {reason_str}."
    else:
        plural = "s" if final_shares != 1 else ""
        summary = (
            f"Buy {final_shares} share{plural} of {{TICKER}} "
            f"(~${final_notional:,.2f} total). "
            f"Risk: ${actual_dollar_risk:,.2f} if stop hits at ${float(stop_loss):.2f}."
        )

    return {
        "entry_price": float(entry_price),
        "stop_loss": float(stop_loss),
        "risk_per_share": float(risk_per_share),
        "risk_model": risk_model,
        "risk_per_trade_usd": float(risk_per_trade_usd) if risk_per_trade_usd is not None else None,
        "risk_per_trade_pct": float(risk_per_trade_pct) if risk_per_trade_pct is not None else None,
        "max_position_usd": float(max_position_usd) if max_position_usd is not None else None,
        "account_equity_used": eq,
        "buying_power_used": bp,
        "account_type": acct_type,
        "risk_dollars_target": float(risk_dollars_target),
        "raw_shares": int(raw_shares),
        "raw_notional": float(raw_notional),
        "constraints": constraints,
        "final_shares": int(final_shares),
        "final_notional": float(final_notional),
        "actual_dollar_risk": float(actual_dollar_risk),
        "summary": summary,
    }


@router.get("/sizing-preview")
async def sizing_preview(
    ticker: str,
    entry: float,
    stop: float,
    current_user: User = Depends(require_kyc_verified),
    db: AsyncSession = Depends(get_db),
):
    """For a hypothetical trade signal at (entry, stop) on `ticker`, show
    *exactly* what each of the user's active broker accounts would buy.

    Pulls per-account risk settings + cached balance from broker_accounts.
    Does NOT place any orders.
    """
    from loguru import logger  # already used elsewhere in this module
    sym = (ticker or "").strip().upper()

    accts_res = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == current_user.id,
            BrokerAccount.is_active == True,  # noqa: E712
        )
    )
    accts = list(accts_res.scalars().all())

    logger.info(
        f"[sizing-preview] user={current_user.email} ticker={sym} "
        f"entry={entry} stop={stop} accounts={len(accts)}"
    )

    per_account = []
    for a in accts:
        sizing = compute_stock_position_size(
            entry_price=float(entry),
            stop_loss=float(stop),
            account_equity=a.cached_equity,
            buying_power=a.cached_buying_power,
            account_type=a.account_type,
            risk_per_trade_usd=a.risk_per_trade_usd,
            risk_per_trade_pct=a.risk_per_trade_pct,
            max_position_usd=a.max_position_usd,
        )
        # Caller substitutes the symbol into the summary placeholder.
        sizing["summary"] = sizing["summary"].replace("{TICKER}", sym)
        per_account.append({
            "broker_account_id": str(a.id),
            "broker": a.broker,
            "account_name": a.account_name,
            "is_demo": bool(a.is_demo),
            "sizing": sizing,
        })

    return {
        "ticker": sym,
        "entry": float(entry),
        "stop": float(stop),
        "per_account": per_account,
    }
