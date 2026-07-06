"""NY-lunch no-trade window — the owner's rule (2026-07-06):
"usually no trade during NY lunch" -> block NEW futures entries between
11:00:00 and 13:59:59 ET. Existing positions are untouched; this only gates
entry paths (entry_guard.can_enter and the account_signals emit path).

Only futures roots are gated (stocks/ETFs pass through). Env
LUNCH_WINDOW_BLOCK=0 disables (default ON). Env LUNCH_WINDOW_EXEMPT is a
comma-separated list of strategy names allowed to trade through lunch —
default empty: NY PM Reversal's doctrinal 14:00-15:00 window starts AT the
gate's end so it needs no exemption, and strategies with NO declared session
window are exactly the ones this gate exists for. Any failure FAILS OPEN
(allow + warn) — a broken clock lookup must not halt the platform.
"""
from __future__ import annotations

import os
import time as _time
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

from loguru import logger

_FUTURES_ROOTS = {"ES", "NQ", "YM", "RTY", "MES", "MNQ", "MYM", "M2K"}
_ET = ZoneInfo("America/New_York")
_WINDOW_START = time(11, 0, 0)
_WINDOW_END = time(14, 0, 0)  # exclusive: 13:59:59 blocked, 14:00:00 allowed

_NAME_CACHE: dict = {}  # strategy_id -> (expires_epoch, name or None)
_TTL = 300.0


def _enabled() -> bool:
    return os.environ.get("LUNCH_WINDOW_BLOCK", "1") == "1"


def _exempt_set() -> set:
    """Lower-cased strategy names exempt from the lunch gate (env
    LUNCH_WINDOW_EXEMPT, comma-separated). Default: empty."""
    raw = os.environ.get("LUNCH_WINDOW_EXEMPT", "") or ""
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def lunch_blocked(instrument, strategy_name: Optional[str] = None,
                  now_et: Optional[datetime] = None) -> tuple:
    """(blocked: bool, reason: str). Pure/sync; never raises.

    Gates only instruments whose upper() is a known futures root; the window
    is 11:00 <= t < 14:00 America/New_York (DST-correct via zoneinfo).
    `now_et` (tests): tz-aware datetimes are converted to ET, naive ones are
    taken as already-ET wall time.
    """
    try:
        if not _enabled():
            return False, "lunch-window disabled (LUNCH_WINDOW_BLOCK=0)"
        inst = (instrument or "").upper()
        if inst not in _FUTURES_ROOTS:
            return False, f"{instrument!r} not a gated futures root"
        if strategy_name and strategy_name.strip().lower() in _exempt_set():
            return False, f"strategy '{strategy_name}' exempt from lunch window"
        if now_et is None:
            now_et = datetime.now(_ET)
        elif getattr(now_et, "tzinfo", None) is not None:
            now_et = now_et.astimezone(_ET)
        t = now_et.time()
        if _WINDOW_START <= t < _WINDOW_END:
            return True, ("NY lunch 11:00-14:00 ET — new futures entries "
                          "blocked (owner rule)")
        return False, f"outside NY lunch window (now={t.strftime('%H:%M:%S')} ET)"
    except Exception as e:  # pragma: no cover — belt-and-braces fail-open
        logger.warning(f"[lunch-window] lunch_blocked errored ({e}) — failing OPEN")
        return False, f"lunch-window errored ({e}) — failing open"


async def strategy_name_for(strategy_id) -> Optional[str]:
    """Strategy name for an id, 5-min cached. Only needed when
    _exempt_set() is non-empty (callers should skip the lookup otherwise).
    Fails OPEN (None) on any error — an unresolvable name simply means the
    strategy cannot claim an exemption."""
    if strategy_id is None:
        return None
    sid = str(strategy_id)
    now = _time.time()
    hit = _NAME_CACHE.get(sid)
    if hit and hit[0] > now:
        return hit[1]
    try:
        from sqlalchemy import text
        from app.database import async_session_factory
        async with async_session_factory() as db:
            row = (await db.execute(
                text("SELECT name FROM strategies WHERE id = :sid"),
                {"sid": strategy_id})).fetchone()
        name = row[0] if row else None
        _NAME_CACHE[sid] = (now + _TTL, name)
        return name
    except Exception as e:
        logger.warning(f"[lunch-window] strategy name lookup failed "
                       f"sid={strategy_id}: {e} — failing OPEN")
        _NAME_CACHE[sid] = (now + 60.0, None)
        return None
