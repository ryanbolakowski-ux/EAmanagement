"""Saro Premarket Watch — the 08:45 ET catalyst watchlist email.

WHAT (owner approved 2026-07-14, 'both' tracks): once per trading morning,
build a short premarket watchlist from
  (1) fresh EDGAR 8-K catalysts (<18h — the same edgar_filings table and
      _CATALYST_WEIGHTS the daily pick uses),
  (2) ONE FMP stock-news call for premarket headlines, and
  (3) the three FMP movers lists (symbols only!),
then confirm every name with a LIVE /stable/quote-short (<=MAX_QUOTES quotes,
QUOTE_PACING_S pacing). Movers rows can carry stale prior-session prices
premarket (the GPC 7/06 incident that spawned the stale-quote hard gate), so
the live quote is the ONLY premarket price this module trusts. The gap is
computed against the REAL previous-session close from the fmp_eod_snapshot
table (fallback: fetch_last_settled_close_sync, bounded to
MAX_CLOSE_FALLBACKS symbols).

DOCTRINE FILTERS (mirrors the scanner's quality bar, not a new invention):
price >= $5, prev-session dollar volume >= $20M WHEN KNOWN (unknown volume is
never fabricated — it passes), |gap| >= 3%. Rank = |gap| x catalyst weight,
email the top TOP_N.

THIS IS NOT A SIGNAL: the email says so explicitly — Saro's confirmed pick
still fires after 9:33 ET (see _min_score_for_et in premarket_scheduler).
Subject carries "Saro" so the EMAIL_KILL_SWITCH whitelist passes it.

PRE-LOCK (Track B ignition): the FULL ranked filtered list (not just the
emailed top 5) is stashed at theta:ignition:candidates:{ET-date} (12h TTL) as
JSON [{ticker, prev_close, premarket_price, gap_pct, catalyst}].

ET-ANCHORED DATES: every date key derives from _now_et() (UTC -> ET). At
00:xx UTC the key is still the PREVIOUS ET day — the UTC-date bug family is
banned here.

Env: PREMARKET_WATCH_ENABLED (default "1").
Latch: theta:premarket_watch:{YYYY-MM-DD} (SETNX, ex 20h). Unlike the EOD
snapshot hook, the latch is NOT released when the run fails: a partial
failure may already have emailed some subscribers, and a duplicate morning
blast is worse than a missed one.

API budget per morning: 1 news + 3 movers + <=60 quote-short
+ <=15 EOD-close fallbacks = <=79 FMP requests (typically ~50-65).
"""
from __future__ import annotations

import html

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import text

# ── knobs ────────────────────────────────────────────────────────────────────
WATCH_FIRE_ET = (8, 45)        # earliest fire (ET)
WATCH_WINDOW_END_ET = (9, 25)  # after this a "premarket" watch is stale noise
                               # (and the 9:33 scanner is about to own the tape)
MAX_QUOTES = 60                # hard cap on quote-short calls per morning
QUOTE_PACING_S = 0.15          # pause between quote-short calls
MAX_CLOSE_FALLBACKS = 15       # cap on per-symbol EOD-close fallback fetches
MIN_PRICE = 5.0                # doctrine: no sub-$5 names
MIN_PREV_DOLLAR_VOL = 20_000_000.0  # $20M prev-session $ volume, when known
MIN_ABS_GAP_PCT = 3.0          # |premarket gap| floor
TOP_N = 5                      # rows in the email
EDGAR_CATALYST_WINDOW_H = 18   # 8-K freshness window
NEWS_WINDOW_H = 18             # headline freshness window
NEWS_LIMIT = 100               # rows from the single stock-news call

NEWS_URL = "https://financialmodelingprep.com/stable/news/stock-latest"

REDIS_LATCH_PREFIX = "theta:premarket_watch:"
LATCH_TTL_S = 20 * 3600
IGNITION_KEY_PREFIX = "theta:ignition:candidates:"
IGNITION_TTL_S = 12 * 3600


