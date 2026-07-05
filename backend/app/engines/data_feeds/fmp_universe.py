"""FMP-sourced candidate universe for the Saro funnel (TRACK fmp-universe,
prev-day join reworked under TRACK fmp-self-sufficiency).

Produces rows in the EXACT grouped-daily shape momentum_scanner's
_fetch_market_snapshot returns (and funnel._coarse consumes):

    {ticker, day: {c, v}, prevDay: {c, v}, lastTrade: {p}, todaysChangePerc}

Nothing switches live here: the scanner only routes to this module when
SARO_UNIVERSE=fmp (default polygon, unchanged behavior), and the
[universe-compare] shadow hook only runs when SARO_UNIVERSE_SHADOW=fmp.

SOURCING (normally 4 HTTP requests per build + one small DB read, 60s TTL):
  1-3. /stable/biggest-gainers | biggest-losers | most-actives (50 rows each)
       — the movers the funnel actually hunts. Payload (probed live
       2026-07-05): symbol, price, change, changesPercentage, exchange.
       NO volume field (verified live).
  4.   /stable/company-screener?volumeMoreThan=500k&marketCapMoreThan=10M&
       limit=10000 — ONE page returns the full sweep (~4k rows incl. ETFs).
       Fields: symbol, price, volume. `volume` is LIVE cumulative day volume
       (matches quote-short exactly; probed NVDA/CLRO) — the day.v join.
  prevDay {c, v}: read from the `fmp_eod_snapshot` table — a once-per-
       trading-day post-close (>=16:15 ET) capture of the SAME screener page
       (after the close its cumulative volume IS the completed session
       volume) + the movers lists. See
       app/engines/data_feeds/fmp_eod_snapshot.py. ZERO extra morning HTTP
       requests on this path, and NO non-FMP vendor anywhere: the grouped-
       daily join this module launched with is fully removed, so the
       universe survives that vendor's cancellation outright.
  BRIDGE (first mornings only, before any snapshot session exists):
       per-symbol /stable/historical-price-eod/full (trimmed with `from`,
       most recent COMPLETED row wins) for ONLY the candidates that matter —
       every movers-list symbol plus the top BRIDGE_SCREENER_TOP_N (~150)
       screener rows by live dollar volume — concurrency-limited
       (semaphore 5), 6s per-request timeouts, hard cap
       BRIDGE_MAX_REQUESTS (200) per build, request count logged. Symbols
       beyond the bridge cap keep the skip-without-baseline behavior.

PREV-CLOSE PREFERENCE ORDER (per symbol):
       snapshot -> bridge EOD -> movers-derived (price - change) with
       prevDay.v = 0. Screener-only symbols with neither snapshot nor bridge
       data are SKIPPED (no honest gap/rel_vol baseline).

WHY historical-price-eod WON THE BRIDGE SLOT over /stable/quote (full):
       probed live 2026-07-05 — quote-full DOES carry previousClose +
       volume (and still NO avgVolume), but its `volume` field is the
       CURRENT session's live cumulative volume; at morning scan time that
       is a partial session, not the prior session's completed volume, so
       it cannot supply the rel_vol denominator (prevDay.v).
       historical-price-eod returns the completed prior session's close AND
       volume together in one request (verified: its latest completed row
       matches quote-full's previousClose). quote-full remains useful only
       as a previousClose cross-check.

FIELD-MAPPING HONESTY / SEMANTIC SHIFTS (sanity-check funnel thresholds via
the [universe-compare] log line before any flip):
  * prevDay.c: snapshot/bridge give the real completed-session close; the
    movers-derived fallback (price - change) is self-consistent with FMP's
    own live numbers (cross-checked against /stable/quote previousClose).
  * rel_vol DENOMINATOR (prevDay.v): the planned avgVolume/averageVolume
    field does NOT exist anywhere on this plan (probed: stable screener and
    stable full quote both lack it; /api/v3 is dead). We use the real
    previous-session volume — the SAME denominator semantics the current
    funnel path already has, so rel_vol_min thresholds keep their meaning.
  * rel_vol NUMERATOR (day.v): LIVE cumulative day volume. During the
    morning scan this is a PARTIAL session, so rel_vol reads LOWER than the
    delayed-tier path that compares two COMPLETED sessions. The compare hook
    carries this note.
  * Movers below the screener sweep (volume < 500k or mcap < $10M) get
    day.v = 0 — unknown volume is reported as 0, never fabricated; the
    funnel's dollar_vol_min drops them.
  * Movers with NO prev-session entry anywhere (snapshot/bridge miss — recent
    IPOs, ticker-format mismatches) keep prevDay.v = 0 under the same
    never-fabricate rule. Consumers MUST treat prevDay.v=0 as "no rel-vol
    baseline", not as a tiny denominator: funnel._coarse already does
    (pvol=0 → relvol=0), and scan_for_momentum / theta_scanner's legacy gate
    now skip such rows instead of dividing by a fabricated 1.
  * Nothing is excluded here by design (leveraged ETFs etc.) — funnel._coarse
    already handles exclusions downstream.

RATE DISCIPLINE: 4 requests per steady-state build (3 movers + 1 screener;
+<=200 one-morning bridge requests only while no snapshot exists), 60s TTL
cache, ONE shared aiohttp session (reused from fmp_feed), hard per-request
timeouts, every sub-fetch independently graceful.
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
EOD_HIST_URL = "https://financialmodelingprep.com/stable/historical-price-eod/full"

# Screener sweep knobs. volumeMoreThan is tuned to the funnel's cheapest
# dollar-vol gate; marketCapMoreThan is a low bar that keeps CLRO-class
# movers ($11M mcap) while cutting shells. limit=10000 returns the whole
# sweep in ONE page at these filters (probed 2026-07-05: 3962 rows).
SCREENER_VOLUME_MIN = 500_000
SCREENER_MCAP_MIN = 10_000_000
SCREENER_LIMIT = 10_000

REQUEST_TIMEOUT_S = 12.0

# Bridge knobs (first-morning fallback while no EOD snapshot session exists).
BRIDGE_MAX_REQUESTS = 200      # hard cap on per-symbol EOD fetches per build
BRIDGE_CONCURRENCY = 5         # semaphore width
BRIDGE_TIMEOUT_S = 6.0         # per-request timeout
BRIDGE_SCREENER_TOP_N = 150    # top screener rows by live dollar volume

# The scanner treats a thinner-than-this universe as a failed build and falls
# back to its own grouped-daily path (same floor as that path's check).
FMP_MIN_UNIVERSE_ROWS = 200

# ── 60s in-process TTL cache (mirrors momentum_scanner._snapshot_cache) ─────
UNIVERSE_TTL_S = 60.0
_universe_cache: dict = {"fetched_at": 0.0, "rows": None}

# [universe-compare] shadow-hook throttle: at most one comparison per this
# many seconds, no matter how often the snapshot is rebuilt.
COMPARE_MIN_INTERVAL_S = 300.0
_compare_last_mono = 0.0

# Strong refs to in-flight compare tasks: asyncio.create_task keeps only a
# WEAK reference, and momentum_scanner deliberately discards the return value
# (fire-and-forget), so without this set a compare task could be GC'd
# mid-flight and its [universe-compare] evidence line silently lost. Tasks
# discard themselves on completion.
_compare_tasks: set = set()


def clear_universe_cache() -> None:
    """Drop the universe cache and compare throttle (tests / ops)."""
    global _compare_last_mono
    _universe_cache["fetched_at"] = 0.0
    _universe_cache["rows"] = None
    _compare_last_mono = 0.0


def _today_et_datestr() -> str:
    try:
        import zoneinfo
        return datetime.now(timezone.utc).astimezone(
            zoneinfo.ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── HTTP choke point (single seam; tests monkeypatch this) ───────────────────

# ── RVOL PACING (review finding 4) ──────────────────────────────────────────
# FMP day.v is LIVE cumulative (partial session); prevDay.v is a FULL prior
# session. Raw relvol therefore underreads ~4-6x at 09:40. Scale the
# denominator by the expected fraction of a session volume completed by scan
# time (standard intraday U-curve) so relvol reads vs-pace — the number
# traders actually mean by RVOL. FMP_RVOL_PACING=0 restores the raw
# prior-session denominator.
import os as _os_pace
_PACING_ON = _os_pace.environ.get("FMP_RVOL_PACING", "1") == "1"
_PACE_POINTS = [
    (4 * 60, 0.01), (8 * 60, 0.04), (9 * 60 + 30, 0.08), (9 * 60 + 40, 0.14),
    (10 * 60, 0.21), (11 * 60, 0.33), (12 * 60, 0.45), (14 * 60, 0.62),
    (15 * 60, 0.78), (16 * 60, 1.00), (20 * 60, 1.00),
]

def _pace_fraction(et_minutes: int) -> float:
    pts = _PACE_POINTS
    if et_minutes <= pts[0][0]:
        return pts[0][1]
    for (m0, f0), (m1, f1) in zip(pts, pts[1:]):
        if et_minutes <= m1:
            return f0 + (f1 - f0) * (et_minutes - m0) / (m1 - m0)
    return 1.0

def _now_et_minutes() -> int:
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    _n = _dt.now(_ZI("America/New_York"))
    return _n.hour * 60 + _n.minute

def _paced_prev_volume(prev_vol) -> int:
    try:
        v = int(prev_vol)
    except (TypeError, ValueError):
        return 0
    if v <= 0 or not _PACING_ON:
        return v
    return max(1, int(v * _pace_fraction(_now_et_minutes())))

async def _fmp_get_json(url: str, params: Optional[dict] = None,
                        timeout_s: float = REQUEST_TIMEOUT_S):
    """One FMP stable-API GET via the shared aiohttp session. Raises
    FMPHTTPError on non-200 so callers can decide fallback behavior."""
    import aiohttp

    key = _env_api_key()
    session = _get_session()
    async with session.get(
        url,
        params={**(params or {}), "apikey": key},
        timeout=aiohttp.ClientTimeout(total=timeout_s),
    ) as resp:
        if resp.status != 200:
            raise FMPHTTPError(resp.status)
        return await resp.json(content_type=None)


# ── prev-session sources: snapshot (standing) then bridge (first mornings) ──
async def _snapshot_prev_map() -> dict:
    """{symbol: {c, v}} from the latest completed-session EOD snapshot.
    {} on any failure or when no prior snapshot session exists."""
    try:
        from app.engines.data_feeds.fmp_eod_snapshot import load_prev_session_map
        return await load_prev_session_map(_today_et_datestr()) or {}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[fmp-universe] snapshot prev-map read failed "
                       f"({type(e).__name__}: {e})")
        return {}


def _bridge_candidates(mover_payloads: list, screener_payload) -> list:
    """Bridge symbol list: every movers-list symbol first (they are what the
    funnel hunts), then the top BRIDGE_SCREENER_TOP_N screener rows by live
    dollar volume, hard-capped at BRIDGE_MAX_REQUESTS symbols."""
    out: list = []
    seen: set = set()
    for payload in mover_payloads or []:
        if not isinstance(payload, list):
            continue
        for m in payload:
            try:
                if not isinstance(m, dict):
                    continue
                sym = str(m.get("symbol") or "").strip().upper()
                if sym and sym not in seen:
                    seen.add(sym)
                    out.append(sym)
            except Exception:
                continue
    ranked: list = []
    if isinstance(screener_payload, list):
        for s in screener_payload:
            try:
                if not isinstance(s, dict):
                    continue
                sym = str(s.get("symbol") or "").strip().upper()
                if not sym or sym in seen:
                    continue
                seen.add(sym)
                dv = float(s.get("price") or 0.0) * float(s.get("volume") or 0.0)
                ranked.append((dv, sym))
            except Exception:
                continue
    ranked.sort(key=lambda t: t[0], reverse=True)
    out.extend(sym for _, sym in ranked[:BRIDGE_SCREENER_TOP_N])
    return out[:BRIDGE_MAX_REQUESTS]


async def _bridge_fetch_prev_eod(sym: str, today_et: str) -> Optional[dict]:
    """One per-symbol EOD fetch → {c, v} for the most recent COMPLETED
    session (rows dated today are a live partial and are skipped). None on
    any failure — the symbol then falls to the next preference tier."""
    try:
        from_date = (datetime.strptime(today_et, "%Y-%m-%d")
                     - timedelta(days=10)).strftime("%Y-%m-%d")
        j = await _fmp_get_json(EOD_HIST_URL, {"symbol": sym, "from": from_date},
                                timeout_s=BRIDGE_TIMEOUT_S)
        if isinstance(j, dict):
            j = j.get("historical")  # tolerate the wrapped variant
        rows = [r0 for r0 in (j or []) if isinstance(r0, dict)]
        # FMP currently returns newest-first (probed live), but never trust
        # upstream ordering: a silent flip would hand back a ~10-day-old
        # close as prevClose. Sort by date desc explicitly before scanning.
        rows.sort(key=lambda r0: str(r0.get("date") or ""), reverse=True)
        for r0 in rows[:5]:
            try:
                if not isinstance(r0, dict):
                    continue
                d = str(r0.get("date") or "")[:10]
                if d and d >= today_et:
                    continue  # today's row = live partial, not a baseline
                c = float(r0.get("close") or 0.0)
                if c > 0:
                    return {"c": c, "v": float(r0.get("volume") or 0.0)}
            except Exception:
                continue
        return None
    except asyncio.CancelledError:
        raise
    except Exception:
        return None


async def _bridge_prev_map(mover_payloads: list, screener_payload) -> dict:
    """First-morning fallback: per-symbol EOD closes/volumes for the bridge
    candidates, semaphore-limited, capped at BRIDGE_MAX_REQUESTS per build."""
    today_et = _today_et_datestr()
    syms = _bridge_candidates(mover_payloads, screener_payload)
    if not syms:
        return {}
    sem = asyncio.Semaphore(BRIDGE_CONCURRENCY)
    out: dict = {}

    async def _one(sym: str) -> None:
        async with sem:
            r = await _bridge_fetch_prev_eod(sym, today_et)
            if r:
                out[sym] = r

    await asyncio.gather(*(_one(s) for s in syms))
    logger.info(f"[fmp-universe] bridge EOD backfill: {len(syms)} requests, "
                f"{len(out)} prev sessions resolved (cap {BRIDGE_MAX_REQUESTS})")
    return out


async def _prev_session_map(mover_payloads: list, screener_payload) -> tuple:
    """(prev_map, source): snapshot when a completed session exists, else the
    per-symbol EOD bridge, else ({}, 'none') — rows then degrade per the
    preference order (movers keep derived prevClose; screener-only skipped)."""
    snap = await _snapshot_prev_map()
    if snap:
        return snap, "snapshot"
    bridge = await _bridge_prev_map(mover_payloads, screener_payload)
    if bridge:
        return bridge, "bridge"
    return {}, "none"


# ── Row building (pure — unit-testable without any HTTP) ────────────────────
def _row(ticker: str, price: float, day_vol: float, prev_close: float,
         prev_vol: float, change_pct: float) -> dict:
    """One row in the exact grouped-daily shape _coarse/scan_for_momentum eat."""
    return {
        "ticker": ticker,
        "day": {"c": price, "v": int(day_vol)},
        "prevDay": {"c": prev_close, "v": _paced_prev_volume(prev_vol)},  # vs-pace RVOL
        "lastTrade": {"p": price},
        "todaysChangePerc": change_pct,
    }


def _build_universe_rows(mover_payloads: list, screener_payload, prev_map: dict) -> list:
    """Merge movers + screener sweep + prev-session map (snapshot or bridge)
    into snapshot rows. Dedupe by symbol (movers win — they carry the live
    change data). prevDay preference per symbol: prev_map (snapshot/bridge)
    -> movers-derived (price - change) with v=0. EXCLUDES nothing by design;
    the funnel handles exclusions downstream."""
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

    # Movers first: price/change/changesPercentage are live; prevDay {c,v}
    # prefers the real completed session (snapshot/bridge) and falls back to
    # prevClose = price - change with v=0; day.v joined from the screener.
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
                pm = (prev_map or {}).get(sym) or {}
                prev_map_close = float(pm.get("c") or 0.0)
                prev_close = prev_map_close if prev_map_close > 0 else (price - change)
                if price <= 0 or prev_close <= 0:
                    continue
                scr = screener_by_sym.get(sym)
                day_vol = float((scr or {}).get("volume") or 0.0)
                if day_vol <= 0:
                    mover_vol_misses += 1  # unknown volume stays 0 — never fabricated
                prev_vol = float(pm.get("v") or 0.0)
                change_pct = float(m.get("changesPercentage") or
                                   ((price - prev_close) / prev_close * 100.0))
                rows.append(_row(sym, price, day_vol, prev_close, prev_vol, change_pct))
                seen.add(sym)
            except Exception:
                continue

    # Screener sweep widens coverage. These rows have no FMP change field, so
    # BOTH prev-side values come from the completed-session map (snapshot or
    # bridge); symbols without an entry are skipped (no honest gap/rel_vol
    # baseline).
    for sym, s in screener_by_sym.items():
        try:
            if sym in seen:
                continue
            pm = (prev_map or {}).get(sym)
            if not pm:
                continue
            price = float(s.get("price") or 0.0)
            prev_close = float(pm.get("c") or 0.0)
            if price <= 0 or prev_close <= 0:
                continue
            day_vol = float(s.get("volume") or 0.0)
            prev_vol = float(pm.get("v") or 0.0)
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

    4 HTTP requests + one snapshot-table read on the steady-state path
    (<= 200 extra one-morning bridge requests while no snapshot exists),
    60s TTL cache, every sub-source independently graceful. Returns [] / a
    thin list on failure — the scanner wiring treats anything under
    FMP_MIN_UNIVERSE_ROWS as a failed build and falls back."""
    now = time.monotonic()
    if _universe_cache["rows"] is not None and (now - _universe_cache["fetched_at"]) < ttl_s:
        return _universe_cache["rows"]
    if not _env_api_key():
        logger.warning("[fmp-universe] FMP_API_KEY is empty — returning empty universe")
        return []

    gainers, losers, actives, screener = await asyncio.gather(
        _fetch_json_safe(GAINERS_URL, None, "biggest-gainers"),
        _fetch_json_safe(LOSERS_URL, None, "biggest-losers"),
        _fetch_json_safe(ACTIVES_URL, None, "most-actives"),
        _fetch_json_safe(SCREENER_URL, {
            "volumeMoreThan": SCREENER_VOLUME_MIN,
            "marketCapMoreThan": SCREENER_MCAP_MIN,
            "limit": SCREENER_LIMIT,
        }, "company-screener"),
    )
    prev_map, prev_src = await _prev_session_map([gainers, losers, actives], screener)

    rows = _build_universe_rows([gainers, losers, actives], screener, prev_map)
    n_movers = sum(len(p) for p in (gainers, losers, actives) if isinstance(p, list))
    logger.info(
        f"[fmp-universe] built {len(rows)} rows "
        f"(movers={n_movers}, screener={len(screener) if isinstance(screener, list) else 0}, "
        f"prev={prev_src}:{len(prev_map or {})})"
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
                 "rel_vol denominator=prev completed session via fmp eod "
                 "snapshot/bridge (no avgVolume on this plan); polygon day "
                 "side=last completed session"),
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
        task = asyncio.get_running_loop().create_task(
            _run_universe_compare(list(polygon_rows or [])))
        _compare_tasks.add(task)  # strong ref until done — see _compare_tasks
        task.add_done_callback(_compare_tasks.discard)
        # Consume the throttle only once a task actually spawned, so a failed
        # spawn (e.g. no running loop) doesn't burn the interval.
        _compare_last_mono = now
        return task
    except Exception as e:
        try:
            logger.warning(f"[universe-compare] hook spawn failed ({type(e).__name__}: {e})")
        except Exception:
            pass
        return None
