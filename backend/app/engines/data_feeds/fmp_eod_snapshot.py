"""FMP EOD volume snapshot (TRACK fmp-self-sufficiency).

The FMP plan has NO avgVolume/averageVolume anywhere (probed: stable screener
and stable full quote both lack it; /api/v3 is dead), so the fmp_universe
prevDay{c,v} join used to lean on ONE Polygon grouped-daily call. This module
removes that dependency: once per trading day, shortly AFTER the close
(>=16:15 ET, Redis SETNX latch, shadow.py pattern), we re-fetch the SAME
company-screener page the universe build uses — after the close its live
cumulative `volume` IS the completed session volume — plus the 3 movers lists
for below-sweep prices, and persist {symbol -> close, volume} for that ET
session date into the small ad-hoc table `fmp_eod_snapshot` (CREATE TABLE IF
NOT EXISTS, sec_edgar.py pattern), keeping only the newest
SNAPSHOT_KEEP_SESSIONS session dates.

Next morning, fmp_universe reads prevDay{c,v} straight from the latest
snapshot dated BEFORE today — ZERO Polygon, ZERO extra morning HTTP requests.

Cost: 4 FMP requests per trading day (3 movers + 1 screener), all fully
try/excepted. Holiday note: if the latch fires on a non-weekend market
holiday, the screener still shows the last completed session's close/volume,
so the persisted VALUES stay honest even if the date label is the holiday.

Env: EOD_SNAPSHOT_ENABLED (default "1"). Latch: fmp:eod_snapshot:{YYYY-MM-DD}.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import text

# Keep the newest N session dates in the table (a week of trading, roughly).
SNAPSHOT_KEEP_SESSIONS = 5

# Fire only after the session is complete — 16:15 ET gives the screener's
# cumulative volume time to settle post-close.
EOD_SNAPSHOT_AFTER_ET = (16, 15)

REDIS_LATCH_PREFIX = "fmp:eod_snapshot:"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS fmp_eod_snapshot (
    session_date  VARCHAR(10) NOT NULL,
    symbol        VARCHAR(20) NOT NULL,
    close         DOUBLE PRECISION NOT NULL,
    volume        BIGINT NOT NULL DEFAULT 0,
    captured_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_date, symbol)
)
"""


# ── small seams (tests monkeypatch these) ────────────────────────────────────
def _now_et() -> Optional[datetime]:
    try:
        import zoneinfo
        return datetime.now(timezone.utc).astimezone(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        return None


def _session_factory():
    from app.database import async_session_factory
    return async_session_factory


def _get_redis():
    import redis as _redis
    return _redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"),
                                 decode_responses=True)


async def _ensure_table(db) -> None:
    await db.execute(text(_CREATE_TABLE_SQL))


# ── storage ──────────────────────────────────────────────────────────────────
async def save_snapshot(session_date: str, quotes: dict) -> int:
    """Persist {symbol -> (close, volume)} for one completed session, then
    prune the table to the newest SNAPSHOT_KEEP_SESSIONS session dates.
    Returns the number of rows written (0 when there is nothing valid)."""
    rows = []
    for sym, cv in (quotes or {}).items():
        try:
            s = str(sym or "").strip().upper()
            close = float(cv[0])
            vol = int(float(cv[1] or 0))
            if s and close > 0:
                rows.append({"d": session_date, "s": s, "c": close, "v": max(vol, 0)})
        except Exception:
            continue
    if not rows:
        return 0
    async with _session_factory()() as db:
        await _ensure_table(db)
        # idempotent per session date: re-runs replace, never duplicate
        await db.execute(text("DELETE FROM fmp_eod_snapshot WHERE session_date = :d"),
                         {"d": session_date})
        await db.execute(
            text("INSERT INTO fmp_eod_snapshot (session_date, symbol, close, volume) "
                 "VALUES (:d, :s, :c, :v)"),
            rows,
        )
        res = await db.execute(text(
            "SELECT DISTINCT session_date FROM fmp_eod_snapshot "
            "ORDER BY session_date DESC"))
        dates = [r[0] for r in res.fetchall() if r and r[0]]
        if len(dates) > SNAPSHOT_KEEP_SESSIONS:
            cutoff = dates[SNAPSHOT_KEEP_SESSIONS - 1]
            await db.execute(text(
                "DELETE FROM fmp_eod_snapshot WHERE session_date < :cut"),
                {"cut": cutoff})
        await db.commit()
    return len(rows)


async def load_prev_session_map(today_et: str) -> dict:
    """{symbol: {"c": close, "v": volume}} for the latest snapshot session
    dated strictly BEFORE today_et — the same 'most recent completed session'
    semantics the old Polygon grouped-daily walk had. Returns {} on any
    failure or when no prior snapshot exists (the caller then bridges)."""
    try:
        async with _session_factory()() as db:
            await _ensure_table(db)
            await db.commit()
            res = await db.execute(text(
                "SELECT MAX(session_date) FROM fmp_eod_snapshot "
                "WHERE session_date < :today"), {"today": today_et})
            row = res.fetchone()
            prev_date = row[0] if row else None
            if not prev_date:
                return {}
            res = await db.execute(text(
                "SELECT symbol, close, volume FROM fmp_eod_snapshot "
                "WHERE session_date = :d"), {"d": prev_date})
            out: dict = {}
            for r in res.fetchall():
                try:
                    sym = str(r[0] or "").strip().upper()
                    close = float(r[1] or 0.0)
                    if sym and close > 0:
                        out[sym] = {"c": close, "v": float(r[2] or 0.0)}
                except Exception:
                    continue
            return out
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[fmp-eod-snapshot] prev-session read failed "
                       f"({type(e).__name__}: {e})")
        return {}