# ── small seams (tests monkeypatch these) ────────────────────────────────────
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_et() -> Optional[datetime]:
    """UTC -> America/New_York. Date keys MUST come from this, never UTC."""
    try:
        import zoneinfo
        return _now_utc().astimezone(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        return None


def _session_factory():
    from app.database import async_session_factory
    return async_session_factory


def _get_redis():
    import redis as _redis
    return _redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"),
                                 decode_responses=True)


async def _send_email(to: str, subject: str, html: str) -> bool:
    """One tracked send. Subject contains 'Saro' -> passes the kill-switch
    whitelist. Tests MUST monkeypatch this (no real emails, ever)."""
    from app.services.email import _send_tracked
    try:
        return bool((await asyncio.to_thread(_send_tracked, to, subject, html) or {}).get("sent"))
    except Exception as e:
        logger.error(f"[premarket-watch] send to {to} failed ({type(e).__name__}: {e})")
        return False


async def _pace() -> None:
    await asyncio.sleep(QUOTE_PACING_S)


# ── pure helpers ─────────────────────────────────────────────────────────────
def _latch_key(today_et: str) -> str:
    return f"{REDIS_LATCH_PREFIX}{today_et}"


def _ignition_key(today_et: str) -> str:
    return f"{IGNITION_KEY_PREFIX}{today_et}"


def _gap_pct(prev_close, live_price) -> Optional[float]:
    """Premarket gap % vs the prior settled close. None when either side is
    missing/non-positive — a gap is never fabricated."""
    try:
        pc = float(prev_close or 0)
        px = float(live_price or 0)
        if pc <= 0 or px <= 0:
            return None
        return round((px - pc) / pc * 100.0, 2)
    except Exception:
        return None


def _passes_filters(price, gap_pct, prev_dollar_vol) -> bool:
    """Doctrine: price >= $5; |gap| >= 3%; prev-session dollar volume >= $20M
    WHEN KNOWN. prev_dollar_vol None/0 means UNKNOWN (the snapshot stores
    below-sweep movers with volume 0) — unknown passes; a KNOWN thin tape is
    rejected. Losers gap down: the |gap| keeps them in (shortable watch)."""
    try:
        if price is None or float(price) < MIN_PRICE:
            return False
        if gap_pct is None or abs(float(gap_pct)) < MIN_ABS_GAP_PCT:
            return False
        if prev_dollar_vol and float(prev_dollar_vol) < MIN_PREV_DOLLAR_VOL:
            return False
        return True
    except Exception:
        return False


def _rank_key(c: dict) -> float:
    """|gap| x catalyst weight — catalyst-free movers rank on gap alone."""
    try:
        g = abs(float(c.get("gap_pct") or 0.0))
    except Exception:
        g = 0.0
    try:
        w = float(c.get("catalyst_weight") or 1.0)
    except Exception:
        w = 1.0
    return g * w


def _rank_candidates(cands: list) -> list:
    return sorted(list(cands or []), key=_rank_key, reverse=True)


def _candidate_json(cands: list) -> str:
    """The Track B pre-lock payload — EXACTLY these five keys per row."""
    return json.dumps([{
        "ticker": c.get("ticker"),
        "prev_close": c.get("prev_close"),
        "premarket_price": c.get("premarket_price"),
        "gap_pct": c.get("gap_pct"),
        "catalyst": c.get("catalyst"),
    } for c in (cands or [])])


def _news_is_fresh(published: str, now_et_naive: datetime,
                   window_h: int = NEWS_WINDOW_H) -> bool:
    """FMP publishedDate ('2026-07-14 11:25:31') is ET-naive (probed live
    2026-07-14: a just-published row matched the ET clock). Unparseable ->
    keep (fail-open: a headline is context, not a trade)."""
    try:
        pub = datetime.strptime(str(published), "%Y-%m-%d %H:%M:%S")
        return pub >= (now_et_naive - timedelta(hours=window_h))
    except Exception:
        return True


