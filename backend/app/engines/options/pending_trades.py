import json
"""Pending-trade lifecycle.

Workflow for the "8:30 AM pre-market scan → 8:45 AM auto-execute" pattern:

  08:30 ET  Scanner runs across the universe → top-K signals
            Each signal is saved as a `pending_trades` row with
            status='pending' and a unique confirm_token. Email goes
            out with Confirm/Skip buttons.

  Between 08:30 and 08:45:
    • User clicks Confirm → status='confirmed' → execute immediately
    • User clicks Skip    → status='declined' → never execute
    • User does nothing   → status stays 'pending' until expiry

  08:45 ET  Auto-execute job:
            For each strategy with require_confirm=False, fire all 'pending'
            into 'confirmed' → execute. For strategies with require_confirm=True,
            transition any still-pending to 'expired'.

  Intraday signals bypass the confirm step — they save with is_intraday=True
  and immediately execute, sending a "receipt" email instead of a confirm.
"""
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from loguru import logger

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.config import settings


def _gen_token() -> str:
    return secrets.token_urlsafe(24)


async def create_pending_trade(*, user_id: str, strategy_id: str,
                                 mode: str, instrument: str, direction: str,
                                 contracts: int, entry: Optional[float],
                                 stop: Optional[float], target: Optional[float],
                                 bias: Optional[str], reason: str,
                                 broker_account_id: Optional[str] = None,
                                 session_id: Optional[str] = None,
                                 is_intraday: bool = False,
                                 expires_in_minutes: int = 30,
                                 notes: Optional[dict] = None) -> str:
    """Insert a pending_trades row, return the row id. The confirm_token is
    only emitted (and only meaningful) when is_intraday=False."""
    pid = str(uuid.uuid4())
    token = None if is_intraday else _gen_token()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=expires_in_minutes)
    async with async_session_factory() as db:
        await db.execute(text("""
            INSERT INTO pending_trades
                (id, user_id, strategy_id, mode, instrument, direction, contracts,
                 entry_price, stop_loss, take_profit, bias, reason, notes,
                 status, confirm_token, expires_at, is_intraday,
                 broker_account_id, session_id)
            VALUES
                (:id, :uid, :sid, :mode, :inst, :dir, :ctr,
                 :ep, :sl, :tp, :bias, :rsn, :notes,
                 :status, :tok, :exp, :intra, :bid, :sess)
        """), {
            "id": pid, "uid": user_id, "sid": strategy_id, "mode": mode,
            "inst": instrument, "dir": direction, "ctr": contracts,
            "ep": entry, "sl": stop, "tp": target,
            "bias": bias, "rsn": reason, "notes": json.dumps(notes or {}),
            "status": "executed" if is_intraday else "pending",
            "tok": token, "exp": expires_at, "intra": is_intraday,
            "bid": broker_account_id, "sess": session_id,
        })
        await db.commit()
    return pid


async def confirm_pending_trade(confirm_token: str) -> Optional[dict]:
    """Mark a pending trade as confirmed and return its dict. Caller is
    responsible for actually placing the order based on the returned data."""
    async with async_session_factory() as db:
        row = (await db.execute(text("""
            UPDATE pending_trades
               SET status = 'confirmed', confirmed_at = NOW()
             WHERE confirm_token = :t
               AND status = 'pending'
               AND expires_at > NOW()
            RETURNING id, user_id, strategy_id, mode, instrument, direction,
                      contracts, entry_price, stop_loss, take_profit, bias,
                      broker_account_id, session_id
        """), {"t": confirm_token})).fetchone()
        await db.commit()
    if not row:
        return None
    return dict(row._mapping)


async def decline_pending_trade(confirm_token: str) -> bool:
    async with async_session_factory() as db:
        r = await db.execute(text("""
            UPDATE pending_trades
               SET status = 'declined', declined_at = NOW()
             WHERE confirm_token = :t AND status = 'pending'
        """), {"t": confirm_token})
        await db.commit()
        return r.rowcount > 0


async def expire_old_pending() -> int:
    """Mark any pending trades past their expires_at as 'expired'."""
    async with async_session_factory() as db:
        r = await db.execute(text("""
            UPDATE pending_trades
               SET status = 'expired'
             WHERE status = 'pending' AND expires_at < NOW()
        """))
        await db.commit()
        return r.rowcount


async def auto_execute_pending(strategy_id: str) -> list[dict]:
    """For strategies with require_confirm=False, auto-confirm any still-
    pending trades. Returns the rows that were flipped — caller fires them."""
    async with async_session_factory() as db:
        rows = (await db.execute(text("""
            UPDATE pending_trades
               SET status = 'confirmed', confirmed_at = NOW()
             WHERE strategy_id = :sid AND status = 'pending'
            RETURNING id, user_id, strategy_id, mode, instrument, direction,
                      contracts, entry_price, stop_loss, take_profit, bias,
                      broker_account_id, session_id
        """), {"sid": strategy_id})).fetchall()
        await db.commit()
    return [dict(r._mapping) for r in rows]
