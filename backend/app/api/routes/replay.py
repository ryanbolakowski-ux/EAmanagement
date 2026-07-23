"""Replay API — read-only historical session playback for the chart trainer.

GET /api/v1/replay/meta?instrument=NQ
    -> {instrument, first_date, last_date, tick, point_label}

GET /api/v1/replay/day?instrument=NQ&date=YYYY-MM-DD&include_overnight=1
    -> {date, instrument, bars: [{t,o,h,l,c,v}], levels: {...}, holiday: false}
    Bars run from 18:00 ET the prior calendar day (include_overnight=1) or
    09:30 ET (include_overnight=0) through 16:00 ET. Levels (PDH/PDL/PDC,
    Asia, London, NY open) are computed server-side with tz-correct ET
    windows via zoneinfo — never by slicing on UTC dates.

GET /api/v1/replay/random?instrument=NQ
    -> same payload as /day plus {"blind": true}. The client hides the date;
    a determined user can still find it in devtools (network tab shows the
    bar timestamps). Acceptable for v1 — this is a training aid, not an exam.

All endpoints are additive and SELECT-only against candle_cache (1m bars,
one row per instrument+timestamp). Payloads are bounded: a full overnight
session 18:00 -> 16:00 ET is at most 22h * 60 = 1,320 one-minute bars.
"""
from __future__ import annotations

import random
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.database import get_db
from app.models.user import User

router = APIRouter()

ET = ZoneInfo("America/New_York")

# instrument -> (tick size, human display label)
INSTRUMENTS: dict[str, tuple[float, str]] = {
    "ES": (0.25, "S&P 500 E-mini"),
    "NQ": (0.25, "Nasdaq-100 E-mini"),
    "YM": (1.0, "Dow Jones E-mini"),
    "RTY": (0.1, "Russell 2000 E-mini"),
}

# candle_cache coverage starts 2023-04-30 22:00 UTC (Sunday overnight), so
# the first full replayable RTH day is Monday 2023-05-01.
MIN_DATE = date_cls(2023, 5, 1)

# Minimum RTH bars for a day to count as a real session (holidays/half days
# and data gaps fall below this; a full RTH session is 390 bars).
MIN_RTH_BARS = 100
MIN_RANDOM_RTH_BARS = 300
RANDOM_MAX_ATTEMPTS = 15


# ── pure window/level helpers (unit-tested without a DB) ─────────────────