# ── candidate sources (each independently graceful) ──────────────────────────
async def _edgar_catalysts() -> dict:
    """{ticker: (weight, one-liner)} for 8-Ks filed in the last
    EDGAR_CATALYST_WINDOW_H hours — same table + weights as the daily pick."""
    from app.engines.options.theta_scanner import _CATALYST_WEIGHTS
    out: dict = {}
    try:
        async with _session_factory()() as db:
            rows = (await db.execute(text(
                "SELECT ticker, item_codes FROM edgar_filings "
                f"WHERE filed_at > NOW() - INTERVAL '{int(EDGAR_CATALYST_WINDOW_H)} hours' "
                "AND ticker IS NOT NULL ORDER BY filed_at DESC"))).fetchall()
        for r in rows:
            try:
                tk = str(r[0] or "").strip().upper()
                if not tk or tk in out:
                    continue
                codes = r[1] or []
                if isinstance(codes, str):
                    codes = json.loads(codes)
                w, reason = 1.0, "fresh 8-K filing"
                for c in codes or []:
                    cw = _CATALYST_WEIGHTS.get(c, 1.0)
                    if cw > w:
                        w, reason = cw, f"8-K item {c}"
                out[tk] = (w, reason)
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"[premarket-watch] edgar read failed ({type(e).__name__}: {e})")
    return out


async def _fetch_premarket_news() -> dict:
    """{symbol: headline} from ONE /stable/news/stock-latest call, filtered to
    the last NEWS_WINDOW_H hours. {} on any failure."""
    out: dict = {}
    try:
        from app.engines.data_feeds import fmp_universe as fu
        rows = await fu._fetch_json_safe(NEWS_URL, {"page": 0, "limit": NEWS_LIMIT},
                                         "stock-news")
        if not isinstance(rows, list):
            return out
        et = _now_et()
        now_naive = et.replace(tzinfo=None) if et is not None else datetime.utcnow()
        for n in rows:
            try:
                if not isinstance(n, dict):
                    continue
                sym = str(n.get("symbol") or "").strip().upper()
                title = str(n.get("title") or "").strip()
                if not sym or not title or sym in out:
                    continue
                if not _news_is_fresh(n.get("publishedDate"), now_naive):
                    continue
                out[sym] = title
            except Exception:
                continue
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[premarket-watch] news fetch failed ({type(e).__name__}: {e})")
    return out


async def _fetch_movers_symbols() -> list:
    """Movers SYMBOLS in list order (gainers, losers, actives) — prices in
    these payloads can be stale premarket, so only the symbols are used;
    price truth comes from the live quote-short pass."""
    syms: list = []
    try:
        from app.engines.data_feeds import fmp_universe as fu
        payloads = await asyncio.gather(
            fu._fetch_json_safe(fu.GAINERS_URL, None, "biggest-gainers"),
            fu._fetch_json_safe(fu.LOSERS_URL, None, "biggest-losers"),
            fu._fetch_json_safe(fu.ACTIVES_URL, None, "most-actives"),
        )
        seen: set = set()
        for payload in payloads:
            if not isinstance(payload, list):
                continue
            for m in payload:
                try:
                    sym = str(m.get("symbol") or "").strip().upper()
                    if sym and sym not in seen:
                        seen.add(sym)
                        syms.append(sym)
                except Exception:
                    continue
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[premarket-watch] movers fetch failed ({type(e).__name__}: {e})")
    return syms


async def _fetch_quote(symbol: str) -> Optional[float]:
    """LIVE premarket price — quote-short updates premarket (single-symbol on
    this plan; the batch form returns [])."""
    from app.engines.data_feeds.fmp_feed import fetch_quote_short_price
    return await fetch_quote_short_price(symbol)


async def _load_prev_map(today_et: str) -> dict:
    """{symbol: {c, v}} — yesterday's real closes from fmp_eod_snapshot."""
    try:
        from app.engines.data_feeds.fmp_eod_snapshot import load_prev_session_map
        return await load_prev_session_map(today_et) or {}
    except Exception as e:
        logger.warning(f"[premarket-watch] prev-map read failed ({type(e).__name__}: {e})")
        return {}


async def _fallback_prev_close(symbol: str) -> Optional[float]:
    """Bounded per-symbol settled-close fetch (sync helper -> thread)."""
    try:
        from app.engines.data_feeds.fmp_feed import fetch_last_settled_close_sync
        return await asyncio.to_thread(fetch_last_settled_close_sync, symbol)
    except Exception:
        return None


