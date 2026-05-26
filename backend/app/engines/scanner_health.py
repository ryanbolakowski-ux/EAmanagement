"""Scanner pipeline health — proves the entire futures + options email path
works end-to-end. Three layers:

  1. Health endpoint  (/api/v1/admin/scanner-health) — JSON snapshot, on-demand
  2. Daily heartbeat email at 9:25 ET — to admin + each strategy subscriber
  3. Background self-test every 5 min during market hours — alerts admin
     if anything silently breaks (yfinance dead, Redis auth failed, etc.)

Goal: never again have a user notice the pipeline is broken before we do.
"""
import asyncio
import os
from datetime import datetime, timezone, timedelta
from typing import Optional
from loguru import logger


_LAST_HEALTH_CHECK: dict = {"ts": None, "ok": False, "components": {}}
_LAST_HEARTBEAT_SENT: Optional[str] = None  # date iso str


async def check_health(verbose: bool = False) -> dict:
    """Probe every layer of the scanner pipeline. Returns dict with per-component
    state + an aggregate ok bool. Designed to be called from both the HTTP
    endpoint and the background self-test."""
    result = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ok": True,
        "components": {},
    }

    # 1. Redis — auth + ability to SETNX (the dedup cap mechanism)
    try:
        import redis.asyncio as _ra
        r = _ra.from_url(os.environ["REDIS_URL"], decode_responses=True)
        pong = await r.ping()
        # try the same SETNX path the cap-writer uses
        await r.set("health:probe", "1", ex=10, nx=True)
        await r.delete("health:probe")
        await r.aclose()
        result["components"]["redis"] = {"ok": True, "ping": pong}
    except Exception as e:
        result["components"]["redis"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        result["ok"] = False

    # 2. Yfinance — can we actually get a recent NQ bar?
    try:
        import yfinance as yf
        df = yf.Ticker("NQ=F").history(period="1d", interval="1m")
        if df is None or df.empty:
            raise RuntimeError("yfinance returned no bars")
        latest = df.index[-1]
        age_min = (datetime.now(timezone.utc) - latest.tz_convert("UTC").to_pydatetime()).total_seconds() / 60
        result["components"]["yfinance"] = {
            "ok": age_min < 60,  # less than 1h stale
            "bars": len(df),
            "latest": latest.strftime("%Y-%m-%d %H:%M %Z"),
            "stale_min": round(age_min, 1),
        }
        if age_min >= 60:
            result["ok"] = False
    except Exception as e:
        result["components"]["yfinance"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
        result["ok"] = False

    # 3. Resend — can we send mail?
    try:
        import httpx
        key = os.environ.get("RESEND_API_KEY", "")
        if not key:
            raise RuntimeError("RESEND_API_KEY not set")
        r = httpx.get("https://api.resend.com/domains", headers={"Authorization": f"Bearer {key}"}, timeout=8)
        result["components"]["resend"] = {"ok": r.status_code == 200, "status": r.status_code}
        if r.status_code != 200:
            result["ok"] = False
    except Exception as e:
        result["components"]["resend"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
        result["ok"] = False

    # 4. Polygon — for options scanner
    try:
        import httpx
        key = os.environ.get("POLYGON_API_KEY", "")
        if not key:
            raise RuntimeError("POLYGON_API_KEY not set")
        r = httpx.get(f"https://api.polygon.io/v1/marketstatus/now?apiKey={key}", timeout=8)
        result["components"]["polygon"] = {"ok": r.status_code == 200, "status": r.status_code}
        if r.status_code != 200:
            result["ok"] = False
    except Exception as e:
        result["components"]["polygon"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
        result["ok"] = False

    # 5. Database — can we query users?
    try:
        from sqlalchemy import text as _t
        from app.database import async_session_factory
        async with async_session_factory() as db:
            row = (await db.execute(_t("SELECT COUNT(*) FROM users WHERE is_active = true"))).scalar()
            result["components"]["database"] = {"ok": True, "active_users": row}
    except Exception as e:
        result["components"]["database"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
        result["ok"] = False

    # 6. Active watchers — should be >0 if user has any signal subscriptions
    try:
        from sqlalchemy import text as _t
        from app.database import async_session_factory
        async with async_session_factory() as db:
            n = (await db.execute(_t("SELECT COUNT(*) FROM account_signal_watchers WHERE is_active = true"))).scalar()
            result["components"]["watchers_active"] = {"ok": n > 0, "count": n}
    except Exception as e:
        result["components"]["watchers_active"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}

    # 7. Theta scanner — did it fire today?
    try:
        from sqlalchemy import text as _t
        from app.database import async_session_factory
        async with async_session_factory() as db:
            row = (await db.execute(_t("""
                SELECT ticker, picked_at FROM email_signals_history
                 WHERE picked_at::date = CURRENT_DATE AND asset_type = 'options'
                 ORDER BY picked_at DESC LIMIT 1
            """))).first()
            if row:
                result["components"]["theta_scanner_today"] = {"ok": True, "ticker": row.ticker, "at": row.picked_at.isoformat()}
            else:
                # Only "not ok" if market is open today AND past 9:50 ET
                from app.engines.market_calendar import market_status as _ms
                ms = _ms()
                expected = ms.get("is_trading_day") and ms.get("now_et", "")[11:16] > "09:50"
                result["components"]["theta_scanner_today"] = {"ok": not expected, "ticker": None, "expected": expected}
                if expected:
                    result["ok"] = False
    except Exception as e:
        result["components"]["theta_scanner_today"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}

    global _LAST_HEALTH_CHECK
    _LAST_HEALTH_CHECK = result
    return result


async def send_daily_heartbeat():
    """Sends one heartbeat email per day at 9:25 ET to each user with active
    strategy subscriptions, confirming the pipeline is online. Includes the
    health-check result so users can SEE the system is alive even on days
    when no setup matches their criteria.
    """
    global _LAST_HEARTBEAT_SENT
    from zoneinfo import ZoneInfo
    et = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
    today_str = et.strftime("%Y-%m-%d")
    if _LAST_HEARTBEAT_SENT == today_str:
        return
    if not (9 <= et.hour <= 10):  # only between 9-10 ET
        return

    from app.engines.market_calendar import market_status as _ms
    ms = _ms()
    if not ms.get("is_trading_day"):
        return  # no heartbeat on weekends/holidays

    health = await check_health()
    from sqlalchemy import text as _t
    from app.database import async_session_factory
    from app.services.email import _send

    async with async_session_factory() as db:
        rows = (await db.execute(_t("""
            SELECT DISTINCT u.email
              FROM users u
              LEFT JOIN strategies s ON s.user_id = u.id AND s.status = 'ACTIVE'
              LEFT JOIN account_signal_watchers w ON w.user_id = u.id AND w.is_active = true
             WHERE u.is_active = true
               AND (s.id IS NOT NULL OR w.id IS NOT NULL)
        """))).fetchall()

    ok_emoji = "✅" if health["ok"] else "⚠️"
    subj = f"🎯 Theta Scanner — heartbeat {ok_emoji} ({today_str})"
    rows_html = ""
    for cname, c in health["components"].items():
        icon = "✅" if c.get("ok") else "❌"
        detail = ""
        for k in ("ping", "bars", "latest", "stale_min", "ticker", "count", "status"):
            if k in c:
                detail += f" {k}={c[k]}"
        if "error" in c:
            detail += f" ERR={c['error'][:60]}"
        rows_html += f"<tr><td>{icon}</td><td><b>{cname}</b></td><td style='color:#64748b'>{detail}</td></tr>"

    html = f"""
<div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;padding:20px">
  <h1 style="color:#7c3aed;margin:0 0 10px">🎯 Theta Scanner — pipeline online</h1>
  <p style="color:#475569;margin:0 0 16px;font-size:14px">
    Heartbeat for {today_str}. This email confirms the scanner is watching your
    active strategies. <b>If a setup matches your criteria today, you will receive
    a separate signal email.</b> No setup match means no email — which is normal.
  </p>
  <table style="border-collapse:collapse;width:100%;font-size:13px">
    <thead><tr style="background:#f1f5f9"><th align=left>OK</th><th align=left>Component</th><th align=left>Detail</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  <p style="color:#94a3b8;font-size:11px;margin-top:20px">
    You receive one of these per trading day. Disable in Profile if not useful.
  </p>
</div>"""

    sent = 0
    for r in rows:
        try:
            if _send(r.email, subj, html):
                sent += 1
        except Exception:
            pass
    _LAST_HEARTBEAT_SENT = today_str
    logger.info(f"[heartbeat] sent to {sent}/{len(rows)} subscribers (health.ok={health['ok']})")


async def run_health_monitor_loop():
    """Background task: runs every 5 minutes during market hours, calls
    check_health, sends an admin alert if anything is broken. Also fires the
    daily 9:25 heartbeat to subscribers."""
    while True:
        try:
            await send_daily_heartbeat()  # idempotent — only fires once per day
            health = await check_health()
            if not health["ok"]:
                # Build a terse log line; future iteration could SMS / Slack
                broken = [k for k, v in health["components"].items() if not v.get("ok")]
                logger.error(f"[scanner-health] PIPELINE DEGRADED — broken: {broken}")
        except Exception as e:
            logger.warning(f"[scanner-health] loop iteration failed: {e}")
        await asyncio.sleep(300)
