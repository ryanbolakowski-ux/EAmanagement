import os
"""Account Signals — strategy watchers that emit notifications instead of
placing orders. Used for prop-firm funded accounts where automated trading
is prohibited."""
import asyncio
import json
import uuid
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.database import get_db, async_session_factory
from app.models.user import User
from app.models.strategy import Strategy
from app.core.auth import require_2fa_when_paid as get_current_user
from app.services.email import _send, _send_tracked, _logo_header
from app.config import settings

router = APIRouter()
# 2FA gate: routes here require totp_enabled if user is on paid/trial subscription


# ─── Pydantic models ─────────────────────────────────────────────────

class WatcherCreate(BaseModel):
    strategy_id: str
    instruments: list[str]
    account_label: str
    channels: list[str] = ["email"]
    session_filter: str = "all"  # all | NY_AM | NY_PM | LONDON | ASIA


class WatcherResponse(BaseModel):
    id: str
    strategy_id: str
    strategy_name: str
    instruments: list[str]
    account_label: str
    channels: list[str]
    session_filter: str = "all"
    is_active: bool
    created_at: str


class SignalResponse(BaseModel):
    id: str
    strategy_name: str
    instrument: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    bias: Optional[str]
    fired_at: str
    status: str
    outcome: Optional[str] = None
    outcome_price: Optional[float] = None
    outcome_r: Optional[float] = None
    resolved_at: Optional[str] = None
    # Suppressed-row diagnostics. `suppression_reason` is the underlying
    # provider_status (e.g. "dead_zone_suppressed", "session_cap_suppressed",
    # "duplicate_suppressed") so the UI can show *why* a signal didn't send.
    suppression_reason: Optional[str] = None
    duplicate_suppressed_at: Optional[str] = None
    duplicate_suppressed_count: Optional[int] = None
    error_message: Optional[str] = None


# ─── Routes ──────────────────────────────────────────────────────────

@router.get("/", response_model=list[SignalResponse])
async def list_signals(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: str = "sent",
    include_suppressed: bool = False,
    limit: int = 200,
):
    """List signals for the current user.

    By default returns only `status='sent'` rows so the Email Signals page
    shows the clean list of signals that were actually delivered. The other
    ~80% of rows in the table are `suppressed` (dead-zone, session-cap,
    duplicate, geometry-rejected, etc.) — useful diagnostics but they should
    not clutter the user-facing feed.

    Override knobs:
      * `?status=foo`            — filter to any specific status value
      * `?include_suppressed=true` — drop the filter entirely (returns all)
    """
    if include_suppressed:
        sql = """
            SELECT s.id, s.instrument, s.direction, s.entry_price, s.stop_loss,
                   s.take_profit, s.bias, s.fired_at, s.status,
                   s.outcome, s.outcome_price, s.outcome_r, s.resolved_at,
                   s.provider_status, s.error_message,
                   s.duplicate_suppressed_at, s.duplicate_suppressed_count,
                   st.name AS strategy_name
              FROM account_signals s
              JOIN strategies st ON st.id = s.strategy_id
             WHERE s.user_id = :uid
             ORDER BY s.fired_at DESC
             LIMIT :lim
        """
        params = {"uid": str(current_user.id), "lim": int(limit)}
    else:
        sql = """
            SELECT s.id, s.instrument, s.direction, s.entry_price, s.stop_loss,
                   s.take_profit, s.bias, s.fired_at, s.status,
                   s.outcome, s.outcome_price, s.outcome_r, s.resolved_at,
                   s.provider_status, s.error_message,
                   s.duplicate_suppressed_at, s.duplicate_suppressed_count,
                   st.name AS strategy_name
              FROM account_signals s
              JOIN strategies st ON st.id = s.strategy_id
             WHERE s.user_id = :uid AND s.status = :status
             ORDER BY s.fired_at DESC
             LIMIT :lim
        """
        params = {"uid": str(current_user.id), "status": status, "lim": int(limit)}
    rows = (await db.execute(text(sql), params)).fetchall()
    logger.info(
        f"[signals.list] user={current_user.id} status={status} "
        f"include_suppressed={include_suppressed} returned={len(rows)}"
    )
    return [
        SignalResponse(
            id=str(r.id), strategy_name=r.strategy_name,
            instrument=r.instrument, direction=r.direction,
            entry_price=float(r.entry_price), stop_loss=float(r.stop_loss),
            take_profit=float(r.take_profit), bias=r.bias,
            fired_at=r.fired_at.isoformat() if r.fired_at else "",
            status=r.status,
            outcome=r.outcome,
            outcome_price=float(r.outcome_price) if r.outcome_price is not None else None,
            outcome_r=float(r.outcome_r) if r.outcome_r is not None else None,
            resolved_at=r.resolved_at.isoformat() if r.resolved_at else None,
            suppression_reason=getattr(r, "provider_status", None) if r.status == "suppressed" else None,
            duplicate_suppressed_at=getattr(r, "duplicate_suppressed_at", None).isoformat() if getattr(r, "duplicate_suppressed_at", None) else None,
            duplicate_suppressed_count=int(r.duplicate_suppressed_count) if getattr(r, "duplicate_suppressed_count", None) is not None else None,
            error_message=getattr(r, "error_message", None),
        )
        for r in rows
    ]


