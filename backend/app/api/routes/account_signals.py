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

from fastapi import APIRouter, Depends, HTTPException, status, Request
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


# Track whether we have already run the idempotent chart-column migration this
# process so we don't issue the ALTER on every list request.
_chart_cols_ensured = False


async def _ensure_chart_columns(db) -> None:
    """Idempotently add the `chart_b64 TEXT` column to both signal-history
    tables. Stores the annotated trade-chart PNG (base64) alongside each
    signal so the Email Signals page can render it inline. Safe to call
    repeatedly — ADD COLUMN IF NOT EXISTS is a no-op once the column exists,
    and we additionally short-circuit after the first success per process.
    Never raises: a migration hiccup must not break the signal feed."""
    global _chart_cols_ensured
    if _chart_cols_ensured:
        return
    try:
        for tbl in ("account_signals", "email_signals_history"):
            await db.execute(text(
                f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS chart_b64 TEXT"
            ))
            # Human-readable level reasons shown next to the stop/target
            # price (e.g. "swing low", "London high"). Added alongside
            # chart_b64 so a single migration pass covers both.
            await db.execute(text(
                f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS stop_reason TEXT"
            ))
            await db.execute(text(
                f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS target_reason TEXT"
            ))
        await db.commit()
        _chart_cols_ensured = True
    except Exception as _e:
        logger.warning(f"[signals] _ensure_chart_columns failed (non-fatal): {_e}")


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
    # Annotated trade-chart PNG (base64) for inline render in the expanded row.
    chart_b64: Optional[str] = None
    # Human-readable level reasons (e.g. "swing low", "London high")
    # shown next to the stop/target price. Never blank — the producer
    # falls back to "strategy stop"/"strategy target".
    stop_reason: Optional[str] = None
    target_reason: Optional[str] = None


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
    await _ensure_chart_columns(db)
    if include_suppressed:
        sql = """
            SELECT s.id, s.instrument, s.direction, s.entry_price, s.stop_loss,
                   s.take_profit, s.bias, s.fired_at, s.status,
                   s.outcome, s.outcome_price, s.outcome_r, s.resolved_at,
                   s.provider_status, s.error_message,
                   s.duplicate_suppressed_at, s.duplicate_suppressed_count,
                   s.chart_b64, s.stop_reason, s.target_reason,
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
                   s.chart_b64, s.stop_reason, s.target_reason,
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
            chart_b64=getattr(r, "chart_b64", None),
            stop_reason=getattr(r, "stop_reason", None),
            target_reason=getattr(r, "target_reason", None),
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
    await _ensure_chart_columns(db)
    rows = (await db.execute(text("""
        SELECT s.id, s.instrument, s.direction, s.entry_price, s.stop_loss,
               s.take_profit, s.bias, s.fired_at, s.status,
               s.outcome, s.outcome_price, s.outcome_r, s.resolved_at,
               s.duplicate_suppressed_at, s.duplicate_suppressed_count,
               s.error_message,
               s.chart_b64, s.stop_reason, s.target_reason,
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
            chart_b64=getattr(r, "chart_b64", None),
            stop_reason=getattr(r, "stop_reason", None),
            target_reason=getattr(r, "target_reason", None),
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
    stop_reason: Optional[str] = None,
    target_reason: Optional[str] = None,
    price_basis: Optional[str] = None,
) -> bool:
    """Send a futures entry-signal email.

    signal_id (recommended): used to build an idempotency key in Redis so the
    same DB signal row can never trigger a duplicate email even if the watcher
    is restarted mid-flight. If omitted, falls back to (to, inst, dir, entry,
    minute) which is also stable per minute.

    entry_detected_at (recommended): UTC timestamp from the moment the strategy
    confirmed the entry. Logged in the [signal-email-timing] line so the gap
    between detection and send is queryable.

    stop_reason / target_reason (LABEL-TRUTH-V1): when the STRATEGY provides
    them (generated from the branch that actually chose each level) they are
    used VERBATIM — the post-hoc level inference below runs only as a fallback
    for callers that cannot supply them. Post-hoc guessing is what fabricated
    impossible labels (a short's stop attributed to a session VWAP sitting
    below entry).

    price_basis (recommended for futures): human note of the bar source the
    signal priced from (e.g. 'live QQQ IEX proxy scaled to NQ (Alpaca)'),
    rendered in the email so overnight recipients can judge price relevance.
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

    # Session + session-anchored trading day (DUP-SEND FIX): computed by the
    # shared pure helper. The day used to be the server's UTC calendar date,
    # so the ASIA session (18:00 ET spans UTC midnight) minted a fresh
    # one-email-per-session cap key at 00:00 UTC and the same user could get
    # the same ASIA setup twice in one evening (observed 2026-07-05: 18:33 ET
    # and 20:40 ET sends). The helper anchors the day to the SESSION START in
    # ET, so one session == one cap key. Never raises.
    from app.engines.account_signals.signal_guard import email_session_and_day as _sess_day
    _sess, _day = _sess_day(_now_utc)

    # --- Cap / DEAD-zone gate -------------------------------------------------
    try:
        import redis as _r_sync
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
        # TTL must OUTLIVE the longest session (ASIA runs 9h, 18:00-03:00 ET)
        # or the cap re-opens mid-session; 4h left exactly that hole. The key
        # embeds the session-anchored day, so a long TTL cannot collide with
        # the next day's session.
        _key = f"futures_email:{to}:{_sess}:{_day}"
        if not _rc.set(_key, "1", ex=10*3600, nx=True):
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
    # ── Annotated trade-chart PNG (best-effort) ──────────────────────────
    # Pull recent bars via the SAME data path the watcher uses
    # (_fetch_bars_sync → Polygon ETF proxy for futures → candle_cache →
    # yfinance), render the TradingView-style position chart, attach it inline
    # (<img src="cid:tradechart">) and stash base64 for the Email Signals page.
    # Any failure (no bars, bad geometry, matplotlib hiccup) degrades to a
    # no-chart email — a chart must NEVER block the signal.
    # ── Level reasons: strategy-provided first, inference only as fallback ──
    # LABEL-TRUTH-V1: when the strategy passed stop_reason/target_reason they
    # describe the level ACTUALLY chosen (sweep extreme / swing / FVG-with-TF /
    # R:R fallback) and are used verbatim. Only when a caller could not supply
    # them do we fall back to the old post-hoc inference — now side-sane and
    # never labelling a stop with VWAP (see level_reasons.py).
    _strat_sr = (stop_reason or "").strip() or None
    _strat_tr = (target_reason or "").strip() or None
    stop_reason = _strat_sr or "strategy stop"
    target_reason = _strat_tr or "strategy target"
    if _strat_sr and _strat_tr:
        _lg.info(
            f"[signal-email] level-reasons (strategy-provided) sym={instrument} "
            f"dir={direction} stop={stop} ({stop_reason}) target={target} ({target_reason})"
        )
    else:
        try:
            from app.engines.account_signals.runner import _fetch_bars_sync as _fb_lr
            from app.engines.level_reasons import infer_stop_target_reasons as _infer_lr
            import pandas as _pd_lr
            _lr_bars = _fb_lr(instrument, "5m", 60) or []
            _lr_df = _pd_lr.DataFrame(_lr_bars) if _lr_bars else None
            _reasons = _infer_lr(
                direction=direction, entry=entry, stop=stop, target=target,
                bars_df=_lr_df, instrument=instrument, now_utc=_now_utc,
                bars_tf_label="5m",
            )
            stop_reason = _strat_sr or _reasons.get("stop_reason") or stop_reason
            target_reason = _strat_tr or _reasons.get("target_reason") or target_reason
            _lg.info(
                f"[signal-email] level-reasons (inferred fallback) sym={instrument} "
                f"dir={direction} stop={stop} ({stop_reason}) target={target} ({target_reason})"
            )
        except Exception as _lr_e:
            _lg.warning(f"[signal-email] reason inference errored sym={instrument}: {type(_lr_e).__name__}: {_lr_e}")
    _chart_png = None
    _chart_b64 = None
    _chart_img_html = ""
    try:
        from app.engines.account_signals.runner import _fetch_bars_sync
        from app.services.trade_chart import generate_trade_chart
        import pandas as _pd_ch
        _tf = "5m"
        _bars = _fetch_bars_sync(instrument, _tf, 50) or []
        _bars_df = None
        if _bars:
            _bars_df = _pd_ch.DataFrame(_bars)
        _chart_png = generate_trade_chart(
            symbol=instrument, timeframe=_tf, bars_df=_bars_df,
            entry=entry, stop=stop, target=target, direction=direction,
            key_levels=None,
            stop_reason=stop_reason, target_reason=target_reason,
        )
    except Exception as _ch_e:
        _lg.warning(f"[signal-email] chart gen errored sym={instrument}: {type(_ch_e).__name__}: {_ch_e}")
        _chart_png = None
    if _chart_png:
        import base64 as _b64_ch
        _chart_b64 = _b64_ch.b64encode(_chart_png).decode()
        _chart_img_html = (
            '<img src="cid:tradechart" alt="trade setup" '
            'style="display:block;width:100%;max-width:520px;border-radius:12px;'
            'border:1px solid #e2e8f0;margin:0 0 14px;"/>'
        )
    else:
        _lg.info(f"[signal-email] chart skipped (invalid geometry) sym={instrument} dir={direction} e={entry} s={stop} t={target}")
    # Price-basis note: which bar source priced this signal (proxy vs real
    # futures) — critical context overnight when ETF proxies can go stale.
    _basis_html = (
        f'<p style="margin:-6px 0 14px;color:#94a3b8;font-size:11px;line-height:1.5;">'
        f'Price basis: {price_basis}</p>'
        if price_basis else ""
    )
    subject = f"🎯 Saro (Futures): {side_word} {instrument} @ {entry:.2f} · {strategy_name}"
    html = f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#0f172a;">
      {_logo_header()}
      <h1 style="margin:0 0 4px;font-size:22px;">Position update — {account_label}</h1>
      <p style="margin:0 0 18px;color:#94a3b8;font-size:13px;">{strategy_name} · {fired_at}</p>

      <p style="margin:0 0 18px;font-size:15px;line-height:1.55;color:#0f172a;">
        Theta Algos has logged an internal {side_word.lower()} position in <strong>{instrument}</strong> at <strong>{entry:.2f}</strong>, with stop loss price <strong style="color:#dc2626;">{stop:.2f}</strong> ({stop_reason}) and take profit price <strong style="color:#16a34a;">{target:.2f}</strong> ({target_reason}). This message is a record of our system's activity. Whether you replicate any portion of it in your own account is entirely your decision and your responsibility.
      </p>

      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:18px;margin-bottom:14px;">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
          <span style="background:{side_color};color:#fff;font-weight:800;padding:4px 10px;border-radius:8px;font-size:13px;letter-spacing:0.05em;">{side_word}</span>
          <span style="font-weight:800;font-size:20px;color:#0f172a;">{instrument}</span>
        </div>
        <table style="width:100%;font-size:14px;border-collapse:collapse;">
          <tr><td style="padding:6px 0;color:#475569;">Entry price</td><td style="text-align:right;font-weight:700;color:#2563eb;font-size:16px;">{entry:.2f}</td></tr>
          <tr><td style="padding:6px 0;color:#475569;">Stop loss price</td><td style="text-align:right;font-weight:700;color:#dc2626;font-size:16px;">{stop:.2f} <span style="color:#94a3b8;font-weight:600;font-size:12px;">({stop_reason})</span></td></tr>
          <tr><td style="padding:6px 0;color:#475569;">Take profit price</td><td style="text-align:right;font-weight:700;color:#16a34a;font-size:16px;">{target:.2f} <span style="color:#94a3b8;font-weight:600;font-size:12px;">({target_reason})</span></td></tr>
          {f'<tr><td style="padding:6px 0;color:#475569;">Bias (HTF)</td><td style="text-align:right;color:#0f172a;text-transform:capitalize;">{bias}</td></tr>' if bias else ''}
        </table>
      </div>

      {_basis_html}
      {_chart_img_html}

      <a href="{settings.FRONTEND_URL}/app/signals/{_sid}/review" style="display:inline-block;background:#2563eb;color:#fff;text-decoration:none;font-weight:700;padding:11px 20px;border-radius:10px;font-size:14px;margin:4px 0 10px;">Review &amp; approve in app &rarr;</a>
      <p style="margin:0 0 6px;color:#94a3b8;font-size:12px;line-height:1.5;">Open in the app to approve or decline this trade idea. Nothing is placed in your account unless you approve it and your plan permits placement.</p>

      <hr style="border:none;border-top:1px solid #e2e8f0;margin:18px 0 14px;"/>

      <p style="margin:0;color:#94a3b8;font-size:11px;line-height:1.6;">
        <strong style="color:#64748b;">Disclosure.</strong> This communication is provided for informational and recordkeeping purposes only and reflects automated activity within the proprietary book of <strong>Theta Algos LLC</strong>. It is not, and may not be construed as, investment advice, a recommendation, an endorsement, a solicitation, or an offer to buy, sell, or hold any security, derivative, futures contract, or other financial instrument. Theta Algos LLC is not a registered investment adviser, broker-dealer, commodity trading advisor, or commodity pool operator, and no fiduciary, advisory, or agency relationship is created by your receipt of this message. Any decision to enter, modify, or close a position in your own account based on the information herein is made solely at your own discretion and risk. Trading futures, options, and other leveraged products involves substantial risk of loss and is not suitable for every investor; you may lose more than your initial deposit. Past or hypothetical performance is not indicative of future results.
      </p>
    </div>
    """
    _result = _send_tracked(to, subject, html, inline_png=_chart_png)
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
        "chart_b64": _chart_b64,
        "stop_reason": stop_reason,
        "target_reason": target_reason,
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