def _et(day: date_cls, hour: int, minute: int = 0) -> datetime:
    """ET wall-clock -> aware datetime. zoneinfo resolves the UTC offset per
    the actual DST rule for that date (the UTC-date bug family is banned)."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)


def session_bounds(day: date_cls, include_overnight: bool = True) -> tuple[datetime, datetime]:
    """[start, end) of the replay window in ET.

    Overnight sessions open at 18:00 ET the prior CALENDAR day (Sunday for a
    Monday session); RTH-only windows open at 09:30 ET. Both end 16:00 ET.
    """
    if include_overnight:
        start = _et(day - timedelta(days=1), 18, 0)
    else:
        start = _et(day, 9, 30)
    return start, _et(day, 16, 0)


def rth_bounds(day: date_cls) -> tuple[datetime, datetime]:
    """[09:30, 16:00) ET for `day`."""
    return _et(day, 9, 30), _et(day, 16, 0)


def slice_day_bars(bars: list[tuple], day: date_cls,
                   include_overnight: bool = True) -> list[tuple]:
    """Filter (ts, o, h, l, c, v) tuples to the replay window [start, end)."""
    start, end = session_bounds(day, include_overnight)
    return [b for b in bars if start <= b[0] < end]


def _window_high_low(bars: list[tuple], start: datetime, end: datetime):
    sel = [b for b in bars if start <= b[0] < end]
    if not sel:
        return None, None
    return max(b[2] for b in sel), min(b[3] for b in sel)


def compute_replay_levels(session_bars: list[tuple],
                          prior_rth_bars: list[tuple],
                          day: date_cls) -> dict:
    """Key levels for the replay chart. All windows are ET wall-clock:

      PDH/PDL/PDC  prior trading day's RTH 09:30-16:00 (caller supplies bars)
      Asia         18:00 prior calendar day -> 03:00 ET (overlaps London 02-03
                   by convention; both sessions claim that hour)
      London       02:00 -> 05:00 ET
      ny_open_ts   epoch seconds of the first bar at/after 09:30 ET
    """
    pdh = pdl = pdc = None
    if prior_rth_bars:
        pdh = max(b[2] for b in prior_rth_bars)
        pdl = min(b[3] for b in prior_rth_bars)
        pdc = prior_rth_bars[-1][4]

    asia_high, asia_low = _window_high_low(
        session_bars, _et(day - timedelta(days=1), 18, 0), _et(day, 3, 0))
    london_high, london_low = _window_high_low(
        session_bars, _et(day, 2, 0), _et(day, 5, 0))

    ny_open_ts = None
    rth_start = _et(day, 9, 30)
    for b in session_bars:
        if b[0] >= rth_start:
            ny_open_ts = int(b[0].timestamp())
            break

    return {
        "pdh": pdh, "pdl": pdl, "pdc": pdc,
        "asia_high": asia_high, "asia_low": asia_low,
        "london_high": london_high, "london_low": london_low,
        "ny_open_ts": ny_open_ts,
    }


def validate_instrument(instrument: str) -> str:
    inst = (instrument or "").strip().upper()
    if inst not in INSTRUMENTS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown instrument '{instrument}'. "
                   f"Valid: {', '.join(sorted(INSTRUMENTS))}")
    return inst


def validate_replay_date(date_str: str, last_date: date_cls) -> date_cls:
    try:
        day = date_cls.fromisoformat(date_str)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422,
                            detail="date must be YYYY-MM-DD")
    if day.weekday() >= 5:
        raise HTTPException(status_code=422,
                            detail=f"{day.isoformat()} is a weekend — futures "
                                   "RTH runs Monday-Friday.")
    if not (MIN_DATE <= day <= last_date):
        raise HTTPException(
            status_code=422,
            detail=f"date out of range: available "
                   f"{MIN_DATE.isoformat()}..{last_date.isoformat()}")
    return day


def _serialize_bars(bars: list[tuple]) -> list[dict]:
    return [{"t": int(b[0].timestamp()), "o": b[1], "h": b[2],
             "l": b[3], "c": b[4], "v": int(b[5] or 0)} for b in bars]


# ── DB access (thin, monkeypatchable in tests) ───────────────────────────

async def _fetch_bars(db: AsyncSession, instrument: str,
                      start: datetime, end: datetime) -> list[tuple]:
    """One query per window: 1m bars in [start, end), ordered by timestamp."""
    rows = (await db.execute(
        text("""
            SELECT timestamp, open, high, low, close, volume FROM candle_cache
            WHERE instrument = :inst AND timestamp >= :start AND timestamp < :end
            ORDER BY timestamp
        """),
        {"inst": instrument,
         "start": start.astimezone(timezone.utc),
         "end": end.astimezone(timezone.utc)},
    )).all()
    return [tuple(r) for r in rows]


async def _count_rth_bars(db: AsyncSession, instrument: str,
                          day: date_cls) -> int:
    start, end = rth_bounds(day)
    row = (await db.execute(
        text("""
            SELECT count(*) FROM candle_cache
            WHERE instrument = :inst AND timestamp >= :start AND timestamp < :end
        """),
        {"inst": instrument,
         "start": start.astimezone(timezone.utc),
         "end": end.astimezone(timezone.utc)},
    )).scalar()
    return int(row or 0)


async def _data_range(db: AsyncSession, instrument: str) -> tuple[datetime, datetime]:
    row = (await db.execute(
        text("SELECT min(timestamp), max(timestamp) FROM candle_cache "
             "WHERE instrument = :inst"),
        {"inst": instrument},
    )).one()
    if row[0] is None:
        raise HTTPException(status_code=404,
                            detail=f"No candle data for {instrument}")
    return row[0], row[1]


async def _fetch_prior_rth(db: AsyncSession, instrument: str,
                           day: date_cls) -> list[tuple]:
    """Prior TRADING day's RTH bars: step back over weekends/holidays (max 5
    calendar-weekday hops so a bad data gap can't loop forever)."""
    prev = day
    for _ in range(5):
        prev = prev - timedelta(days=1)
        while prev.weekday() >= 5:
            prev = prev - timedelta(days=1)
        bars = await _fetch_bars(db, instrument, *rth_bounds(prev))
        if len(bars) >= MIN_RTH_BARS:
            return bars
    return []