# ── the build ────────────────────────────────────────────────────────────────
async def build_watchlist(today_et: str) -> list:
    """Full ranked, filtered candidate list (catalyst names first in the quote
    budget, then movers). Each row: {ticker, prev_close, premarket_price,
    gap_pct, catalyst, catalyst_weight}."""
    edgar = await _edgar_catalysts()
    news = await _fetch_premarket_news()
    movers = await _fetch_movers_symbols()

    order: list = []
    seen: set = set()
    for sym in list(edgar.keys()) + list(news.keys()) + movers:
        if sym and sym not in seen:
            seen.add(sym)
            order.append(sym)
    order = order[:MAX_QUOTES]

    prev_map = await _load_prev_map(today_et)
    fallbacks_left = MAX_CLOSE_FALLBACKS
    out: list = []
    for sym in order:
        px = await _fetch_quote(sym)
        await _pace()
        if not px or float(px) < MIN_PRICE:   # cheap reject before a fallback fetch
            continue
        prev = prev_map.get(sym) or {}
        try:
            prev_close = float(prev.get("c") or 0.0)
        except Exception:
            prev_close = 0.0
        try:
            prev_dollar_vol = prev_close * float(prev.get("v") or 0.0)
        except Exception:
            prev_dollar_vol = 0.0
        if prev_close <= 0 and fallbacks_left > 0:
            fallbacks_left -= 1
            prev_close = float(await _fallback_prev_close(sym) or 0.0)
        gap = _gap_pct(prev_close, px)
        if gap is None or not _passes_filters(px, gap, prev_dollar_vol):
            continue
        w, reason = edgar.get(sym, (1.0, ""))
        catalyst = reason or (news.get(sym) or "")[:110] or "premarket mover"
        out.append({
            "ticker": sym,
            "prev_close": round(prev_close, 4),
            "premarket_price": round(float(px), 4),
            "gap_pct": gap,
            "catalyst": catalyst,
            "catalyst_weight": w,
        })
    return _rank_candidates(out)


# ── email ────────────────────────────────────────────────────────────────────
async def _subscriber_emails() -> list:
    """SAME recipient set as the daily pick / no-pick emails: active users
    with an ACTIVE theta_scanner strategy."""
    try:
        async with _session_factory()() as db:
            rows = (await db.execute(text(
                "SELECT DISTINCT u.email FROM users u JOIN strategies s ON s.user_id = u.id "
                "WHERE s.signal_mode = 'theta_scanner' AND s.status = 'ACTIVE' "
                "AND u.is_active = true"))).fetchall()
        return [r[0] for r in rows if r and r[0]]
    except Exception as e:
        logger.error(f"[premarket-watch] recipient query failed ({type(e).__name__}: {e})")
        return []


def _build_email_html(date_str: str, rows: list) -> str:
    trs = "".join(
        "<tr style='border-top:1px solid #e2e8f0;'>"
        f"<td style='padding:8px;font-weight:700;'>{c['ticker']}</td>"
        f"<td style='padding:8px;text-align:right;'>${c['premarket_price']:.2f}</td>"
        f"<td style='padding:8px;text-align:right;font-weight:700;"
        f"color:{'#16a34a' if float(c['gap_pct']) >= 0 else '#dc2626'};'>{c['gap_pct']:+.1f}%</td>"
        f"<td style='padding:8px;text-align:right;color:#64748b;'>${c['prev_close']:.2f}</td>"
        f"<td style='padding:8px;color:#475569;font-size:12px;'>{html.escape(str(c['catalyst']))}</td>"
        "</tr>"
        for c in rows)
    return f"""<div style="font-family:-apple-system,sans-serif;max-width:640px;margin:0 auto;padding:24px;color:#0f172a;">
      <h1 style="margin:0 0 8px;color:#7c3aed;">🌅 Saro Premarket Watch</h1>
      <p style="color:#64748b;font-size:12px;margin:0 0 16px;">Catalyst-ranked premarket gappers — {date_str}, built 08:45 ET</p>
      <div style="background:#fef3c7;border:1px solid #f59e0b;color:#92400e;padding:10px 12px;border-radius:8px;font-size:13px;font-weight:700;margin:0 0 14px;">Watchlist only — Saro's confirmed pick still fires after 9:33 ET. Not a trade signal.</div>
      <table style="width:100%;border-collapse:collapse;font-size:13px;">
        <tr style="color:#94a3b8;font-size:11px;text-align:left;"><th style="padding:8px;">Ticker</th><th style="padding:8px;text-align:right;">Premarket</th><th style="padding:8px;text-align:right;">Gap</th><th style="padding:8px;text-align:right;">Prev close</th><th style="padding:8px;">Catalyst</th></tr>
        {trs}
      </table>
      <p style="font-size:10px;color:#94a3b8;margin-top:20px;">Prices are live FMP quotes at build time; gaps vs the prior session close. Premarket tape can be thin — expect slippage vs these marks.</p>
    </div>"""