async def _resolve_signal_outcomes(db, max_age_hours: int = 168, limit: int = 200):
    """EMAIL-SIGNALS-RESOLVER-V3. Resolve unresolved SENT signals (>30 min old)
    against REAL-FUTURES 1m candle_cache (the SAME source the emitter + backtest
    use). The old resolver used a drifting ETF-proxy scale that mis-aligned the
    candles vs the emitted price (10-330 pts vs 3-50 pt stops), flipping outcomes.
    It also ignored BREAK-EVEN: a trade that reaches +1R then retraces to entry is
    a 'breakeven' (not a loss), exactly as the strategies + backtest book it.
    Outcomes: win (target) / breakeven (+1R then stop) / loss (stop, never +1R) /
    expired / needs_review. Suppressed rows are never resolved."""
    from sqlalchemy import text as _text
    rows = (await db.execute(_text("""
        SELECT id, instrument, direction, entry_price, stop_loss, take_profit, fired_at
          FROM account_signals
         WHERE outcome IS NULL AND status = 'sent'
           AND fired_at < NOW() - INTERVAL '30 minutes'
         ORDER BY fired_at ASC LIMIT :limit
    """), {"limit": limit})).fetchall()
    if not rows:
        return 0
    from app.engines.data_feeds.local_cache import fetch_from_cache
    from app.engines.data_feeds.polygon_feed import fetch_polygon_data
    resolved = 0
    now = datetime.now(timezone.utc)
    for r in rows:
        outcome = None; outcome_price = None; outcome_r = None; reason = None; had_data = False; src = None
        try:
            fired_at = r.fired_at if r.fired_at.tzinfo else r.fired_at.replace(tzinfo=timezone.utc)
            age_h = (now - fired_at).total_seconds() / 3600.0
            # PRIMARY: real-futures 1m candle_cache (24h coverage, correct scale).
            df = None
            try:
                df = await fetch_from_cache(r.instrument, fired_at, now, "1m")
                if df is not None and len(df) > 0:
                    src = "candle_cache"
            except Exception:
                df = None
            if df is None or len(df) == 0:
                try:
                    df = await fetch_polygon_data(instrument=r.instrument, start_date=fired_at, end_date=now, interval="1m")
                    if df is not None and len(df) > 0:
                        src = "proxy_fallback"
                except Exception as _fe:
                    df = None
                    logger.warning(f"[signals] resolve price-fetch failed id={r.id} {r.instrument}: {_fe}")
            if df is not None and len(df) > 0:
                had_data = True
                entry = float(r.entry_price); stop = float(r.stop_loss); target = float(r.take_profit)
                risk = abs(entry - stop)
                be_trigger = (entry + risk) if r.direction == "long" else (entry - risk)   # +1R
                reached_be = False
                for _ts, _row in df.iterrows():
                    hi = float(_row["high"]); lo = float(_row["low"])
                    if r.direction == "long":
                        if hi >= be_trigger:
                            reached_be = True
                        hit_t = hi >= target; hit_s = lo <= stop
                    else:
                        if lo <= be_trigger:
                            reached_be = True
                        hit_t = lo <= target; hit_s = hi >= stop
                    if hit_t and hit_s:
                        outcome, outcome_price, outcome_r, reason = "breakeven", entry, 0.0, "ambiguous_bar"; break
                    if hit_t:
                        _raw = abs(target - entry) / risk if risk > 0 else 0
                        if _raw > 50:
                            outcome, reason = "data_error", f"r_explosion_{_raw:.0f}"
                            logger.warning(f"[signals] data-error id={r.id} R={_raw:.1f} (e={entry} s={stop} t={target})"); break
                        outcome, outcome_price, outcome_r, reason = "win", target, _raw, "target_hit"; break
                    if hit_s:
                        if reached_be:
                            outcome, outcome_price, outcome_r, reason = "breakeven", entry, 0.0, "breakeven"
                        else:
                            outcome, outcome_price, outcome_r, reason = "loss", stop, -1.0, "stop_hit"
                        break
            if outcome is None and age_h > max_age_hours:
                if had_data:
                    outcome, reason = "expired", f"no_hit_after_{int(age_h)}h"
                else:
                    outcome, reason = "needs_review", "no_price_data"
            logger.info(f"[signals] resolve id={r.id} {r.instrument} {r.direction} age={age_h:.1f}h "
                        f"src={src} had_data={had_data} -> {outcome or 'still_pending'} ({reason or '-'})")
            if outcome:
                await db.execute(_text("""
                    UPDATE account_signals
                       SET outcome = :o, outcome_price = :op, outcome_r = :or_,
                           outcome_reason = :rs, resolved_at = NOW()
                     WHERE id = :id
                """), {"o": outcome, "op": outcome_price, "or_": outcome_r, "rs": reason, "id": str(r.id)})
                resolved += 1
        except Exception as e:
            logger.warning(f"[signals] resolve failed for {r.id}: {e}")
    if resolved > 0:
        await db.commit()
        logger.info(f"[signals] resolved {resolved}/{len(rows)} pending sent signals this pass")
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

    # R:R aggregation guard: exclude |outcome_r| > 20 from totals/averages so a
    # single bad signal (e.g. near-zero risk → exploded R) can't poison the avg.
    # 20R is already extreme — real-world R:R signals top out around 10-15. The
    # excluded_outliers count is surfaced to the UI so users see when this fires.
    BAD_R_THRESHOLD = 20.0
    rows = (await db.execute(text("""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE outcome = 'win') AS wins,
            COUNT(*) FILTER (WHERE outcome = 'loss') AS losses,
            COUNT(*) FILTER (WHERE outcome = 'breakeven') AS breakeven,
            COUNT(*) FILTER (WHERE outcome = 'expired') AS expired,
            COUNT(*) FILTER (WHERE outcome IS NULL) AS pending,
            COALESCE(SUM(outcome_r) FILTER (WHERE outcome IN ('win','loss')
                                              AND ABS(outcome_r) <= :rmax), 0) AS total_r,
            COALESCE(AVG(outcome_r) FILTER (WHERE outcome IN ('win','loss')
                                              AND ABS(outcome_r) <= :rmax), 0) AS avg_r,
            COUNT(*) FILTER (WHERE outcome IN ('win','loss')
                              AND ABS(outcome_r) > :rmax) AS excluded_outliers
          FROM account_signals
         WHERE user_id = :uid AND status = 'sent'
    """), {"uid": str(current_user.id), "rmax": BAD_R_THRESHOLD})).first()
    if rows and int(rows.excluded_outliers or 0) > 0:
        logger.warning(f"[signals/stats] user={current_user.email} excluded {rows.excluded_outliers} outlier signals with |outcome_r|>{BAD_R_THRESHOLD} from aggregates")

    total = int(rows.total or 0)
    wins = int(rows.wins or 0)
    losses = int(rows.losses or 0)
    breakeven = int(getattr(rows, "breakeven", 0) or 0)
    # Decided trades include break-even. win_rate counts BE as "not a loss"
    # (matches the backtest is_winner); effective_win_rate excludes BE.
    resolved = wins + losses + breakeven
    win_rate = ((wins + breakeven) / resolved * 100.0) if resolved > 0 else 0.0
    effective_win_rate = (wins / (wins + losses) * 100.0) if (wins + losses) > 0 else 0.0
    return {
        "total": total, "wins": wins, "losses": losses, "breakeven": breakeven,
        "effective_win_rate": round(effective_win_rate, 1),
        "expired": int(rows.expired or 0), "pending": int(rows.pending or 0),
        "resolved": resolved,
        "win_rate": round(win_rate, 1),
        "total_r": round(float(rows.total_r or 0), 2),
        "avg_r": round(float(rows.avg_r or 0), 2),
        "excluded_outliers": int(rows.excluded_outliers or 0),
    }