async def _build_day_payload(db: AsyncSession, instrument: str,
                             day: date_cls, include_overnight: bool) -> dict:
    """Shared by /day and /random. Raises 404 if the day has no real RTH."""
    session_start, session_end = session_bounds(day, include_overnight=True)
    session_bars = await _fetch_bars(db, instrument, session_start, session_end)

    rth_start, _ = rth_bounds(day)
    rth_count = sum(1 for b in session_bars if b[0] >= rth_start)
    if rth_count < MIN_RTH_BARS:
        raise HTTPException(
            status_code=404,
            detail=f"No RTH session for {instrument} on {day.isoformat()} "
                   f"({rth_count} bars) — likely a market holiday or a data "
                   "gap. Pick another date.")

    prior_rth = await _fetch_prior_rth(db, instrument, day)
    levels = compute_replay_levels(session_bars, prior_rth, day)
    out_bars = (session_bars if include_overnight
                else slice_day_bars(session_bars, day, include_overnight=False))
    return {
        "date": day.isoformat(),
        "instrument": instrument,
        "bars": _serialize_bars(out_bars),
        "levels": levels,
        "holiday": False,
    }


# ── routes ───────────────────────────────────────────────────────────────

@router.get("/meta")
async def replay_meta(
    instrument: str = Query("NQ"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    inst = validate_instrument(instrument)
    tick, label = INSTRUMENTS[inst]
    first_ts, last_ts = await _data_range(db, inst)
    return {
        "instrument": inst,
        "first_date": max(MIN_DATE, first_ts.astimezone(ET).date()).isoformat(),
        "last_date": last_ts.astimezone(ET).date().isoformat(),
        "tick": tick,
        "point_label": label,
    }


@router.get("/day")
async def replay_day(
    instrument: str = Query("NQ"),
    date: str = Query(..., description="YYYY-MM-DD (ET trading day)"),
    include_overnight: int = Query(1),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    inst = validate_instrument(instrument)
    _, last_ts = await _data_range(db, inst)
    day = validate_replay_date(date, last_ts.astimezone(ET).date())
    return await _build_day_payload(db, inst, day,
                                    include_overnight=bool(include_overnight))


@router.get("/random")
async def replay_random(
    instrument: str = Query("NQ"),
    include_overnight: int = Query(1),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    inst = validate_instrument(instrument)
    _, last_ts = await _data_range(db, inst)
    last_date = last_ts.astimezone(ET).date()
    span = (last_date - MIN_DATE).days
    if span < 1:
        raise HTTPException(status_code=404, detail="Not enough history yet.")

    for _ in range(RANDOM_MAX_ATTEMPTS):
        day = MIN_DATE + timedelta(days=random.randint(0, span - 1))
        if day.weekday() >= 5:
            continue
        # Cheap COUNT probe first so a holiday costs one query, not a full fetch.
        if await _count_rth_bars(db, inst, day) < MIN_RANDOM_RTH_BARS:
            continue
        payload = await _build_day_payload(
            db, inst, day, include_overnight=bool(include_overnight))
        payload["blind"] = True
        return payload

    raise HTTPException(
        status_code=503,
        detail="Could not find a full trading day after "
               f"{RANDOM_MAX_ATTEMPTS} attempts — try again.")
