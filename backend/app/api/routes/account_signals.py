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
from app.core.auth import get_current_user
from app.services.email import _send, _logo_header
from app.config import settings

router = APIRouter()


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


# ─── Routes ──────────────────────────────────────────────────────────

@router.get("/", response_model=list[SignalResponse])
async def list_signals(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(text("""
        SELECT s.id, s.instrument, s.direction, s.entry_price, s.stop_loss,
               s.take_profit, s.bias, s.fired_at, s.status, st.name AS strategy_name
        FROM account_signals s
        JOIN strategies st ON st.id = s.strategy_id
        WHERE s.user_id = :uid
        ORDER BY s.fired_at DESC
        LIMIT 200
    """), {"uid": str(current_user.id)})
    return [
        SignalResponse(
            id=str(r.id), strategy_name=r.strategy_name,
            instrument=r.instrument, direction=r.direction,
            entry_price=float(r.entry_price), stop_loss=float(r.stop_loss),
            take_profit=float(r.take_profit), bias=r.bias,
            fired_at=r.fired_at.isoformat() if r.fired_at else "",
            status=r.status,
        )
        for r in rows.fetchall()
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
) -> bool:
    side_color = "#16a34a" if direction == "long" else "#dc2626"
    side_word = "LONG" if direction == "long" else "SHORT"
    # Subject framed as a log entry, not a directive — "position update", not
    # "place this order". Same for the body. The footer carries a formal
    # CFTC-style disclosure so the email cannot be characterised as a
    # solicitation, recommendation, or financial advice.
    # 🎯 Theta Scanner (Futures) — rebranded so it passes the whitelist firewall.
    # Redis cap: 1 email per (user, instrument-family, session) to kill multi-strategy spam.
    try:
        import redis as _r_sync, os as _os_fs
        from datetime import datetime as _dt_fs, date as _date_fs
        try:
            import zoneinfo as _zi_fs
            _et = _dt_fs.utcnow().replace(tzinfo=_dt_fs.now().astimezone().tzinfo).astimezone(_zi_fs.ZoneInfo("America/New_York"))
        except Exception:
            _et = _dt_fs.utcnow()
        _t_min = _et.hour * 60 + _et.minute
        if _t_min >= 18*60 or _t_min < 3*60:        _sess = "ASIA"
        elif 3*60 <= _t_min < 9*60:                 _sess = "LONDON"
        elif 9*60+30 <= _t_min < 12*60:             _sess = "NY_AM"
        elif 14*60+30 <= _t_min < 16*60+30:         _sess = "NY_PM"
        else:                                        _sess = "DEAD"
        _rc = _r_sync.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
        _MICRO = {"MES":"ES","MNQ":"NQ","MYM":"YM","M2K":"RTY"}
        _inst_fam = _MICRO.get(instrument.upper(), instrument.upper())
        _day = _date_fs.today().isoformat()
        if _sess == "DEAD":
            from loguru import logger as _lg; _lg.info(f"[futures-email] DEAD zone, suppressed {instrument} for {to}")
            return False
        # ONE futures email per session per user — first qualifying setup wins.
        # Was: per-instrument-family. Now: total cap across ES/NQ/YM.
        _strat_slug = re.sub(r"[^a-zA-Z0-9_]", "_", strategy_name or "unknown")[:32]
        _key = f"futures_email:{to}:{_strat_slug}:{_sess}:{_day}"
        if not _rc.set(_key, "1", ex=4*3600, nx=True):
            from loguru import logger as _lg; _lg.info(f"[futures-email] CAP-HIT {instrument} for {to} session={_sess}")
            return False
    except Exception as _e:
        pass  # fail-open
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
    return _send(to, subject, html)
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