@router.get("/resolution-report")
async def resolution_report(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin: how SENT Email Signals have resolved + how many are still pending,
    by age. Surfaces whether the backlog is being cleared."""
    tier = current_user.subscription_tier.value if hasattr(current_user.subscription_tier, "value") else str(current_user.subscription_tier)
    if tier != "tier_5":
        raise HTTPException(status_code=403, detail="Admin only.")
    from sqlalchemy import text as _text
    by = (await db.execute(_text("""
        SELECT COALESCE(outcome, 'pending') AS o, count(*) AS n
          FROM account_signals WHERE status = 'sent' GROUP BY 1 ORDER BY 2 DESC
    """))).fetchall()
    age = (await db.execute(_text("""
        SELECT count(*) FILTER (WHERE fired_at <  now() - interval '7 days') AS gt7d,
               count(*) FILTER (WHERE fired_at >= now() - interval '7 days') AS le7d,
               min(fired_at) AS oldest
          FROM account_signals WHERE status = 'sent' AND outcome IS NULL
    """))).first()
    last = (await db.execute(_text("""
        SELECT max(resolved_at) AS last_resolved,
               count(*) FILTER (WHERE resolved_at > now() - interval '24 hours') AS last24h
          FROM account_signals WHERE status = 'sent'
    """))).first()
    return {
        "sent_by_outcome": {row.o: int(row.n) for row in by},
        "pending_over_7d": int(age.gt7d or 0),
        "pending_under_7d": int(age.le7d or 0),
        "oldest_pending": age.oldest.isoformat() if age.oldest else None,
        "last_resolved_at": last.last_resolved.isoformat() if last and last.last_resolved else None,
        "resolved_last_24h": int(last.last24h or 0) if last else 0,
    }


# ── PHASE-F-APPROVE-DECLINE: non-automated users review trade ideas ──────────

def _signal_to_tradesignal(row):
    from app.engines.strategy_engine.base_strategy import TradeSignal, SignalType
    return TradeSignal(
        signal=SignalType.LONG if row.direction == "long" else SignalType.SHORT,
        instrument=row.instrument, entry_price=float(row.entry_price),
        stop_loss=float(row.stop_loss), take_profit=float(row.take_profit), contracts=1,
    )


@router.get("/{signal_id}/review")
async def get_signal_for_review(signal_id: str, current_user: User = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)):
    """Detail for the in-app review page (owner-only). Powers the approve/decline UI."""
    from sqlalchemy import text as _t
    r = (await db.execute(_t("""
        SELECT id, instrument, direction, entry_price, stop_loss, take_profit, bias,
               fired_at, status, outcome, decision, decided_at, placed_ref
          FROM account_signals WHERE id = :id AND user_id = :uid
    """), {"id": signal_id, "uid": str(current_user.id)})).first()
    if not r:
        raise HTTPException(status_code=404, detail="Signal not found.")
    from app.core.packages import requires_manual_approval, can_place_on_approval
    return {
        "id": str(r.id), "instrument": r.instrument, "direction": r.direction,
        "entry_price": float(r.entry_price), "stop_loss": float(r.stop_loss),
        "take_profit": float(r.take_profit), "bias": r.bias,
        "fired_at": r.fired_at.isoformat() if r.fired_at else None,
        "status": r.status, "outcome": r.outcome, "decision": r.decision,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        "placed_ref": r.placed_ref,
        "requires_manual_approval": requires_manual_approval(current_user),
        "can_place_on_approval": can_place_on_approval(current_user),
    }


async def _record_decision(db, signal_id, current_user, request, decision: str):
    from sqlalchemy import text as _t
    r = (await db.execute(_t("""
        SELECT id, instrument, direction, entry_price, stop_loss, take_profit, strategy_id, decision
          FROM account_signals WHERE id = :id AND user_id = :uid AND status = 'sent'
    """), {"id": signal_id, "uid": str(current_user.id)})).first()
    if not r:
        raise HTTPException(status_code=404, detail="Signal not found.")
    if r.decision:
        raise HTTPException(status_code=409, detail=f"Already {r.decision}.")
    ip = request.client.host if request and request.client else None
    ua = request.headers.get("user-agent") if request else None
    await db.execute(_t("""
        UPDATE account_signals SET decision = :d, decided_at = NOW(), decided_by = :uid,
               decided_via = 'app', decision_ip = :ip, decision_user_agent = :ua
         WHERE id = :id
    """), {"d": decision, "uid": str(current_user.id), "ip": ip, "ua": ua, "id": signal_id})
    return r, ip, ua


@router.post("/{signal_id}/decline")
async def decline_signal(signal_id: str, request: Request,
                         current_user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    """Decline a trade idea — recorded + audited, NO trade is placed."""
    from app.api.routes.security import audit_log, EVENT_TRADE_DECLINED
    r, ip, ua = await _record_decision(db, signal_id, current_user, request, "declined")
    await audit_log(db, current_user.id, EVENT_TRADE_DECLINED,
                    {"signal_id": signal_id, "instrument": r.instrument}, request)
    await db.commit()
    logger.info(f"[signal-decision] DECLINED signal={signal_id} user={current_user.id}")
    return {"signal_id": signal_id, "decision": "declined", "placed": False}


@router.post("/{signal_id}/approve")
async def approve_signal(signal_id: str, request: Request,
                         current_user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    """Approve a trade idea — recorded + audited. The trade is placed ONLY if the
    tier may place on approval AND an eligible active session can take it; the
    exact result (or why not) is recorded in placed_ref + the audit log."""
    from app.api.routes.security import audit_log, EVENT_TRADE_APPROVED
    from app.core.packages import can_place_on_approval
    r, ip, ua = await _record_decision(db, signal_id, current_user, request, "approved")
    placed_ref = "approved"
    if not can_place_on_approval(current_user):
        placed_ref = "approved_signal_only(tier_not_eligible_to_place)"
    else:
        try:
            from app.engines.account_signals.runner import route_emitted_signal
            routed = await route_emitted_signal(signal_id, str(current_user.id), r.instrument,
                                                _signal_to_tradesignal(r),
                                                str(r.strategy_id) if r.strategy_id else None)
            placed_ref = ("; ".join(f"{m}:{'entered' if e else 'skip:'+rs}" for m, k, e, rs in routed)
                          if routed else "approved_no_active_eligible_session")
        except Exception as _pe:
            placed_ref = f"approved_place_error:{type(_pe).__name__}"
    from sqlalchemy import text as _t2
    await db.execute(_t2("UPDATE account_signals SET placed_ref = :pr WHERE id = :id"),
                     {"pr": placed_ref[:500], "id": signal_id})
    await audit_log(db, current_user.id, EVENT_TRADE_APPROVED,
                    {"signal_id": signal_id, "instrument": r.instrument, "placed_ref": placed_ref}, request)
    await db.commit()
    logger.info(f"[signal-decision] APPROVED signal={signal_id} user={current_user.id} -> {placed_ref}")
    return {"signal_id": signal_id, "decision": "approved", "placed_ref": placed_ref}


@router.get("/my-access")
async def my_access(current_user: User = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)):
    """The signed-in user's plan access: tier, automation status (agreement_required
    / pending / disabled / enabled / not_eligible), capabilities, and which
    agreements are accepted. The frontend badge + access explainer read this."""
    from app.core.packages import (is_fully_automated_tier, gets_signals,
        requires_manual_approval, can_place_on_approval, automation_status, tier_value)
    from app.api.routes.legal import has_current_ack
    from sqlalchemy import select as _select
    from app.models.user import BrokerAccount as _BA
    has_fat = await has_current_ack(db, current_user.id, "fully_automated_trading")
    has_sig = await has_current_ack(db, current_user.id, "signals_disclosure")
    acct = (await db.execute(_select(_BA).where(_BA.user_id == current_user.id))).scalars().first()
    trading_enabled = getattr(acct, "trading_enabled", None) if acct else None
    return {
        "tier": tier_value(current_user),
        "fully_automated": is_fully_automated_tier(current_user),
        "gets_signals": gets_signals(current_user),
        "requires_manual_approval": requires_manual_approval(current_user),
        "can_place_on_approval": can_place_on_approval(current_user),
        "automation_status": automation_status(current_user, has_agreement=has_fat,
                                                trading_enabled=trading_enabled),
        "agreements": {"fully_automated_trading": has_fat, "signals_disclosure_v2": has_sig},
        "has_broker_account": acct is not None,
    }