# ── the run ──────────────────────────────────────────────────────────────────
async def run_premarket_watch(today_et: str) -> int:
    """Build -> pre-lock -> email. Returns emails sent (0 when the watchlist
    is empty — an empty morning sends nothing by design, but the pre-lock key
    is still written so Track B knows the build ran and found nothing)."""
    rows = await build_watchlist(today_et)

    # PRE-LOCK for Track B ignition — the FULL ranked filtered list.
    try:
        _get_redis().set(_ignition_key(today_et), _candidate_json(rows),
                         ex=IGNITION_TTL_S)
    except Exception as e:
        logger.warning(f"[premarket-watch] ignition pre-lock write failed "
                       f"({type(e).__name__}: {e})")

    if not rows:
        logger.info(f"[premarket-watch] {today_et}: no qualifying names — no email")
        return 0

    top = rows[:TOP_N]
    subject = f"🌅 Saro Premarket Watch — {today_et}"
    html = _build_email_html(today_et, top)
    sent = 0
    for email in await _subscriber_emails():
        try:
            if await _send_email(email, subject, html):
                sent += 1
        except Exception as e:
            logger.error(f"[premarket-watch] emit to {email} failed "
                         f"({type(e).__name__}: {e})")
    logger.info(f"[premarket-watch] {today_et}: {len(rows)} candidates, "
                f"emailed top {len(top)} to {sent} subscribers")
    return sent


# ── scheduler hook — once per ET trading day, fully isolated ─────────────────
async def _check_and_run_premarket_watch() -> None:
    """Called each premarket_scheduler loop iteration. Fires once per trading
    day inside the 08:45-09:25 ET window (SETNX latch, ET-anchored date).
    Gated by PREMARKET_WATCH_ENABLED (default on). Failures are swallowed and
    the latch is KEPT — a duplicate blast is worse than a missed morning."""
    try:
        if (os.environ.get("PREMARKET_WATCH_ENABLED", "1") or "1").strip() != "1":
            return
        et = _now_et()
        if et is None or et.weekday() >= 5:
            return
        try:
            from app.engines.market_calendar import is_trading_day
            if not is_trading_day(et.date()):
                return  # full-day market holiday
        except Exception:
            pass  # calendar unavailable -> the weekday gate above still holds
        t = et.hour * 60 + et.minute
        lo = WATCH_FIRE_ET[0] * 60 + WATCH_FIRE_ET[1]
        hi = WATCH_WINDOW_END_ET[0] * 60 + WATCH_WINDOW_END_ET[1]
        if t < lo or t > hi:
            return
        today_key = et.strftime("%Y-%m-%d")   # ET-anchored — NEVER the UTC date
        try:
            r = _get_redis()
            if not r.set(_latch_key(today_key), "running", ex=LATCH_TTL_S, nx=True):
                return  # already ran today (or another worker owns it)
        except Exception:
            return  # no redis latch -> skip rather than risk a duplicate blast
        logger.info(f"[premarket-watch] firing {et.strftime('%H:%M ET')} ({today_key})")
        try:
            await run_premarket_watch(today_key)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Latch intentionally NOT released: a partial send may already be
            # in subscriber inboxes.
            logger.error(f"[premarket-watch] run failed ({type(e).__name__}: {e})")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[premarket-watch] check failed ({type(e).__name__}: {e})")