@router.get("/suppressed", response_model=list[SignalResponse])
async def list_suppressed_signals(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
):
    """Admin-only view of suppressed rows for diagnostics.

    Surfaces duplicate-suppression metadata and any error_message that was
    captured at send time. Sorted newest first so the freshest suppressions
    are easy to spot."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only.")
    rows = (await db.execute(text("""
        SELECT s.id, s.instrument, s.direction, s.entry_price, s.stop_loss,
               s.take_profit, s.bias, s.fired_at, s.status,
               s.outcome, s.outcome_price, s.outcome_r, s.resolved_at,
               s.duplicate_suppressed_at, s.duplicate_suppressed_count,
               s.error_message,
               st.name AS strategy_name
          FROM account_signals s
          JOIN strategies st ON st.id = s.strategy_id
         WHERE s.status = 'suppressed'
         ORDER BY s.fired_at DESC
         LIMIT :lim
    """), {"lim": int(limit)})).fetchall()
    logger.info(f"[signals.suppressed] admin={current_user.id} returned={len(rows)}")
    return [
        SignalResponse(
            id=str(r.id), strategy_name=r.strategy_name,
            instrument=r.instrument, direction=r.direction,
            entry_price=float(r.entry_price), stop_loss=float(r.stop_loss),
            take_profit=float(r.take_profit), bias=r.bias,
            fired_at=r.fired_at.isoformat() if r.fired_at else "",
            status=r.status,
            outcome=r.outcome,
            outcome_price=float(r.outcome_price) if r.outcome_price is not None else None,
            outcome_r=float(r.outcome_r) if r.outcome_r is not None else None,
            resolved_at=r.resolved_at.isoformat() if r.resolved_at else None,
            duplicate_suppressed_at=r.duplicate_suppressed_at.isoformat() if r.duplicate_suppressed_at else None,
            duplicate_suppressed_count=int(r.duplicate_suppressed_count) if r.duplicate_suppressed_count is not None else None,
            error_message=r.error_message,
        )
        for r in rows
    ]


@router.get("/watchers", response_model=list[WatcherResponse])
async def list_watchers(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(text("""
        SELECT w.id, w.strategy_id, w.instruments, w.account_label, w.channels,
               w.is_active, w.created_at, st.name AS strategy_name
        FROM account_signal_watchers w
        JOIN strategies st ON st.id = w.strategy_id
        WHERE w.user_id = :uid AND w.is_active = TRUE
        ORDER BY w.created_at DESC
    """), {"uid": str(current_user.id)})
    return [
        WatcherResponse(
            id=str(r.id), strategy_id=str(r.strategy_id),
            strategy_name=r.strategy_name,
            instruments=r.instruments or [],
            account_label=r.account_label,
            channels=r.channels or ["email"], session_filter=getattr(r, "session_filter", "all"),
            is_active=r.is_active,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows.fetchall()
    ]


@router.post("/watchers", response_model=WatcherResponse, status_code=status.HTTP_201_CREATED)
async def create_watcher(
    data: WatcherCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    sresult = await db.execute(
        select(Strategy).where(Strategy.id == data.strategy_id, Strategy.user_id == current_user.id)
    )
    strategy = sresult.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    # Bug 2: a DRAFT strategy is not ready to trade — block watcher creation so
    # we never email/push signals for an unpublished strategy. The UI surfaces
    # this 409 with a clear "activate the strategy first" message.
    _stat = strategy.status.value if hasattr(strategy.status, "value") else str(strategy.status)
    if _stat == "draft":
        raise HTTPException(
            status_code=409,
            detail="This strategy is a draft. Activate (publish) it before creating a signal watcher.",
        )

    wid = uuid.uuid4()
    # asyncpg + JSON columns: list/dict params must be JSON-serialized first
    await db.execute(text("""
        INSERT INTO account_signal_watchers
            (id, user_id, strategy_id, instruments, account_label, channels, session_filter, is_active, created_at)
        VALUES
            (CAST(:id AS uuid), CAST(:uid AS uuid), CAST(:sid AS uuid),
             CAST(:inst AS json), :label, CAST(:ch AS json), :sf, TRUE, NOW())
    """), {
        "id":    str(wid), "uid":   str(current_user.id),
        "sid":   data.strategy_id,
        "inst":  json.dumps(data.instruments),
        "label": data.account_label,
        "ch":    json.dumps(data.channels or ["email"]),
        "sf":    None,
    })
    await db.commit()

    # Kick off the watcher loop
    from app.engines.account_signals.runner import start_watcher
    asyncio.create_task(start_watcher(str(wid), data.strategy_id, str(current_user.id), data.instruments, data.account_label, data.channels))

    return WatcherResponse(
        id=str(wid), strategy_id=data.strategy_id, strategy_name=strategy.name,
        instruments=data.instruments, account_label=data.account_label,
        channels=data.channels or ["email"], is_active=True,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


@router.delete("/watchers/{watcher_id}")
async def stop_watcher(
    watcher_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(text("""
        UPDATE account_signal_watchers SET is_active = FALSE
         WHERE id = :id AND user_id = :uid
    """), {"id": watcher_id, "uid": str(current_user.id)})
    await db.commit()
    from app.engines.account_signals.runner import stop_watcher as _stop
    await _stop(watcher_id)
    return {"status": "stopped"}


# ─── Email rendering ─────────────────────────────────────────────────

def send_signal_email(
    to: str, username: str, account_label: str,
    strategy_name: str, instrument: str, direction: str,
    entry: float, stop: float, target: float, bias: Optional[str], fired_at: str,
    signal_id: Optional[str] = None,
    entry_detected_at: Optional["datetime"] = None,
) -> bool:
    """Send a futures entry-signal email.

    signal_id (recommended): used to build an idempotency key in Redis so the
    same DB signal row can never trigger a duplicate email even if the watcher
    is restarted mid-flight. If omitted, falls back to (to, inst, dir, entry,
    minute) which is also stable per minute.

    entry_detected_at (recommended): UTC timestamp from the moment the strategy
    confirmed the entry. Logged in the [signal-email-timing] line so the gap
    between detection and send is queryable.
    """
    from loguru import logger as _lg
    from datetime import datetime as _dt_fs, date as _date_fs, timezone as _tz_fs
    side_color = "#16a34a" if direction == "long" else "#dc2626"
    side_word = "LONG" if direction == "long" else "SHORT"

    # Capture detection + attempt timestamps up front so they are emitted on
    # every path — success, cap-hit, DEAD-zone, exception.
    _now_utc = _dt_fs.now(_tz_fs.utc)
    if entry_detected_at is None:
        entry_detected_at = _now_utc
    _detected_iso = entry_detected_at.astimezone(_tz_fs.utc).isoformat()
    _attempt_iso  = _now_utc.isoformat()
    _sid = signal_id or f"{to}|{instrument}|{direction}|{entry:.2f}|{_now_utc.strftime('%Y%m%d%H%M')}"

    _sess = "UNKNOWN"
    _day = _date_fs.today().isoformat()

    # --- Cap / DEAD-zone gate -------------------------------------------------
    try:
        import redis as _r_sync
        # Clean timezone-aware ET conversion. The prior version did
        # utcnow().replace(tzinfo=now().astimezone().tzinfo) which mixes naive
        # UTC with the system TZ; coincidentally OK in a UTC container, but
        # produces wrong session labels anywhere else.
        try:
            import zoneinfo as _zi_fs
            _et = _now_utc.astimezone(_zi_fs.ZoneInfo("America/New_York"))
        except Exception:
            _et = _now_utc
        _t_min = _et.hour * 60 + _et.minute
        if _t_min >= 18*60 or _t_min < 3*60:        _sess = "ASIA"
        elif 3*60 <= _t_min < 9*60:                 _sess = "LONDON"
        elif 9*60+30 <= _t_min < 12*60:             _sess = "NY_AM"
        elif 14*60+30 <= _t_min < 16*60+30:         _sess = "NY_PM"
        else:                                        _sess = "DEAD"
        _rc = _r_sync.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)

        # Layer 0 — per-signal idempotency. If we have already attempted this
        # exact signal row (same DB id) we MUST NOT send again even if the
        # caller fires twice (process restart, retry on partial commit, etc).
        _idemp_key = f"signal_email:idemp:{_sid}"
        if not _rc.set(_idemp_key, _attempt_iso, ex=86400, nx=True):
            _lg.info(
                f"[signal-email-timing] DUPLICATE signal_id={_sid} symbol={instrument} "
                f"strategy={strategy_name} entry_detected_at={_detected_iso} "
                f"sent_attempt_at={_attempt_iso} outcome=duplicate-suppressed"
            )
            return {"sent": False, "provider_status": "duplicate_suppressed", "provider_message_id": None, "error": None, "suppressed": True}

        if _sess == "DEAD":
            _lg.info(
                f"[signal-email-timing] DEAD-ZONE signal_id={_sid} symbol={instrument} "
                f"strategy={strategy_name} to={to} entry_detected_at={_detected_iso} "
                f"sent_attempt_at={_attempt_iso} outcome=dead-zone-suppressed"
            )
            return {"sent": False, "provider_status": "dead_zone_suppressed", "provider_message_id": None, "error": None, "suppressed": True}

        # Layer 1 — one futures email per (user, session, day). First wins.
        _key = f"futures_email:{to}:{_sess}:{_day}"
        if not _rc.set(_key, "1", ex=4*3600, nx=True):
            _lg.info(
                f"[signal-email-timing] CAP-HIT signal_id={_sid} symbol={instrument} "
                f"strategy={strategy_name} to={to} session={_sess} "
                f"entry_detected_at={_detected_iso} sent_attempt_at={_attempt_iso} "
                f"outcome=session-cap"
            )
            return {"sent": False, "provider_status": "session_cap_suppressed", "provider_message_id": None, "error": None, "suppressed": True}
    except Exception as _cap_err:
        # Fail-open is correct — we would rather send than not — but we MUST
        # log it so a Redis outage doesn't go unnoticed. Was: bare pass.
        _lg.warning(
            f"[signal-email-timing] CAP-CHECK-ERROR signal_id={_sid} symbol={instrument} "
            f"strategy={strategy_name} to={to} err={type(_cap_err).__name__}: {str(_cap_err)[:120]} "
            f"-- proceeding with send (fail-open)"
        )
    subject = f"🎯 Theta Scanner (Futures): {side_word} {instrument} @ {entry:.2f} · {strategy_name}"
    html = f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#0f172a;">
      {_logo_header()}
      <h1 style="margin:0 0 4px;font-size:22px;">Position update — {account_label}</h1>
      <p style="margin:0 0 18px;color:#94a3b8;font-size:13px;">{strategy_name} · {fired_at}</p>

      <p style="margin:0 0 18px;font-size:15px;line-height:1.55;color:#0f172a;">
        Theta Algos has logged an internal {side_word.lower()} position in <strong>{instrument}</strong> at <strong>{entry:.2f}</strong>, with stop loss price <strong style="color:#dc2626;">{stop:.2f}</strong> and take profit price <strong style="color:#16a34a;">{target:.2f}</strong>. This message is a record of our system's activity. Whether you replicate any portion of it in your own account is entirely your decision and your responsibility.
      </p>

      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:18px;margin-bottom:14px;">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
          <span style="background:{side_color};color:#fff;font-weight:800;padding:4px 10px;border-radius:8px;font-size:13px;letter-spacing:0.05em;">{side_word}</span>
          <span style="font-weight:800;font-size:20px;color:#0f172a;">{instrument}</span>
        </div>
        <table style="width:100%;font-size:14px;border-collapse:collapse;">
          <tr><td style="padding:6px 0;color:#475569;">Entry price</td><td style="text-align:right;font-weight:700;color:#2563eb;font-size:16px;">{entry:.2f}</td></tr>
          <tr><td style="padding:6px 0;color:#475569;">Stop loss price</td><td style="text-align:right;font-weight:700;color:#dc2626;font-size:16px;">{stop:.2f}</td></tr>
          <tr><td style="padding:6px 0;color:#475569;">Take profit price</td><td style="text-align:right;font-weight:700;color:#16a34a;font-size:16px;">{target:.2f}</td></tr>
          {f'<tr><td style="padding:6px 0;color:#475569;">Bias (HTF)</td><td style="text-align:right;color:#0f172a;text-transform:capitalize;">{bias}</td></tr>' if bias else ''}
        </table>
      </div>

      <hr style="border:none;border-top:1px solid #e2e8f0;margin:18px 0 14px;"/>

      <p style="margin:0;color:#94a3b8;font-size:11px;line-height:1.6;">
        <strong style="color:#64748b;">Disclosure.</strong> This communication is provided for informational and recordkeeping purposes only and reflects automated activity within the proprietary book of <strong>Theta Algos LLC</strong>. It is not, and may not be construed as, investment advice, a recommendation, an endorsement, a solicitation, or an offer to buy, sell, or hold any security, derivative, futures contract, or other financial instrument. Theta Algos LLC is not a registered investment adviser, broker-dealer, commodity trading advisor, or commodity pool operator, and no fiduciary, advisory, or agency relationship is created by your receipt of this message. Any decision to enter, modify, or close a position in your own account based on the information herein is made solely at your own discretion and risk. Trading futures, options, and other leveraged products involves substantial risk of loss and is not suitable for every investor; you may lose more than your initial deposit. Past or hypothetical performance is not indicative of future results.
      </p>
    </div>
    """
    _result = _send_tracked(to, subject, html)
    _ok = bool(_result.get("sent"))
    _sent_iso = _dt_fs.now(_tz_fs.utc).isoformat()
    _latency_ms = int((_dt_fs.now(_tz_fs.utc) - entry_detected_at).total_seconds() * 1000) if entry_detected_at else 0
    _lg.info(
        f"[signal-email-timing] signal_id={_sid} symbol={instrument} "
        f"strategy={strategy_name} direction={direction} entry={entry:.2f} to={to} "
        f"session={_sess} entry_detected_at={_detected_iso} email_sent_at={_sent_iso} "
        f"latency_ms={_latency_ms} outcome={('sent' if _ok else 'send-failed')}"
    )
    return {
        "sent": _ok,
        "provider_message_id": _result.get("provider_message_id"),
        "provider_status": _result.get("provider_status"),
        "error": _result.get("error"),
        "suppressed": False,
        "provider_sent_at": _sent_iso if _ok else None,
    }
