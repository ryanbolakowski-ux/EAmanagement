"""Saro 1.3 screen — bulk FMP screener -> capped daily-history compression
funnel (S1+S2) -> M&A hard exclude (S3, FMP half).

FMP BUDGET (worst case per trading day, all /stable, all redis-cached
per-ET-day under saro13:* keys so a same-day rerun costs ~0):
    1   company-screener (one page, client-side NASDAQ/NYSE filter)
 + 30   historical-price-eod/full (DAILY_HISTORY_CAP — hard counter; the
        funnel STOPS pulling new histories at the cap, cache hits are free)
 +  1   mergers-acquisitions-latest (ONE list, cached per day)
 + 10   news/stock headline checks (NEWS_CHECK_CAP, S1/S2 survivors only)
 = 42   calls/day max (<= the ~45 ceiling; morning funnel ~79 + saro12 ~76)

M&A FILTER POLARITY (owner instruction — do not invert):
  * confirmed hit (targetedSymbol match OR spec-keyword headline)
        -> fail CLOSED: the ticker is HARD EXCLUDED
  * data unavailable (both endpoints down/unreadable)
        -> fail OPEN with a logged note — an outage must not zero the list

LIVE-PARTIAL RULE: FMP EOD returns today's row intraday as a live partial;
indicators.daily_rows_to_df drops rows dated >= today ET and sorts date
desc explicitly before reversing (copied from _bridge_fetch_prev_eod's
hardening). All compression math runs on COMPLETED sessions only.

SHADOW ONLY: this module reads market data and writes redis caches. It
never emails, never orders, never routes.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional

from app.engines.scanner.saro13 import config as C

logger = logging.getLogger(__name__)

MA_KEYWORD_RE = re.compile(C.MA_KEYWORD_PATTERN, re.IGNORECASE)


# ── pure, unit-testable filters ────────────────────────────────────────────
def normalize_exchange(row: dict) -> str:
    """Map FMP exchange spellings to 'NASDAQ' / 'NYSE' / '' (other)."""
    raw = str(row.get("exchangeShortName") or row.get("exchange") or "").upper()
    if "NASDAQ" in raw:
        return "NASDAQ"
    if raw == "NYSE" or "NEW YORK STOCK EXCHANGE" in raw:
        return "NYSE"
    return ""


def passes_base_screen(row: dict) -> tuple[bool, str]:
    """Universe pre-filter on one bulk-screener row (engineering choice —
    liquid optionable names; the owner spec starts at S1). Returns
    (ok, reject_reason)."""
    try:
        price = float(row.get("price") or 0)
        volume = float(row.get("volume") or 0)
        mcap = float(row.get("marketCap") or 0)
    except Exception:
        return False, "unparseable row"
    if not (C.PRICE_MIN <= price <= C.PRICE_MAX):
        return False, f"price {price} outside ${C.PRICE_MIN:g}-${C.PRICE_MAX:g}"
    if volume < C.DAILY_VOLUME_MIN:
        return False, f"volume {volume:.0f} < {C.DAILY_VOLUME_MIN}"
    if mcap < C.MARKET_CAP_MIN:
        return False, f"marketCap {mcap:.0f} < {C.MARKET_CAP_MIN}"
    if normalize_exchange(row) not in C.ALLOWED_EXCHANGES:
        return False, (f"exchange '{row.get('exchangeShortName') or row.get('exchange')}' "
                       f"not NASDAQ/NYSE")
    if row.get("isEtf") is True or row.get("isFund") is True:
        return False, "ETF/fund"
    if row.get("isActivelyTrading") is False:
        return False, "not actively trading"
    return True, ""


def ma_target_hit(ma_rows: Optional[list], sym: str) -> bool:
    """True when `sym` appears as targetedSymbol in the M&A-latest list —
    a CONFIRMED pending buyout/acquisition/take-private (hard exclude)."""
    if not ma_rows:
        return False
    s = str(sym).upper()
    for row in ma_rows:
        try:
            if str(row.get("targetedSymbol") or "").upper() == s:
                return True
        except Exception:
            continue
    return False


def news_keyword_hit(articles: Optional[list]) -> Optional[str]:
    """Headline keyword fallback (spec: buyout|acquisition|merger|
    take-private). Scans TITLES only — returns the matching headline, or
    None. Empty/None input -> None (no evidence, not an exclusion)."""
    if not articles:
        return None
    for a in articles:
        try:
            title = str(a.get("title") or "")
            if title and MA_KEYWORD_RE.search(title):
                return title
        except Exception:
            continue
    return None


# ── redis helpers (per-ET-day caches) ──────────────────────────────────────
def _redis():
    import redis.asyncio as _redis
    return _redis.from_url(os.environ.get(C.REDIS_URL_ENV, C.REDIS_URL_DEFAULT),
                           decode_responses=True)


async def _cache_get_json(r, key: str):
    try:
        raw = await r.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def _cache_set_json(r, key: str, value, ttl_s: int = C.DAY_CACHE_TTL_S):
    try:
        await r.set(key, json.dumps(value), ex=ttl_s)
    except Exception as e:
        logger.warning(f"[saro13-screen] cache set {key} failed: {e}")


# ── data fetchers (lazy imports; failures fail open per candidate) ─────────
async def _bulk_screener(r, date_key: str) -> list[dict]:
    """ONE company-screener page (redis-cached per ET day)."""
    cached = await _cache_get_json(r, C.SCREEN_CACHE_FMT.format(date=date_key))
    if isinstance(cached, list) and cached:
        logger.info(f"[saro13-screen] screener cache hit ({len(cached)} rows, 0 FMP calls)")
        return cached
    from app.engines.data_feeds.fmp_universe import _fmp_get_json
    try:
        rows = await _fmp_get_json(C.SCREENER_URL, {
            "priceMoreThan": C.PRICE_MIN,
            "priceLowerThan": C.PRICE_MAX,
            "volumeMoreThan": C.DAILY_VOLUME_MIN,
            "marketCapMoreThan": C.MARKET_CAP_MIN,
            "limit": C.SCREENER_LIMIT,
        })
    except Exception as e:
        logger.warning(f"[saro13-screen] bulk screener failed: {e}")
        return []
    rows = rows if isinstance(rows, list) else []
    if rows:
        await _cache_set_json(r, C.SCREEN_CACHE_FMT.format(date=date_key), rows)
    return rows


async def _fetch_daily_history(r, date_key: str, sym: str,
                               budget: dict) -> Optional[list]:
    """~100 trading days of daily OHLCV for `sym` (redis-cached per ET day).
    Counts REAL FMP pulls against budget['eod_pulls']; returns None when the
    DAILY_HISTORY_CAP is exhausted (budget['cap_hit'] set) or on failure."""
    key = C.DAILY_CACHE_FMT.format(date=date_key, ticker=sym)
    cached = await _cache_get_json(r, key)
    if isinstance(cached, list) and cached:
        return cached
    if budget.get("eod_pulls", 0) >= C.DAILY_HISTORY_CAP:
        budget["cap_hit"] = True
        return None
    budget["eod_pulls"] = budget.get("eod_pulls", 0) + 1
    from app.engines.data_feeds.fmp_universe import _fmp_get_json
    try:
        from_date = (datetime.strptime(date_key, "%Y-%m-%d")
                     - timedelta(days=C.EOD_FROM_DAYS)).strftime("%Y-%m-%d")
        j = await _fmp_get_json(C.EOD_HIST_URL, {"symbol": sym, "from": from_date})
    except Exception as e:
        logger.debug(f"[saro13-screen] daily history {sym} failed: {e}")
        return None
    if isinstance(j, dict):
        j = j.get("historical")  # tolerate the wrapped variant
    rows = [r0 for r0 in (j or []) if isinstance(r0, dict)]
    if rows:
        await _cache_set_json(r, key, rows)
    return rows or None


async def _ma_list(r, date_key: str) -> Optional[list]:
    """mergers-acquisitions-latest — ONE call, cached per ET day. Returns
    None only when the endpoint is UNAVAILABLE (drives the fail-open leg);
    an empty-but-successful list is [] (usable evidence of no deals)."""
    key = C.MA_CACHE_FMT.format(date=date_key)
    cached = await _cache_get_json(r, key)
    if isinstance(cached, list):
        return cached
    from app.engines.data_feeds.fmp_universe import _fmp_get_json
    try:
        rows = await _fmp_get_json(C.MA_LATEST_URL, {"page": 0, "limit": 1000})
    except Exception as e:
        logger.warning(f"[saro13-screen] M&A list unavailable: {e}")
        return None
    rows = rows if isinstance(rows, list) else []
    await _cache_set_json(r, key, rows)
    return rows


async def _news_headlines(r, date_key: str, sym: str,
                          budget: dict) -> Optional[list]:
    """news/stock headlines for `sym` (cached per ET day, NEWS_CHECK_CAP).
    None = unavailable/uncheckable (fail-open leg), list = usable."""
    key = C.NEWS_CACHE_FMT.format(date=date_key, ticker=sym)
    cached = await _cache_get_json(r, key)
    if isinstance(cached, list):
        return cached
    if budget.get("news_checks", 0) >= C.NEWS_CHECK_CAP:
        logger.info(f"[saro13-screen] news-check cap ({C.NEWS_CHECK_CAP}) reached "
                    f"— {sym} headline check skipped")
        return None
    budget["news_checks"] = budget.get("news_checks", 0) + 1
    from app.engines.data_feeds.fmp_universe import _fmp_get_json
    try:
        rows = await _fmp_get_json(C.NEWS_STOCK_URL,
                                   {"symbols": sym, "limit": C.NEWS_LIMIT})
    except Exception as e:
        logger.debug(f"[saro13-screen] news check {sym} failed: {e}")
        return None
    if isinstance(rows, dict):
        rows = rows.get("content") or rows.get("news")  # tolerate wrappers
    rows = rows if isinstance(rows, list) else []
    await _cache_set_json(r, key, rows)
    return rows


async def ma_gate(r, date_key: str, sym: str, budget: dict) -> tuple[bool, str]:
    """S3 M&A hard exclude. Returns (excluded, note).
    Fail CLOSED on a confirmed hit; fail OPEN (with a log) only when BOTH
    evidence sources are unavailable."""
    ma_rows = await _ma_list(r, date_key)
    if ma_target_hit(ma_rows, sym):
        return True, "confirmed M&A target (mergers-acquisitions-latest targetedSymbol) — HARD EXCLUDE"
    articles = await _news_headlines(r, date_key, sym, budget)
    headline = news_keyword_hit(articles)
    if headline:
        return True, f"M&A headline keyword hit — HARD EXCLUDE: {headline[:120]!r}"
    if ma_rows is None and articles is None:
        logger.warning(f"[saro13-screen] {sym}: M&A status UNVERIFIABLE "
                       f"(list + news both unavailable) — FAIL-OPEN")
        return False, "M&A status unverified (endpoints unavailable) — fail-open"
    return False, "no pending M&A evidence (targetedSymbol list + headlines clean)"


# ── the funnel ─────────────────────────────────────────────────────────────
async def run_screen(*, r=None, date_key: Optional[str] = None) -> list[dict]:
    """S1+S2+S3(M&A) funnel. Returns candidate dicts:
      {ticker, price, today_vol, close, prev_close, live_chg_pct, upper_bb,
       lower_bb, bb_width, swing_low, compression_range, rsi, atr,
       vol_ratio, screen_reasons, comp_detail}
    Also writes the candidate ticker list to redis (CANDIDATES_KEY_FMT) for
    the nightly IV-snapshot job. Never raises; failures -> shorter list.
    The IV-Rank gate (S3, needs Postgres) is applied by shadow.py."""
    own_r = r is None
    if r is None:
        r = _redis()
    budget: dict = {"eod_pulls": 0, "news_checks": 0, "cap_hit": False}
    try:
        if date_key is None:
            from app.engines.options.ignition_shadow import _now_et
            date_key = _now_et().strftime("%Y-%m-%d")

        from app.engines.scanner.saro13 import indicators as I

        rows = await _bulk_screener(r, date_key)
        base = []
        for row in rows:
            sym = str(row.get("symbol") or "").strip().upper()
            if not sym or not sym.isalnum():
                continue
            ok, _why = passes_base_screen(row)
            if ok:
                base.append(row)
        logger.info(f"[saro13-screen] base screen: {len(base)}/{len(rows)} rows pass")

        # deterministic pre-rank before the capped history pulls: dollar
        # volume desc (most liquid chains first — theta viability bias;
        # compression itself is only knowable after the history pull)
        base.sort(key=lambda x: float(x.get("price") or 0) * float(x.get("volume") or 0),
                  reverse=True)

        out: list[dict] = []
        n_hist = n_comp = 0
        for row in base:
            if budget["cap_hit"]:
                break
            sym = str(row["symbol"]).upper()
            hist = await _fetch_daily_history(r, date_key, sym, budget)
            if budget["cap_hit"]:
                logger.info(f"[saro13-screen] daily-history cap "
                            f"({C.DAILY_HISTORY_CAP}) reached — funnel stops")
                break
            if not hist:
                logger.debug(f"[saro13-screen] {sym}: no daily history — drop")
                continue
            n_hist += 1
            st = I.compression_state(I.daily_rows_to_df(hist, today_et=date_key))
            if not st["compressed"]:
                logger.info(f"[saro13-screen] {sym} S1/S2 reject: "
                            f"{'; '.join(st['reject_reasons'][:3])}")
                continue
            n_comp += 1
            # S1 live leg: today's move so far vs the last COMPLETED close
            # (screener price = best-available live). The coil must not
            # already be breaking while we scan.
            live_ok, live_pct = I.passes_daily_change(row.get("price"), st["close"])
            if not live_ok:
                logger.info(f"[saro13-screen] {sym} live daily change "
                            f"{live_pct:+.2f}% > +{C.DAILY_CHANGE_MAX_PCT:g}% — reject")
                continue
            excluded, ma_note = await ma_gate(r, date_key, sym, budget)
            if excluded:
                logger.info(f"[saro13-screen] {sym} M&A exclude: {ma_note}")
                continue
            reasons = [
                (f"BB width {st['bb_width']} <= p{C.BBWIDTH_LOWEST_PCT:g} "
                 f"{st['bb_width_thresh']} of trailing {C.BBWIDTH_RANGE_SESSIONS}d "
                 f"(lowest-{C.BBWIDTH_LOWEST_PCT:g}% coil)"),
                f"ATR({C.ATR_PERIOD}) strictly declining {C.ATR_DECLINE_SESSIONS} sessions",
                f"RSI({C.RSI_PERIOD}) {st['rsi']} in [{C.RSI_BAND_LO:g},{C.RSI_BAND_HI:g}]",
                (f"volume {st['vol']:,.0f} < SMA{C.VOL_SMA_PERIOD} "
                 f"{st['vol_sma']:,.0f} (contraction, ratio {st['vol_ratio']})"),
                (f"daily change ok: last session {st['last_chg_pct']:+.2f}%, "
                 f"live {live_pct if live_pct is not None else 'n/a'}% "
                 f"<= +{C.DAILY_CHANGE_MAX_PCT:g}%"),
                ma_note,
            ]
            out.append({"ticker": sym, "price": float(row.get("price") or 0),
                        "today_vol": int(float(row.get("volume") or 0)),
                        "close": st["close"], "prev_close": st["prev_close"],
                        "live_chg_pct": live_pct,
                        "upper_bb": st["upper_bb"], "lower_bb": st["lower_bb"],
                        "bb_width": st["bb_width"], "swing_low": st["swing_low"],
                        "compression_range": st["compression_range"],
                        "rsi": st["rsi"], "atr": st["atr"],
                        "vol_ratio": st["vol_ratio"],
                        "screen_reasons": reasons, "comp_detail": st})
            logger.info(f"[saro13-screen] {sym} COILED: width {st['bb_width']}, "
                        f"RSI {st['rsi']}, vol ratio {st['vol_ratio']}")

        # persist the candidate list for the nightly Tradier IV snapshot
        try:
            await _cache_set_json(r, C.CANDIDATES_KEY_FMT.format(date=date_key),
                                  [c["ticker"] for c in out])
        except Exception:
            pass
        logger.info(f"[saro13-screen] done: {len(out)} candidates "
                    f"({n_hist} histories -> {n_comp} coiled -> {len(out)} after "
                    f"live-change + M&A); FMP calls this run: "
                    f"~{1 + budget['eod_pulls'] + 1 + budget['news_checks']} "
                    f"(budget max {1 + C.DAILY_HISTORY_CAP + 1 + C.NEWS_CHECK_CAP})")
        return out
    except Exception as e:
        logger.error(f"[saro13-screen] run_screen failed: {e}")
        return []
    finally:
        if own_r:
            try:
                await r.aclose()
            except Exception:
                pass