# ── the EOD capture ──────────────────────────────────────────────────────────
async def capture_eod_snapshot(session_date: Optional[str] = None) -> int:
    """One post-close capture: the SAME screener page fmp_universe uses (its
    live cumulative volume equals the completed session volume after the
    close) + the 3 movers lists so below-sweep movers keep an honest close
    (volume stored as 0 — unknown volume is never fabricated). 4 FMP requests
    total. Returns rows persisted; 0 on any failure."""
    from app.engines.data_feeds import fmp_universe as fu

    if not fu._env_api_key():
        logger.warning("[fmp-eod-snapshot] FMP_API_KEY is empty — skipping capture")
        return 0
    if session_date is None:
        et = _now_et()
        if et is None:
            return 0
        session_date = et.strftime("%Y-%m-%d")

    gainers, losers, actives, screener = await asyncio.gather(
        fu._fetch_json_safe(fu.GAINERS_URL, None, "biggest-gainers"),
        fu._fetch_json_safe(fu.LOSERS_URL, None, "biggest-losers"),
        fu._fetch_json_safe(fu.ACTIVES_URL, None, "most-actives"),
        fu._fetch_json_safe(fu.SCREENER_URL, {
            "volumeMoreThan": fu.SCREENER_VOLUME_MIN,
            "marketCapMoreThan": fu.SCREENER_MCAP_MIN,
            "limit": fu.SCREENER_LIMIT,
        }, "company-screener"),
    )

    quotes: dict = {}
    if isinstance(screener, list):
        for s in screener:
            try:
                if not isinstance(s, dict):
                    continue
                sym = str(s.get("symbol") or "").strip().upper()
                price = float(s.get("price") or 0.0)
                if sym and price > 0 and sym not in quotes:
                    quotes[sym] = (price, float(s.get("volume") or 0.0))
            except Exception:
                continue
    for payload in (gainers, losers, actives):
        if not isinstance(payload, list):
            continue
        for m in payload:
            try:
                if not isinstance(m, dict):
                    continue
                sym = str(m.get("symbol") or "").strip().upper()
                price = float(m.get("price") or 0.0)
                if sym and price > 0 and sym not in quotes:
                    quotes[sym] = (price, 0.0)  # below the sweep: close only
            except Exception:
                continue

    if not quotes:
        logger.warning("[fmp-eod-snapshot] nothing to persist (all fetches failed?)")
        return 0
    n = await save_snapshot(session_date, quotes)
    logger.info(f"[fmp-eod-snapshot] persisted {n} symbols for {session_date}")
    return n


# ── scheduler hook — once per ET trading day, fully isolated ────────────────
async def _check_and_run_eod_snapshot() -> None:
    """Called each premarket_scheduler loop iteration; runs the capture
    exactly once per trading day (Redis SETNX latch, shadow.py pattern) once
    past 16:15 ET. Gated by EOD_SNAPSHOT_ENABLED (default on). Any failure is
    swallowed — it can never affect the scan loop."""
    try:
        if (os.environ.get("EOD_SNAPSHOT_ENABLED", "1") or "1").strip() != "1":
            return
        et = _now_et()
        if et is None or et.weekday() >= 5:
            return
        gate_h, gate_m = EOD_SNAPSHOT_AFTER_ET
        if (et.hour * 60 + et.minute) < (gate_h * 60 + gate_m):
            return
        today_key = et.strftime("%Y-%m-%d")
        try:
            r = _get_redis()
            if not r.set(f"{REDIS_LATCH_PREFIX}{today_key}", "running",
                         ex=36 * 3600, nx=True):
                return  # already captured today (or another worker owns it)
        except Exception:
            return  # no redis latch → skip rather than risk duplicate runs
        logger.info(f"[fmp-eod-snapshot] daily capture start "
                    f"{et.strftime('%H:%M ET')} ({today_key})")
        persisted = 0
        try:
            persisted = await capture_eod_snapshot(today_key)
        finally:
            if not persisted:
                # Self-healing: a transient FMP failure right after 16:15
                # must not burn the whole day's snapshot (next morning would
                # pay the <=200-request bridge). Release the latch so a later
                # loop iteration retries the capture the same evening.
                try:
                    r.delete(f"{REDIS_LATCH_PREFIX}{today_key}")
                except Exception:
                    pass
    except asyncio.CancelledError:
        raise
    except Exception as e:
        try:
            logger.warning(f"[fmp-eod-snapshot] check failed ({type(e).__name__}: {e})")
        except Exception:
            pass