"""Patch — add device-registration + push helpers. Append to account_signals.py."""


# ─── Device registration (called by the mobile app) ──────────────────

class DeviceRegister(BaseModel):
    token: str
    platform: str  # 'ios' | 'android'
    device_name: Optional[str] = None


@router.post("/devices/register")
async def register_device(
    data: DeviceRegister,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mobile app calls this on first launch (and on token refresh) so the
    server knows where to send push notifications. Idempotent — same token
    twice updates the timestamp rather than creating a duplicate row."""
    if data.platform not in ("ios", "android"):
        raise HTTPException(status_code=400, detail="platform must be 'ios' or 'android'.")
    did = uuid.uuid4()
    now = datetime.now(timezone.utc)
    await db.execute(text("""
        INSERT INTO user_device_tokens (id, user_id, token, platform, device_name, registered_at, last_used_at)
        VALUES (:id, :uid, :tok, :plat, :name, :now, :now)
        ON CONFLICT (token) DO UPDATE SET
            user_id = EXCLUDED.user_id,
            platform = EXCLUDED.platform,
            device_name = EXCLUDED.device_name,
            last_used_at = EXCLUDED.last_used_at
    """), {
        "id": str(did), "uid": str(current_user.id), "tok": data.token,
        "plat": data.platform, "name": data.device_name, "now": now,
    })
    await db.commit()
    return {"status": "ok"}


@router.delete("/devices/{token}")
async def deregister_device(
    token: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Called when the user signs out of the mobile app — tears down the
    registration so signals stop pushing to that device."""
    await db.execute(text(
        "DELETE FROM user_device_tokens WHERE token = :tok AND user_id = :uid"
    ), {"tok": token, "uid": str(current_user.id)})
    await db.commit()
    return {"status": "ok"}


async def send_push_to_user(user_id: str, title: str, body: str, data: Optional[dict] = None) -> int:
    """Send a push notification to every device the user has registered.
    Returns the number of devices that received the push successfully.

    Currently a stub — once FCM/APNS keys are configured we slot the actual
    HTTP/2 calls in here. For now it logs the intent so the channel works
    end-to-end (UI → DB → runner → here) and we can validate behaviour."""
    from app.database import async_session_factory
    async with async_session_factory() as db:
        rows = await db.execute(text(
            "SELECT token, platform FROM user_device_tokens WHERE user_id = :uid"
        ), {"uid": str(user_id)})
        devices = rows.fetchall()
    if not devices:
        logger.info(f"[Push] No registered devices for user {user_id} — skipping push")
        return 0
    # TODO: replace with real Firebase / APNS HTTP/2 send once keys are in.
    # firebase_admin.messaging.send_multicast(...) or aioapns send_message(...)
    for d in devices:
        logger.info(f"[Push] (stub) -> {d.platform} | {title} | {body}")
    return len(devices)


# ────────────────────────────────────────────────────────────────────────
# Signal outcome resolution
# Walks unresolved signals (outcome IS NULL) and checks the price history
# to see if TP or SL was hit. Marks win/loss/expired accordingly.
# ────────────────────────────────────────────────────────────────────────
from datetime import datetime, timezone, timedelta


async def _resolve_signal_outcomes(db, max_age_hours: int = 168):
    """Resolve any unresolved signals older than 30 minutes by checking
    intraday candles against entry/stop/target. Signals older than
    max_age_hours (default 7 days) that never hit either get marked 'expired'."""
    from sqlalchemy import text as _text
    rows = (await db.execute(_text("""
        SELECT id, instrument, direction, entry_price, stop_loss, take_profit, fired_at
          FROM account_signals
         WHERE outcome IS NULL
           AND fired_at < NOW() - INTERVAL '30 minutes'
           AND fired_at > NOW() - (INTERVAL '1 hour' * :max_age_hours)
         ORDER BY fired_at ASC
         LIMIT 50
    """), {"max_age_hours": max_age_hours})).fetchall()

    if not rows:
        return 0

    from app.engines.data_feeds.polygon_feed import fetch_polygon_data
    resolved = 0
    now = datetime.now(timezone.utc)

    for r in rows:
        try:
            fired_at = r.fired_at if r.fired_at.tzinfo else r.fired_at.replace(tzinfo=timezone.utc)
            df = await fetch_polygon_data(
                instrument=r.instrument,
                start_date=fired_at,
                end_date=now,
                interval="5m",
            )
            if df is None or len(df) == 0:
                continue
            entry = float(r.entry_price); stop = float(r.stop_loss); target = float(r.take_profit)
            risk = abs(entry - stop)
            outcome = None; outcome_price = None; outcome_r = None
            for ts, row in df.iterrows():
                hi, lo = float(row["high"]), float(row["low"])
                if r.direction == "long":
                    if lo <= stop:
                        outcome, outcome_price, outcome_r = "loss", stop, -1.0; break
                    if hi >= target:
                        outcome, outcome_price = "win", target
                        outcome_r = abs(target - entry) / risk if risk > 0 else 0
                        break
                else:  # short
                    if hi >= stop:
                        outcome, outcome_price, outcome_r = "loss", stop, -1.0; break
                    if lo <= target:
                        outcome, outcome_price = "win", target
                        outcome_r = abs(entry - target) / risk if risk > 0 else 0
                        break
            # If signal is older than max_age and never hit, mark expired
            if outcome is None and (now - fired_at) > timedelta(hours=max_age_hours):
                outcome = "expired"
            if outcome:
                await db.execute(_text("""
                    UPDATE account_signals
                       SET outcome = :o, outcome_price = :op, outcome_r = :or_,
                           resolved_at = NOW()
                     WHERE id = :id
                """), {"o": outcome, "op": outcome_price, "or_": outcome_r, "id": str(r.id)})
                resolved += 1
        except Exception as e:
            logger.warning(f"[signals] resolve failed for {r.id}: {e}")
    if resolved > 0:
        await db.commit()
        logger.info(f"[signals] resolved {resolved} signals")
    return resolved


@router.get("/stats")
async def signals_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resolve any pending outcomes and return aggregate winrate stats."""
    # Trigger a resolution pass (best-effort, time-bounded by SQL LIMIT 50)
    try:
        await _resolve_signal_outcomes(db)
    except Exception as e:
        logger.warning(f"[signals] stats resolve pass failed: {e}")

    rows = (await db.execute(text("""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE outcome = 'win') AS wins,
            COUNT(*) FILTER (WHERE outcome = 'loss') AS losses,
            COUNT(*) FILTER (WHERE outcome = 'expired') AS expired,
            COUNT(*) FILTER (WHERE outcome IS NULL) AS pending,
            COALESCE(SUM(outcome_r) FILTER (WHERE outcome IN ('win','loss')), 0) AS total_r,
            COALESCE(AVG(outcome_r) FILTER (WHERE outcome IN ('win','loss')), 0) AS avg_r
          FROM account_signals
         WHERE user_id = :uid
    """), {"uid": str(current_user.id)})).first()

    total = int(rows.total or 0)
    wins = int(rows.wins or 0)
    losses = int(rows.losses or 0)
    resolved = wins + losses
    win_rate = (wins / resolved * 100.0) if resolved > 0 else 0.0
    return {
        "total": total, "wins": wins, "losses": losses,
        "expired": int(rows.expired or 0), "pending": int(rows.pending or 0),
        "resolved": resolved,
        "win_rate": round(win_rate, 1),
        "total_r": round(float(rows.total_r or 0), 2),
        "avg_r": round(float(rows.avg_r or 0), 2),
    }
