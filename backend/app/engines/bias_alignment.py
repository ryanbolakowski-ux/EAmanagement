"""Daily-bias alignment — the owner's hard rule (2026-07-06):
bullish daily bias -> LONG entries only; bearish -> SHORT only;
neutral/unknown -> both allowed.

Enforced in entry_guard.can_enter (every paper/live/routed entry) and the
account_signals emit path (a contradicting signal never emails). Env
DAILY_BIAS_ALIGNMENT=0 disables (default ON). Bias cached 5 min; any
bias-engine failure FAILS OPEN (allow + warn) — a dead bias engine must not
halt the platform.

Context: 2026-07-05/06 the futures strategies shorted NQ off local structure
while the daily bias read bullish (rows recorded direction=short,
bias=bullish); 4/29 wins that day. Local structure may TIME entries; the
daily bias now gates DIRECTION.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from loguru import logger

_CACHE: dict = {}  # instrument -> (expires_epoch, bias or None)
_TTL = 300.0
_PARENT = {"MES": "ES", "MNQ": "NQ", "MYM": "YM", "M2K": "RTY"}


def _enabled() -> bool:
    return os.environ.get("DAILY_BIAS_ALIGNMENT", "1") == "1"


async def get_daily_bias(instrument: str) -> Optional[str]:
    """'bullish' | 'bearish' | 'neutral' | None — same engine as the dashboard."""
    inst = _PARENT.get((instrument or "").upper(), (instrument or "").upper())
    now = time.time()
    hit = _CACHE.get(inst)
    if hit and hit[0] > now:
        return hit[1]
    try:
        from app.api.routes.dashboard import _compute_daily_bias
        from app.database import async_session_factory
        async with async_session_factory() as db:
            b = await _compute_daily_bias(db, inst)
        bias = (b or {}).get("intraday_bias") or (b or {}).get("bias") or (b or {}).get("trend")
        bias = str(bias).lower() if bias else None
        if bias not in ("bullish", "bearish", "neutral"):
            bias = None
        _CACHE[inst] = (now + _TTL, bias)
        return bias
    except Exception as e:
        logger.warning(f"[bias-align] daily bias lookup failed for {inst}: {e} — failing OPEN")
        _CACHE[inst] = (now + 60.0, None)
        return None


async def direction_allowed(instrument: str, direction: str) -> tuple:
    if not _enabled():
        return True, "bias-alignment disabled"
    d = (direction or "").lower()
    if d not in ("long", "short"):
        return True, "unknown direction — not gated"
    bias = await get_daily_bias(instrument)
    if bias == "bullish" and d == "short":
        return False, f"daily bias BULLISH — shorts blocked on {instrument} (owner rule)"
    if bias == "bearish" and d == "long":
        return False, f"daily bias BEARISH — longs blocked on {instrument} (owner rule)"
    return True, f"aligned (bias={bias or 'unknown'})"
