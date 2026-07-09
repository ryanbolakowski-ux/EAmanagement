"""Read endpoint for the most recent Theta Scanner pick."""
import os, json
import asyncio
from loguru import logger
from datetime import date
from fastapi import APIRouter, Depends
from app.core.auth import require_2fa_when_paid as get_current_user
from app.models.user import User
import redis.asyncio as _r

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from app.database import get_db

router = APIRouter()
# 2FA gate: routes here require totp_enabled if user is on paid/trial subscription


def _market_session_flags() -> dict:
    """Return current ET session info: {label, duration, is_open}."""
    from datetime import datetime as _dts
    try:
        import zoneinfo
        et = _dts.now().astimezone(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        return {"label": "unknown", "duration": "day", "is_open": True}
    h, m = et.hour, et.minute
    t = h * 60 + m
    # 4:00-9:30 ET = pre-market
    if 4*60 <= t < 9*60+30:   return {"label": "PRE_MARKET",  "duration": "pre",  "is_open": False}
    # 9:30-16:00 ET = regular
    if 9*60+30 <= t < 16*60:  return {"label": "REGULAR",     "duration": "day",  "is_open": True}
    # 16:00-20:00 ET = after-hours
    if 16*60 <= t < 20*60:    return {"label": "AFTER_HOURS", "duration": "post", "is_open": False}
    # Closed
    return {"label": "CLOSED", "duration": "gtc", "is_open": False}


def _live_polygon_price(ticker: str):
    """Quick helper to fetch a current price for limit-order sizing."""
    import os as _osp, requests as _rqp
    k = _osp.environ.get("POLYGON_API_KEY", "")
    if not k: return None
    try:
        r = _rqp.get(f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}", params={"apiKey": k}, timeout=3)
        if r.status_code != 200: return None
        t = (r.json() or {}).get("ticker") or {}
        for fld, sub in (("lastTrade","p"),("min","c"),("day","c"),("prevDay","c")):
            v = (t.get(fld) or {}).get(sub)
            if v and float(v) > 0: return float(v)
    except Exception: pass
    return None




async def _refresh_broker_balance(db, user_id):
    """Pull live Tradier balance + write to broker_accounts.cached_*.
    Returns (equity, buying_power, open_pl) or (None, None, None) on failure."""
    from sqlalchemy import select, text as _t
    from app.models.user import BrokerAccount
    from app.engines.live_trading.broker_factory import build_broker_from_account
    broker = None
    try:
        acct = (await db.execute(select(BrokerAccount).where(BrokerAccount.user_id == user_id))).scalar_one_or_none()
        if not acct: return None, None, None
        broker = build_broker_from_account(acct)
        ok = await broker.connect()
        if not ok: return None, None, None
        bal = await broker.get_balance()
        await db.execute(_t("UPDATE broker_accounts SET cached_equity=:eq, cached_buying_power=:bp, cached_balance_at=NOW() WHERE id=:id"),
                         {"eq": bal.get("equity"), "bp": bal.get("buying_power"), "id": acct.id})
        await db.commit()
        return bal.get("equity"), bal.get("buying_power"), (bal.get("raw") or {}).get("open_pl")
    except Exception as e:
        return None, None, None
    finally:
        # Always release the broker's aiohttp session — without this every call
        # (on-demand views, the admin Fix, and the ~15-min balance-sync loop)
        # leaks a ClientSession ("Unclosed client session").
        if broker is not None:
            try:
                await broker.disconnect()
            except Exception:
                pass



# ─── Market status: weekend / news-blackout / open ──────────────────────
from datetime import datetime as _dts, timedelta as _tds


def _get_market_status_sync() -> dict:
    """Return {status, ...} where status is 'weekend' | 'open' | 'news_blackout' (sync part)."""
    try:
        import zoneinfo
        et = _dts.now().astimezone(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        et = _dts.utcnow()
    # Weekend or US market holiday → market closed
    from app.engines.market_calendar import is_trading_day as _is_td, holiday_name as _hn, next_trading_day as _ntd
    today = et.date()
    if not _is_td(today):
        nxt = _ntd(today)
        next_open = nxt.strftime("%A %b %d")
        hn = _hn(today)
        if hn:
            # Full-day holiday — show holiday name
            return {
                "status": "holiday",
                "holiday_name": hn,
                "today": et.strftime("%A"),
                "next_open": next_open,
            }
        else:
            # Weekend
            return {"status": "weekend", "today": et.strftime("%A"), "next_open": next_open}
    return {"status": "open"}


async def _get_market_status(db) -> dict:
    """Full check: weekend, then news-blackout via news_blackouts table."""
    base = _get_market_status_sync()
    if base["status"] in ("weekend", "holiday"):
        return base
    # News-blackout: any high-severity event within -30 min to +60 min of now
    try:
        from sqlalchemy import text as _t
        row = (await db.execute(_t("""
            SELECT event_name, event_time, severity
              FROM news_blackouts
             WHERE event_time BETWEEN (NOW() - INTERVAL '60 minutes') AND (NOW() + INTERVAL '30 minutes')
               AND lower(severity) IN ('high', 'red')
             ORDER BY event_time ASC LIMIT 1
        """))).first()
        if row:
            import zoneinfo
            et_time = row.event_time.astimezone(zoneinfo.ZoneInfo("America/New_York"))
            return {
                "status": "news_blackout",
                "event_name": row.event_name,
                "event_time_et": et_time.strftime("%I:%M %p ET"),
                "event_time_iso": row.event_time.isoformat(),
            }
    except Exception:
        pass
    return base

_redis = _r.from_url(os.environ.get("REDIS_URL", "redis://edge_redis:6379"), decode_responses=True)


@router.get("/today-pick")
async def today_pick(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the most-recent Theta Scanner pick + current market status."""
    # 1. Market status comes first — weekend / news blackout overrides everything
    status = await _get_market_status(db)
    if status["status"] in ("weekend", "holiday"):
        return {
            "pick": None, "market_status": status,
            "message": f"Markets closed — it's {status['today']}. Scanner resumes {status['next_open']} at 9:25 AM ET.",
        }
    if status["status"] == "news_blackout":
        return {
            "pick": None, "market_status": status,
            "message": f"Scanner paused — {status['event_name']} at {status['event_time_et']}. We don't trade through high-impact news.",
        }
    # 2. Normal path — return today's pick from Redis (fast path)
    raw = None
    try:
        raw = await _redis.get(f"theta:lastpick:{date.today().isoformat()}")
        # Do NOT fall back to theta:lastpick:latest — that key is not
        # date-bounded and would surface a PRIOR day's pick on a no-pick day
        # (this was the "yesterday's pick still showing" bug). The DB fallback
        # below is date-bounded (picked_at::date = CURRENT_DATE).
    except Exception as _re:
        # Redis hiccup (auth, network, restart) — fall through to DB
        raw = None

    # 3. Fallback: read today's pick from email_signals_history. Bulletproof
    #    against Redis being unavailable or having stale/missing keys (e.g.
    #    if the writer process had stale auth when the scanner fired).
    if not raw:
        try:
            from sqlalchemy import text as _t
            row = (await db.execute(_t("""
                SELECT ticker, asset_type, direction, entry, stop, target,
                       gap_pct, rel_vol, today_vol, score, catalyst_reason, picked_at
                  FROM email_signals_history
                 WHERE picked_at::date = CURRENT_DATE
                   AND asset_type = 'options'
                 ORDER BY picked_at DESC LIMIT 1
            """))).first()
            if row:
                m = row._mapping
                pick_dict = {
                    "ticker": m["ticker"], "asset_type": m["asset_type"],
                    "direction": m["direction"], "entry": float(m["entry"]),
                    "stop": float(m["stop"]), "target": float(m["target"]),
                    "gap_pct": float(m["gap_pct"]), "rel_vol": float(m["rel_vol"]),
                    "today_vol": int(m["today_vol"]), "score": float(m["score"]),
                    "catalyst_reason": m["catalyst_reason"],
                    "projected_move_pct": 10.0,
                    "picked_at": m["picked_at"].isoformat(),
                }
                raw = json.dumps(pick_dict)
        except Exception:
            pass

    if not raw:
        # No pick stored for today. Distinguish "window closed, no qualifying
        # setup" (with the reason) from "still scanning" so the UI never shows a
        # stale prior-day pick.
        no_pick_reason = None
        try:
            _np = await _redis.get(f"theta:nopick:{date.today().isoformat()}")
            if _np:
                no_pick_reason = (json.loads(_np) or {}).get("reason")
        except Exception:
            no_pick_reason = None
        if no_pick_reason:
            return {"pick": None, "no_pick": True, "reason": no_pick_reason,
                    "market_status": status,
                    "message": f"No pick today \u2014 {no_pick_reason}"}
        return {"pick": None, "no_pick": False, "market_status": status,
                "message": "No pick yet today. Scanner runs through 9:25 ET."}
    try:
        pick = json.loads(raw)
        # Enrich with live price + % change vs entry — best-effort
        try:
            live = _polygon_snapshot_price(pick.get("ticker"))
            if live and pick.get("entry"):
                pick["live_price"] = round(live, 4)
                pick["live_pct"] = round((live - float(pick["entry"])) / float(pick["entry"]) * 100.0, 2)
        except Exception: pass
        return {"pick": pick, "market_status": status}
    except Exception:
        return {"pick": None, "market_status": status, "message": "Stored pick is corrupt; will refresh on next scan."}


@router.get("/criteria")
async def scanner_criteria(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the Theta Scanner scoring rubric + today's pick rationale."""
    criteria = [
        {
            "name": "Gap %",
            "rule": "Open vs prior close between 5% and 25%",
            "rationale": "Below 5% lacks catalyst momentum; above 25% gaps often fade hard."
        },
        {
            "name": "Price band",
            "rule": "$2 ≤ price ≤ $200",
            "rationale": "Cheap-stock filter (avoids penny pumps); upper bound keeps options affordable on a $1k Saro allocation."
        },
        {
            "name": "Dollar volume",
            "rule": "today_vol × price ≥ $5M",
            "rationale": "Liquidity floor: thin tape means you cannot exit cleanly when the stop hits."
        },
        {
            "name": "Relative volume",
            "rule": "today_vol / prev_vol ≥ 2.0x (capped at 10x for scoring)",
            "rationale": "Confirms institutional participation. 2x prior day = real flow, not just gap-and-yawn."
        },
        {
            "name": "Catalyst weight (8-K item code)",
            "rule": "Item 1.01: 2.0x | 7.01: 1.4x | 8.01: 1.5x | 2.02: 1.3x | 5.02: 1.2x | none: 1.0x",
            "rationale": "Material agreements and Reg-FD disclosures move stocks harder than rumor-driven gaps."
        },
        {
            "name": "Symbol cleanliness",
            "rule": "skip warrants/units/rights/preferreds (W, WS, .U, .R, dotted)",
            "rationale": "Thin options markets + unusual mechanics break the standard sizing model."
        },
        {
            "name": "Score formula",
            "rule": "score = gap_pct × ln(today_vol) × catalyst_weight × min(rel_vol, 10) / 100",
            "rationale": "Multiplicative: ALL four factors must be present to score high. Higher = more momentum + catalyst confidence."
        },
        {
            "name": "Time-tiered threshold",
            "rule": "6:00 ET: ≥20 | 7:00: ≥18 | 7:30: ≥16 | 8:00: ≥14 | 8:30: ≥12 | 9:00: ≥10 | 9:25-9:50: any",
            "rationale": "Earlier = stricter. Pre-market liquidity is thin so we only fire on EXCEPTIONAL setups before 7am; the bar drops as we approach the open."
        },
    ]

    current_pick = None
    try:
        raw = await _r.from_url(os.environ.get("REDIS_URL", "redis://edge_redis:6379"),
                                 decode_responses=True).get("theta:lastpick:latest")
        if raw:
            p = json.loads(raw)
            n_alts = len(p.get("alternatives") or [])
            why = (
                f"Highest score ({p.get('score')}) among {n_alts + 1} candidates passing "
                f"gap/volume/cleanliness filters. Gap +{p.get('gap_pct', 0):.1f}% "
                f"within 5-25%; rel-vol {p.get('rel_vol')}x above 2.0 floor; "
                f"catalyst weight {p.get('catalyst_weight', 1.0):.1f}x from "
                f"{p.get('catalyst_reason') or 'no specific 8-K'}."
            )
            current_pick = {
                "ticker": p.get("ticker"),
                "score": p.get("score"),
                "gap_pct": p.get("gap_pct"),
                "rel_vol": p.get("rel_vol"),
                "today_vol": p.get("today_vol"),
                "catalyst_reason": p.get("catalyst_reason"),
                "catalyst_weight": p.get("catalyst_weight"),
                "entry": p.get("entry"), "stop": p.get("stop"), "target": p.get("target"),
                "picked_at": p.get("picked_at"),
                "why_selected": why,
                "alternatives": p.get("alternatives") or [],
            }
    except Exception:
        current_pick = None

    return {"criteria": criteria, "current_pick": current_pick}


@router.get("/history")
async def scanner_history(
    days: int = 30,
    asset_type: str = "all",   # 'options' | 'futures' | 'all'
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Best-effort resolve pass — walks unresolved picks against Polygon daily candles
    try: await _resolve_email_signal_outcomes(db)
    except Exception: pass

    """Return the last N days of Theta Scanner picks. Filterable by asset_type."""
    days = max(1, min(int(days), 90))
    where = "picked_at > NOW() - (INTERVAL '1 day' * :d) AND COALESCE(shadow,false)=false"
    params = {"d": days}
    if asset_type in ("options", "futures", "stocks"):
        where += " AND asset_type = :at"
        params["at"] = asset_type
    rows = (await db.execute(text(f"""
        SELECT id, picked_at, ticker, asset_type, direction, entry, stop, target,
               gap_pct, rel_vol, today_vol, score, catalyst_reason,
               outcome, outcome_pct, resolved_at
          FROM email_signals_history
         WHERE {where}
         ORDER BY picked_at DESC
         LIMIT 200
    """), params)).fetchall()
    return {
        "days": days, "asset_type": asset_type, "count": len(rows),
        "picks": [dict(r._mapping) for r in rows],
    }


@router.get("/shadow-stats")
async def scanner_shadow_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Per-template forward-test stats from the daily SHADOW scan + promotion
    readiness. Watch-only templates only — nothing here is live or tradeable."""
    try:
        await _resolve_email_signal_outcomes(db)
    except Exception:
        pass
    from app.engines.scanner.promotion import template_stats
    return await template_stats(db)



# ────────────────────────────────────────────────────────────────────────
# Outcome resolver: walk Polygon daily candles after each pick, mark win
# when target hit / loss when stop hit / expired after 5 trading days.
# ────────────────────────────────────────────────────────────────────────
import os as _os
import requests as _rq
from datetime import datetime as _dt, timedelta as _td




def _polygon_snapshot_price(ticker: str):
    """Return latest available price for a US stock ticker. None on failure.
    Fallback chain: lastTrade.p -> min.c -> day.c -> prevDay.c (cover off-hours)."""
    key = _os.environ.get("POLYGON_API_KEY", "")
    if not key: return None
    try:
        url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
        r = _rq.get(url, params={"apiKey": key}, timeout=4)
        if r.status_code != 200: return None
        t = (r.json() or {}).get("ticker") or {}
        for field, sub in (("lastTrade", "p"), ("min", "c"), ("day", "c"), ("prevDay", "c")):
            obj = t.get(field) or {}
            v = obj.get(sub)
            if v and float(v) > 0:
                return float(v)
    except Exception:
        return None
    return None

def _polygon_daily_range(ticker: str, start_iso: str, end_iso: str):
    """Return daily OHLC bars for ticker. Empty list on failure.
    POLYGON-EXIT: falls back to FMP daily EOD (same row shape) when Polygon
    has no key or returns nothing — survives the Polygon cancellation."""
    rows = _polygon_daily_range_polygon(ticker, start_iso, end_iso)
    if rows:
        return rows
    try:
        from app.engines.data_feeds.fmp_feed import fetch_daily_bars_sync
        return fetch_daily_bars_sync(ticker, start_iso, end_iso) or []
    except Exception:
        return []


def _polygon_daily_range_polygon(ticker: str, start_iso: str, end_iso: str):
    """Original Polygon implementation (primary while the key lives)."""
    key = _os.environ.get("POLYGON_API_KEY", "")
    if not key: return []
    try:
        url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start_iso}/{end_iso}"
        r = _rq.get(url, params={"adjusted": "true", "sort": "asc", "apiKey": key}, timeout=10)
        if r.status_code != 200: return []
        return (r.json().get("results") or [])
    except Exception:
        return []


async def _resolve_email_signal_outcomes(db):
    """Walk unresolved rows in email_signals_history; mark win/loss/expired."""
    from sqlalchemy import text as _t
    rows = (await db.execute(_t("""
        SELECT id, ticker, entry, stop, target, picked_at
          FROM email_signals_history
         WHERE outcome IS NULL
           AND picked_at < NOW() - INTERVAL '1 hour'
           AND picked_at > NOW() - INTERVAL '14 days'
         ORDER BY picked_at ASC
         LIMIT 100
    """))).fetchall()
    if not rows:
        return 0
    resolved = 0
    today = _dt.utcnow().date()
    consecutive_empty = 0
    for r in rows:
        try:
            start = r.picked_at.date().isoformat()
            end = today.isoformat()
            bars = await asyncio.to_thread(_polygon_daily_range, r.ticker, start, end)
            if not bars:
                consecutive_empty += 1
                if consecutive_empty >= 3 and not _os.environ.get("POLYGON_API_KEY", ""):
                    logger.warning("[email-outcomes] 3 consecutive tickers with no bars "
                                   "and no POLYGON_API_KEY set — aborting this resolution pass")
                    break
                continue
            consecutive_empty = 0
            entry = float(r.entry); stop = float(r.stop); target = float(r.target)
            outcome = None; outcome_pct = None
            for bar in bars:
                hi = float(bar.get("h") or 0); lo = float(bar.get("l") or 0)
                # Long-side semantics: target above entry, stop below
                if lo <= stop:
                    outcome = "loss"; outcome_pct = round((stop - entry) / entry * 100.0, 2); break
                if hi >= target:
                    outcome = "win";  outcome_pct = round((target - entry) / entry * 100.0, 2); break
            # If 5+ trading days passed without hitting either → expired
            if outcome is None:
                age_days = (today - r.picked_at.date()).days
                if age_days >= 5:
                    last_close = float(bars[-1].get("c") or entry)
                    outcome = "expired"; outcome_pct = round((last_close - entry) / entry * 100.0, 2)
            if outcome:
                await db.execute(_t("""
                    UPDATE email_signals_history
                       SET outcome = :o, outcome_pct = :p, resolved_at = NOW()
                     WHERE id = :id
                """), {"o": outcome, "p": outcome_pct, "id": r.id})
                resolved += 1
        except Exception as e:
            continue
    if resolved > 0:
        await db.commit()
    return resolved



@router.get("/open-positions")
async def open_positions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List Theta Scanner positions still open + live PnL."""
    import os as _os, requests as _rq
    key = _os.environ.get("POLYGON_API_KEY", "")
    rows = (await db.execute(text("""
        SELECT id, ticker, qty, entry_price, trail_high, hard_stop, target,
               opened_at, broker_order_id
          FROM open_positions_watch
         WHERE user_id = CAST(:uid AS uuid) AND status = 'open'
           AND source = 'theta_scanner'
           AND opened_at::date = CURRENT_DATE
         ORDER BY opened_at DESC
    """), {"uid": str(current_user.id)})).fetchall()
    positions = []
    total_cost = 0.0; total_value = 0.0
    for r in rows:
        live = None
        if key:
            try:
                u = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{r.ticker}"
                resp = await asyncio.to_thread(_rq.get, u, params={"apiKey": key}, timeout=3)
                if resp.status_code == 200:
                    t = (resp.json() or {}).get("ticker") or {}
                    for fld, sub in (("lastTrade","p"), ("min","c"), ("day","c"), ("prevDay","c")):
                        v = (t.get(fld) or {}).get(sub)
                        if v and float(v) > 0: live = float(v); break
            except Exception: pass
        entry = float(r.entry_price); qty = int(r.qty)
        cost = entry * qty
        value = (live or entry) * qty
        unreal = value - cost
        unreal_pct = (unreal / cost * 100.0) if cost else 0
        total_cost += cost; total_value += value
        positions.append({
            "id": r.id, "ticker": r.ticker, "qty": qty,
            "entry_price": entry, "live_price": live,
            "trail_high": float(r.trail_high), "target": float(r.target or 0),
            "hard_stop": float(r.hard_stop or 0),
            "unrealized_pnl": round(unreal, 2),
            "unrealized_pct": round(unreal_pct, 2),
            "opened_at": r.opened_at.isoformat(),
            "broker_order_id": r.broker_order_id,
        })
    total_pnl = total_value - total_cost
    total_pct = (total_pnl / total_cost * 100.0) if total_cost else 0
    # Today's closed positions (realized P&L)
    closed_rows = (await db.execute(text("""
        SELECT ticker, qty, entry_price, exit_price, exit_reason, closed_at
          FROM open_positions_watch
         WHERE user_id = CAST(:uid AS uuid)
           AND status = 'closed'
           AND closed_at::date = CURRENT_DATE
           AND opened_at::date = CURRENT_DATE
           AND source = 'theta_scanner'
         ORDER BY closed_at DESC
    """), {"uid": str(current_user.id)})).fetchall()
    closed_today = []
    realized_total = 0.0
    for c in closed_rows:
        if not c.exit_price: continue
        entry = float(c.entry_price); exitp = float(c.exit_price); qty = int(c.qty)
        rpnl = (exitp - entry) * qty
        rpct = (exitp - entry) / entry * 100.0 if entry else 0
        realized_total += rpnl
        closed_today.append({
            "ticker": c.ticker, "qty": qty,
            "entry_price": entry, "exit_price": exitp,
            "realized_pnl": round(rpnl, 2), "realized_pct": round(rpct, 2),
            "exit_reason": c.exit_reason, "closed_at": c.closed_at.isoformat(),
        })
    eq, bp, open_pl = await _refresh_broker_balance(db, current_user.id)
    return {
        "positions": positions, "count": len(positions),
        "total_cost": round(total_cost, 2), "total_value": round(total_value, 2),
        "total_unrealized": round(total_pnl, 2), "total_unrealized_pct": round(total_pct, 2),
        "closed_today": closed_today,
        "realized_today": round(realized_total, 2),
        "tradier_equity": eq,
        "tradier_buying_power": bp,
        "tradier_open_pl": open_pl,
    }


@router.post("/close-all")
async def close_all_positions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit market SELL for every open Theta Scanner position + mark closed."""
    from app.engines.live_trading.broker_factory import build_broker_from_account
    from app.engines.live_trading.broker_base import OrderRequest, OrderSide, OrderType
    from app.models.user import BrokerAccount
    from sqlalchemy import select as _sel
    rows = (await db.execute(text("""
        SELECT id, broker_account_id, ticker, qty, broker_order_id
          FROM open_positions_watch
         WHERE user_id = CAST(:uid AS uuid) AND status = 'open'
    """), {"uid": str(current_user.id)})).fetchall()
    results = []
    for r in rows:
        acct = (await db.execute(_sel(BrokerAccount).where(BrokerAccount.id == r.broker_account_id))).scalar_one_or_none()
        if not acct:
            results.append({"ticker": r.ticker, "ok": False, "err": "broker account missing"}); continue
        try:
            broker = build_broker_from_account(acct)
            await broker.connect()
            resp_o = await broker.place_order(OrderRequest(
                instrument=r.ticker, side=OrderSide.SELL,
                quantity=int(r.qty), order_type=OrderType.MARKET,
            ))
            # Capture current price as exit
            exit_p = None
            try:
                import os as _os2, requests as _rq2
                k2 = _os2.environ.get("POLYGON_API_KEY", "")
                u2 = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{r.ticker}"
                resp = await asyncio.to_thread(_rq2.get, u2, params={"apiKey": k2}, timeout=3)
                if resp.status_code == 200:
                    tk = (resp.json() or {}).get("ticker") or {}
                    for fld, sub in (("lastTrade","p"), ("min","c"), ("day","c")):
                        v = (tk.get(fld) or {}).get(sub)
                        if v and float(v) > 0: exit_p = float(v); break
            except Exception: pass
            await db.execute(text("""
                UPDATE open_positions_watch
                   SET status='closed', exit_reason='manual_close_all',
                       exit_price=:ep, closed_at=NOW()
                 WHERE id=:id
            """), {"id": r.id, "ep": exit_p})
            try:
                if exit_p and r.broker_order_id:
                    await db.execute(text("""
                        UPDATE trades
                           SET status='closed', exit_price=:ep, exit_time=NOW(),
                               pnl = (:ep - entry_price) * contracts,
                               net_pnl = (:ep - entry_price) * contracts,
                               exit_reason='manual_close_all'
                         WHERE broker_order_id = :oid AND status='open'
                    """), {"ep": exit_p, "oid": r.broker_order_id})
            except Exception: pass
            results.append({"ticker": r.ticker, "qty": int(r.qty), "ok": True, "order_id": resp_o.broker_order_id})
        except Exception as e:
            results.append({"ticker": r.ticker, "ok": False, "err": str(e)})
    await db.commit()
    return {"closed": results, "count": len([r for r in results if r.get("ok")])}



@router.post("/force-close-all")
async def force_close_all_tradier(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit market SELL for EVERY open position at the broker.
    Captures live exit price + computes realized pnl."""
    from app.engines.live_trading.broker_factory import build_broker_from_account
    from app.engines.live_trading.broker_base import OrderRequest, OrderSide, OrderType
    from app.models.user import BrokerAccount
    from sqlalchemy import select as _sel
    import os as _os_fc, requests as _rq_fc
    acct = (await db.execute(_sel(BrokerAccount).where(BrokerAccount.user_id == current_user.id, BrokerAccount.is_active == True))).scalar_one_or_none()
    if not acct:
        return {"closed": [], "count": 0, "error": "no broker account linked"}
    broker = build_broker_from_account(acct)
    ok = await broker.connect()
    if not ok:
        return {"closed": [], "count": 0, "error": "broker connect failed"}
    # MANUAL-CLOSE-FIX: close from OUR ledger (open_positions_watch), not the
    # broker's position list. Tradier SANDBOX returns no positions, so the old
    # broker-ledger loop closed NOTHING and rows stuck open forever.
    _owrows = (await db.execute(text(
        "SELECT ticker, qty FROM open_positions_watch "
        "WHERE user_id = CAST(:uid AS uuid) AND status='open'"
    ), {"uid": str(current_user.id)})).fetchall()
    results = []
    key = _os_fc.environ.get("POLYGON_API_KEY", "")
    if not _owrows:
        return {"closed": [], "count": 0, "error": "no open positions on record to close"}
    for p in _owrows:
        sym = p.ticker; qty = int(p.qty or 0)
        if not sym or qty <= 0: continue
        try:
            resp_o = await broker.place_order(OrderRequest(
                instrument=sym, side=OrderSide.SELL,
                quantity=qty, order_type=OrderType.MARKET,
            ))
            # Capture live price
            ep = None
            try:
                rr = _rq_fc.get(f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{sym}", params={"apiKey": key}, timeout=3)
                if rr.status_code == 200:
                    tk2 = (rr.json() or {}).get("ticker") or {}
                    for fld, sub in (("lastTrade","p"),("min","c"),("day","c"),("prevDay","c")):
                        v = (tk2.get(fld) or {}).get(sub)
                        if v and float(v) > 0:
                            ep = float(v); break
            except Exception:
                ep = None
            results.append({"ticker": sym, "qty": qty, "ok": True, "order_id": resp_o.broker_order_id, "exit_price": ep})
            try:
                await db.execute(text("UPDATE open_positions_watch SET status='closed', exit_reason='force_close_all', exit_price=:ep, closed_at=NOW() WHERE user_id=CAST(:uid AS uuid) AND ticker=:tk AND status='open'"),
                                 {"uid": str(current_user.id), "tk": sym, "ep": ep})
                await db.execute(text("UPDATE trades SET status='closed', exit_time=NOW(), exit_reason='force_close_all', exit_price=:ep, pnl=CASE WHEN :ep IS NULL THEN NULL ELSE (CAST(:ep AS NUMERIC) - entry_price) * contracts END, net_pnl=CASE WHEN :ep IS NULL THEN NULL ELSE (CAST(:ep AS NUMERIC) - entry_price) * contracts END WHERE user_id=CAST(:uid AS uuid) AND instrument=:tk AND mode='live' AND status='open'"),
                                 {"uid": str(current_user.id), "tk": sym, "ep": ep})
            except Exception: pass
        except Exception as e:
            results.append({"ticker": sym, "qty": qty, "ok": False, "err": str(e)})
    await db.commit()
    return {"closed": results, "count": len([r for r in results if r.get("ok")])}



@router.get("/pending-orders")
async def pending_orders(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List open/pending orders at the broker — used by widget to show 'will fill at market open' status."""
    from app.engines.live_trading.broker_factory import build_broker_from_account
    from app.models.user import BrokerAccount
    from sqlalchemy import select as _sel
    import requests as _rq_po
    acct = (await db.execute(_sel(BrokerAccount).where(BrokerAccount.user_id == current_user.id, BrokerAccount.is_active == True))).scalar_one_or_none()
    if not acct: return {"orders": [], "count": 0, "session": _market_session_flags()}
    try:
        broker = build_broker_from_account(acct)
        await broker.connect()
        url = f"https://sandbox.tradier.com/v1/accounts/{broker.account_id}/orders"
        r = _rq_po.get(url, headers={"Authorization": f"Bearer {broker.access_token}", "Accept": "application/json"}, timeout=8)
        if r.status_code != 200:
            return {"orders": [], "count": 0, "session": _market_session_flags(), "error": f"tradier {r.status_code}"}
        raw = ((r.json() or {}).get("orders") or {}).get("order") or []
        if isinstance(raw, dict): raw = [raw]
        pending_only = [o for o in raw if o.get("status") in ("open", "pending", "partially_filled")]
        out = []
        for o in pending_only:
            out.append({
                "id": o.get("id"),
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "quantity": o.get("quantity"),
                "type": o.get("type"),
                "price": o.get("price"),
                "status": o.get("status"),
                "duration": o.get("duration"),
                "create_date": o.get("create_date"),
            })
        return {"orders": out, "count": len(out), "session": _market_session_flags()}
    except Exception as e:
        return {"orders": [], "count": 0, "session": _market_session_flags(), "error": str(e)}


@router.delete("/pending-orders/{order_id}")
async def cancel_pending_order(
    order_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a specific pending order at Tradier."""
    from app.engines.live_trading.broker_factory import build_broker_from_account
    from app.models.user import BrokerAccount
    from sqlalchemy import select as _sel
    import requests as _rq_co
    acct = (await db.execute(_sel(BrokerAccount).where(BrokerAccount.user_id == current_user.id, BrokerAccount.is_active == True))).scalar_one_or_none()
    if not acct: return {"ok": False, "error": "no broker"}
    broker = build_broker_from_account(acct)
    await broker.connect()
    url = f"https://sandbox.tradier.com/v1/accounts/{broker.account_id}/orders/{order_id}"
    r = _rq_co.delete(url, headers={"Authorization": f"Bearer {broker.access_token}", "Accept": "application/json"}, timeout=8)
    return {"ok": r.status_code == 200, "status_code": r.status_code}



@router.post("/close-trade/{trade_id}")
async def close_specific_trade(
    trade_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Close one specific trade by ID via broker market sell."""
    from app.engines.live_trading.broker_factory import build_broker_from_account
    from app.engines.live_trading.broker_base import OrderRequest, OrderSide, OrderType
    from app.models.user import BrokerAccount
    from sqlalchemy import select as _sel
    row = (await db.execute(text("""
        SELECT id, instrument, direction, contracts, broker_account_id, broker_order_id, entry_price
          FROM trades
         WHERE id = CAST(:tid AS uuid) AND user_id = CAST(:uid AS uuid) AND status='open'
    """), {"tid": trade_id, "uid": str(current_user.id)})).first()
    if not row:
        return {"ok": False, "error": "trade not found or already closed"}
    acct = (await db.execute(_sel(BrokerAccount).where(BrokerAccount.id == row.broker_account_id))).scalar_one_or_none()
    if not acct:
        return {"ok": False, "error": "broker account missing"}
    # Get live price
    ep = _polygon_snapshot_price(row.instrument)
    broker = build_broker_from_account(acct)
    # MANUAL-CLOSE-FIX: surface broker errors instead of a swallowed 500.
    try:
        await broker.connect()
        side = OrderSide.SELL if row.direction == "long" else OrderSide.BUY
        sess = _market_session_flags()
        if sess["is_open"]:
            resp = await broker.place_order(OrderRequest(
                instrument=row.instrument, side=side, quantity=int(row.contracts),
                order_type=OrderType.MARKET, time_in_force="day"))
        elif ep and sess["duration"] in ("pre", "post"):
            limit_px = round(ep * (0.99 if side == OrderSide.SELL else 1.01), 2)
            resp = await broker.place_order(OrderRequest(
                instrument=row.instrument, side=side, quantity=int(row.contracts),
                order_type=OrderType.LIMIT, price=limit_px, time_in_force=sess["duration"]))
        else:
            resp = await broker.place_order(OrderRequest(
                instrument=row.instrument, side=side, quantity=int(row.contracts),
                order_type=OrderType.MARKET, time_in_force="day"))
    except Exception as _ce:
        logger.error(f"[close-trade] broker error {row.instrument}: {_ce}")
        return {"ok": False, "error": f"broker rejected the close: {_ce}"}
    # MANUAL-CLOSE-FIX: ALWAYS record the close (+commit), even with no live
    # price — otherwise the SELL fires but the row stays 'open' forever.
    await db.execute(text(
        "UPDATE trades SET status='closed', exit_time=NOW(), exit_reason='manual_per_row', "
        "exit_price=:ep, pnl=CASE WHEN :ep IS NULL THEN pnl ELSE (CAST(:ep AS NUMERIC)-entry_price)*contracts END, "
        "net_pnl=CASE WHEN :ep IS NULL THEN net_pnl ELSE (CAST(:ep AS NUMERIC)-entry_price)*contracts END "
        "WHERE id = CAST(:tid AS uuid)"), {"ep": ep, "tid": str(row.id)})
    await db.execute(text(
        "UPDATE open_positions_watch SET status='closed', exit_reason='manual_per_row', "
        "exit_price=:ep, closed_at=NOW() WHERE ticker=:tk AND user_id=CAST(:uid AS uuid) AND status='open'"),
        {"ep": ep, "tk": row.instrument, "uid": str(current_user.id)})
    await db.commit()
    return {"ok": True, "order_id": getattr(resp, "broker_order_id", None), "ticker": row.instrument, "exit_price": ep, "priced": ep is not None}


@router.get("/analyze")
async def scanner_analyze(
    ticker: str,
    direction: str = "long",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Analyze ANY ticker on demand: live price + structure-based entry/stop/target
    (real R:R + measured-move) + the scanner quality-gate verdict (above/below VWAP,
    liquidity, overextension). Read-only; no email, no order. Not a prediction."""
    from app.engines.options.theta_scanner import analyze_ticker
    return await analyze_ticker(db, ticker, direction)
