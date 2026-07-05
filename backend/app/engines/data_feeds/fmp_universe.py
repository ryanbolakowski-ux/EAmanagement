"""FMP-sourced candidate universe for the Saro funnel (TRACK fmp-universe).

Produces rows in the EXACT grouped-daily shape momentum_scanner's
_fetch_market_snapshot returns (and funnel._coarse consumes):

    {ticker, day: {c, v}, prevDay: {c, v}, lastTrade: {p}, todaysChangePerc}

Nothing switches live here: the scanner only routes to this module when
SARO_UNIVERSE=fmp (default polygon, unchanged behavior), and the
[universe-compare] shadow hook only runs when SARO_UNIVERSE_SHADOW=fmp.

SOURCING (<= 5 HTTP requests per build, 60s in-process TTL):
  1-3. /stable/biggest-gainers | biggest-losers | most-actives (50 rows each)
       — the movers the funnel actually hunts. Payload (probed live
       2026-07-05): symbol, price, change, changesPercentage, exchange.
       NO volume field (the planning brief assumed movers carry volume —
       verified live that they do NOT).
  4.   /stable/company-screener?volumeMoreThan=500k&marketCapMoreThan=10M&
       limit=10000 — ONE page returns the full sweep (~4k rows incl. ETFs
       like QQQ/SPY; probed live: limit=10000 accepted, 3962 rows matched, so
       one page covers it). Fields: symbol, price, volume. `volume` matches
       quote-short's CUMULATIVE day volume exactly (probed NVDA/CLRO), i.e.
       it is LIVE intraday cumulative volume during a session — this is the
       day.v join for mover symbols and the widening sweep.
  5.   Polygon grouped-daily for the most recent COMPLETED session
       → prevDay {c, v}. Prev-day data is static after the close, so the
       15-min-DELAYED Polygon tier is perfectly fine for it.

FIELD-MAPPING HONESTY / SEMANTIC SHIFTS (sanity-check funnel thresholds via
the [universe-compare] log line before any flip):
  * prevDay.c: movers derive prevClose = price - change from FMP's own
    numbers (self-consistent with the live price; cross-checked against
    /stable/quote previousClose live). Screener-only rows take the Polygon
    completed-session close.
  * rel_vol DENOMINATOR (prevDay.v): the planned avgVolume/averageVolume
    field does NOT exist anywhere on this plan (probed: stable screener and
    stable full quote both lack it; /api/v3 is dead). We use the real
    previous-session volume from Polygon grouped-daily instead — the SAME
    denominator semantics the current funnel path already has, so rel_vol_min
    thresholds keep their meaning.
  * rel_vol NUMERATOR (day.v): LIVE cumulative day volume. During the
    morning scan this is a PARTIAL session (premarket + first RTH minutes),
    so rel_vol reads LOWER than the current Polygon path, which (delayed
    tier) compares two COMPLETED sessions (yesterday vs the day before —
    i.e. it scans yesterday's gappers). FMP's day side is fresher; the
    numerator is partial. The compare hook carries this note.
  * Movers below the screener sweep (volume < 500k or mcap < $10M) get
    day.v = 0 — unknown volume is reported as 0, never fabricated; the
    funnel's dollar_vol_min drops them. Screener-only symbols missing from
    the Polygon prev map are SKIPPED (no honest gap/rel_vol baseline).
  * Nothing is excluded here by design (leveraged ETFs etc.) — funnel._coarse
    already handles exclusions downstream.

RATE DISCIPLINE: <= 5 requests per build (3 movers + 1 screener + 1 Polygon
grouped; the Polygon probe walks back over weekend/holiday dates only on
non-200/empty days), 60s TTL cache, ONE shared aiohttp session (reused from
fmp_feed), hard per-request timeouts, every sub-fetch independently graceful.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from app.engines.data_feeds.fmp_feed import FMPHTTPError, _env_api_key, _get_session

# ── Endpoints (stable API only — /api/v3 is dead for this account) ──────────
GAINERS_URL = "https://financialmodelingprep.com/stable/biggest-gainers"
LOSERS_URL = "https://financialmodelingprep.com/stable/biggest-losers"
ACTIVES_URL = "https://financialmodelingprep.com/stable/most-actives"
SCREENER_URL = "https://financialmodelingprep.com/stable/company-screener"
POLYGON_GROUPED_URL = "https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date}"

# Screener sweep knobs. volumeMoreThan is tuned to the funnel's cheapest
# dollar-vol gate; marketCapMoreThan is a low bar that keeps CLRO-class
# movers ($11M mcap) while cutting shells. limit=10000 returns the whole
# sweep in ONE page at these filters (probed 2026-07-05: 3962 rows).
SCREENER_VOLUME_MIN = 500_000
SCREENER_MCAP_MIN = 10_000_000
SCREENER_LIMIT = 10_000

REQUEST_TIMEOUT_S = 12.0

# The scanner treats a thinner-than-this universe as a failed build and falls
# back to the Polygon path (same floor as the Polygon grouped-daily check).
FMP_MIN_UNIVERSE_ROWS = 200

# ── 60s in-process TTL cache (mirrors momentum_scanner._snapshot_cache) ─────
UNIVERSE_TTL_S = 60.0
_universe_cache: dict = {"fetched_at": 0.0, "rows": None}

# [universe-compare] shadow-hook throttle: at most one comparison per this
# many seconds, no matter how often the snapshot is rebuilt.
COMPARE_MIN_INTERVAL_S = 300.0
_compare_last_mono = 0.0


def clear_universe_cache() -> None:
    """Drop the universe cache and compare throttle (tests / ops)."""
    global _compare_last_mono
    _universe_cache["fetched_at"] = 0.0
    _universe_cache["rows"] = None
    _compare_last_mono = 0.0


# ── HTTP choke points (single seams; tests monkeypatch these) ───────────────
async def _fmp_get_json(url: str, params: Optional[dict] = None):
    """One FMP stable-API GET via the shared aiohttp session. Raises
    FMPHTTPError on non-200 so callers can decide fallback behavior."""
    import aiohttp

    key = _env_api_key()
    session = _get_session()
    async with session.get(
        url,
        params={**(params or {}), "apikey": key},
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
    ) as resp:
        if resp.status != 200:
            raise FMPHTTPError(resp.status)
        return await resp.json(content_type=None)


async def _polygon_prev_session_map() -> dict:
    """{ticker: {c, v, ...}} for the most recent COMPLETED grouped-daily
    session, walking back over weekends/holidays. {} on any failure — the
    caller degrades gracefully (movers keep their derived prevClose but lose
    rel_vol; the scanner's row floor then forces the Polygon fallback)."""
    import aiohttp

    api_key = (os.environ.get("POLYGON_API_KEY", "") or "").strip()
    if not api_key:
        return {}
    session = _get_session()
    today = datetime.now(timezone.utc).date()
    for back in range(1, 8):
        d = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        try:
            async with session.get(
                POLYGON_GROUPED_URL.format(date=d),
                params={"adjusted": "true", "apiKey": api_key},
                timeout=aiohttp.ClientTimeout(total=15.0),
            ) as resp:
                if resp.status != 200:
                    continue
                j = await resp.json(content_type=None)
            if j.get("status") in ("OK", "DELAYED") and (j.get("resultsCount") or 0) > 100:
                return {r.get("T"): r for r in (j.get("results") or []) if r.get("T")}
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
    return {}


# ── Row building (pure — unit-testable without any HTTP) ────────────────────
def _row(ticker: str, price: float, day_vol: float, prev_close: float,
         prev_vol: float, change_pct: float) -> dict:
    """One row in the exact grouped-daily shape _coarse/scan_for_momentum eat."""
    return {
        "ticker": ticker,
        "day": {"c": price, "v": int(day_vol)},
        "prevDay": {"c": prev_close, "v": int(prev_vol)},
        "lastTrade": {"p": price},
        "todaysChangePerc": change_pct,
    }


def _build_universe_rows(mover_payloads: list, screener_payload, prev_map: dict) -> list:
    """Merge movers + screener sweep + Polygon prev-session map into snapshot
    rows. Dedupe by symbol (movers win — they carry the live change data).
    EXCLUDES nothing by design; the funnel handles exclusions downstream."""
    screener_by_sym: dict = {}
    if isinstance(screener_payload, list):
        for s in screener_payload:
            try:
                if not isinstance(s, dict):
                    continue
                sym = str(s.get("symbol") or "").strip().upper()
                if sym and sym not in screener_by_sym:
                    screener_by_sym[sym] = s
            except Exception:
                continue

    rows: list = []
    seen: set = set()
    mover_vol_misses = 0

    # Movers first: price/change/changesPercentage are live; prevClose is
    # derived from FMP's own numbers; day.v joined from the screener sweep.
    for payload in mover_payloads or []:
        if not isinstance(payload, list):
            continue
        for m in payload:
            try:
                if not isinstance(m, dict):
                    continue
                sym = str(m.get("symbol") or "").strip().upper()
                if not sym or sym in seen:
                    continue
                price = float(m.get("price") or 0.0)
                change = float(m.get("change") or 0.0)
                prev_close = price - change
                if price <= 0 or prev_close <= 0:
                    continue
                scr = screener_by_sym.get(sym)
                day_vol = float((scr or {}).get("volume") or 0.0)
                if day_vol <= 0:
                    mover_vol_misses += 1  # unknown volume stays 0 — never fabricated
                prev_vol = float(((prev_map or {}).get(sym) or {}).get("v") or 0.0)
                change_pct = float(m.get("changesPercentage") or
                                   ((price - prev_close) / prev_close * 100.0))
                rows.append(_row(sym, price, day_vol, prev_close, prev_vol, change_pct))
                seen.add(sym)
            except Exception:
                continue

    # Screener sweep widens coverage. These rows have no FMP change field, so
    # BOTH prev-side values come from the Polygon completed session; symbols
    # without a prev-map entry are skipped (no honest gap/rel_vol baseline).
    for sym, s in screener_by_sym.items():
        try:
            if sym in seen:
                continue
            pg = (prev_map or {}).get(sym)
            if not pg:
                continue
            price = float(s.get("price") or 0.0)
            prev_close = float(pg.get("c") or 0.0)
            if price <= 0 or prev_close <= 0:
                continue
            day_vol = float(s.get("volume") or 0.0)
            prev_vol = float(pg.get("v") or 0.0)
            change_pct = (price - prev_close) / prev_close * 100.0
            rows.append(_row(sym, price, day_vol, prev_close, prev_vol, change_pct))
            seen.add(sym)
        except Exception:
            continue

    if mover_vol_misses:
        logger.info(f"[fmp-universe] {mover_vol_misses} mover(s) below the screener sweep "
                    "kept day.v=0 (unknown volume is never fabricated)")
    return rows


# ── The universe build ───────────────────────────────────────────────────────
async def _fetch_json_safe(url: str, params: Optional[dict], label: str):
    """One graceful sub-fetch: any failure logs and returns None."""
    try:
        return await _fmp_get_json(url, params)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[fmp-universe] {label} fetch failed ({type(e).__name__}: {e})")
        return None


async def fetch_fmp_universe(ttl_s: float = UNIVERSE_TTL_S) -> list:
    """Build the FMP candidate universe (grouped-daily-shaped rows).

    <= 5 HTTP requests, 60s TTL cache, every sub-source independently
    graceful. Returns [] / a thin list on failure — the scanner wiring treats
    anything under FMP_MIN_UNIVERSE_ROWS as a failed build and falls back to
    the Polygon path."""
    now = time.monotonic()
    if _universe_cache["rows"] is not None and (now - _universe_cache["fetched_at"]) < ttl_s:
        return _universe_cache["rows"]
    if not _env_api_key():
        logger.warning("[fmp-universe] FMP_API_KEY is empty — returning empty universe")
        return []

    gainers, losers, actives, screener, prev_map = await asyncio.gather(
        _fetch_json_safe(GAINERS_URL, None, "biggest-gainers"),
        _fetch_json_safe(LOSERS_URL, None, "biggest-losers"),
        _fetch_json_safe(ACTIVES_URL, None, "most-actives"),
        _fetch_json_safe(SCREENER_URL, {
            "volumeMoreThan": SCREENER_VOLUME_MIN,
            "marketCapMoreThan": SCREENER_MCAP_MIN,
            "limit": SCREENER_LIMIT,
        }, "company-screener"),
        _polygon_prev_session_map(),
    )

    rows = _build_universe_rows([gainers, losers, actives], screener, prev_map)
    n_movers = sum(len(p) for p in (gainers, losers, actives) if isinstance(p, list))
    logger.info(
        f"[fmp-universe] built {len(rows)} rows "
        f"(movers={n_movers}, screener={len(screener) if isinstance(screener, list) else 0}, "
        f"polygon_prev={len(prev_map or {})})"
    )
    _universe_cache["rows"] = rows
    _universe_cache["fetched_at"] = time.monotonic()
    return rows


# ── [universe-compare] — the Monday parallel-validation logger ───────────────
def _dollar_vol(row: dict) -> float:
    day = row.get("day") or {}
    try:
        return float(day.get("c") or 0.0) * float(day.get("v") or 0.0)
    except Exception:
        return 0.0


def _top_by_dollar_vol(rows: list, n: int) -> list:
    ranked = sorted(rows or [], key=_dollar_vol, reverse=True)
    return [str(r.get("ticker") or "") for r in ranked[:n] if r.get("ticker")]


def _funnel_top(rows: list, n: int = 15) -> list:
    """Top-n tickers this universe would actually feed the funnel: stage-1
    coarse gate of the flagship template (momentum_breakout), ranked by
    dollar volume. Falls back to raw dollar-vol ranking if the funnel import
    fails — the compare line must never die on a refactor."""
    try:
        from app.engines.scanner.definitions import TEMPLATES
        from app.engines.scanner.funnel import _coarse

        tpl = TEMPLATES["momentum_breakout"]
        cands = [c for c in (_coarse(tpl, r) for r in (rows or [])) if c]
        cands.sort(key=lambda c: c.get("dollar_vol", 0.0), reverse=True)
        return [c["ticker"] for c in cands[:n]]
    except Exception:
        return _top_by_dollar_vol(rows, n)


def _compare_summary(polygon_rows: list, fmp_rows: list) -> dict:
    """One structured comparison payload — the flip/no-flip evidence."""
    poly_syms = {str(r.get("ticker") or "").upper() for r in (polygon_rows or [])}
    fmp_syms = {str(r.get("ticker") or "").upper() for r in (fmp_rows or [])}
    poly_top50 = set(_top_by_dollar_vol(polygon_rows, 50))
    fmp_top50 = set(_top_by_dollar_vol(fmp_rows, 50))
    poly_top15 = _funnel_top(polygon_rows, 15)
    fmp_top15 = _funnel_top(fmp_rows, 15)
    return {
        "polygon_rows": len(polygon_rows or []),
        "fmp_rows": len(fmp_rows or []),
        "top50_dollar_vol_overlap": len(poly_top50 & fmp_top50),
        "polygon_top15_funnel": poly_top15,
        "fmp_top15_funnel": fmp_top15,
        "fmp_top15_missing_from_polygon_universe": [t for t in fmp_top15 if t not in poly_syms],
        "polygon_top15_missing_from_fmp_universe": [t for t in poly_top15 if t not in fmp_syms],
        "note": ("fmp day.v=live cumulative (partial intraday at scan time); "
                 "rel_vol denominator=prev completed session (same as polygon path; "
                 "no avgVolume on this plan); polygon day side=last completed session"),
    }


async def _run_universe_compare(polygon_rows: list) -> None:
    """The fire-and-forget compare task body. NEVER raises."""
    try:
        fmp_rows = await fetch_fmp_universe()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[universe-compare] fmp universe fetch failed "
                       f"({type(e).__name__}: {e}) — no comparison this cycle")
        return
    try:
        summary = _compare_summary(polygon_rows, fmp_rows)
        logger.info("[universe-compare] " + json.dumps(summary, default=str))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[universe-compare] comparison failed ({type(e).__name__}: {e})")


def maybe_spawn_universe_compare(polygon_rows: list):
    """Shadow parallel-validation hook, called by momentum_scanner right after
    a REAL Polygon snapshot is built. Fire-and-forget, fully try/excepted,
    default OFF: it only spawns when SARO_UNIVERSE=polygon (i.e. FMP is not
    live) AND SARO_UNIVERSE_SHADOW=fmp, at most once per
    COMPARE_MIN_INTERVAL_S. Returns the spawned task (test hook) or None."""
    global _compare_last_mono
    try:
        source = (os.environ.get("SARO_UNIVERSE", "polygon") or "polygon").strip().lower()
        shadow = (os.environ.get("SARO_UNIVERSE_SHADOW", "") or "").strip().lower()
        if source != "polygon" or shadow != "fmp":
            return None
        now = time.monotonic()
        if _compare_last_mono and (now - _compare_last_mono) < COMPARE_MIN_INTERVAL_S:
            return None
        _compare_last_mono = now
        return asyncio.get_running_loop().create_task(
            _run_universe_compare(list(polygon_rows or [])))
    except Exception as e:
        try:
            logger.warning(f"[universe-compare] hook spawn failed ({type(e).__name__}: {e})")
        except Exception:
            pass
        return None
