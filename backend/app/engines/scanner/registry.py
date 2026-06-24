"""Scanner registry (SCANNER-V1).

Maps Strategy.signal_mode -> an async handler that returns a list of hits, so the
premarket scheduler dispatches through one table instead of a long if/elif chain.

Phase 0: every legacy mode is registered as a thin delegator that makes the
EXACT same scan_* call the old dispatcher made (behavior is unchanged). As new
template-driven modes are implemented (P2) and approved, they're registered here
too. Imports of premarket_scheduler internals are done lazily inside handlers to
avoid a circular import (premarket_scheduler imports this module at dispatch).

Handler signature: ``async (strategy, user, *, is_premarket: bool) -> list``.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Optional

Handler = Callable[..., Awaitable[list]]


async def _h_low_float_squeeze(strategy, user, *, is_premarket: bool) -> list:
    from app.engines.options.stt_scanners import scan_low_float_squeeze
    return await scan_low_float_squeeze(top_k=1)


async def _h_fifty_two_week_breakout(strategy, user, *, is_premarket: bool) -> list:
    from app.engines.options.stt_scanners import scan_52w_breakout
    return await scan_52w_breakout(top_k=1)


async def _h_premarket_gap_runner(strategy, user, *, is_premarket: bool) -> list:
    from app.engines.options.stt_scanners import scan_premarket_gappers
    return await scan_premarket_gappers(top_k=1)


async def _h_oracle_opening_candle(strategy, user, *, is_premarket: bool) -> list:
    from app.engines.options.stt_scanners import scan_oracle_opening_candle
    return await scan_oracle_opening_candle(top_k=1)


async def _h_momentum_scanner(strategy, user, *, is_premarket: bool) -> list:
    # Identical params to the legacy dispatcher: long-only $2-$10, +5..15%,
    # >=750k vol, >=4x vol-ratio, top_k=5 (4 runners-up in the email).
    from app.engines.options.momentum_scanner import scan_for_momentum
    return await scan_for_momentum(
        min_change_pct=5.0, max_change_pct=15.0,
        min_price=2.0, max_price=10.0,
        min_day_volume=750_000, min_vol_ratio=4.0,
        top_k=5, include_negative=False,
    )


async def _h_universe_scan(strategy, user, *, is_premarket: bool) -> list:
    # Legacy ICT-on-watchlist (also the default for unknown modes), byte-identical
    # to the old `else:` branch — returns [] when there's no universe.
    from app.engines.options.universe import get_universe
    from app.engines.options.universe_scanner import scan_universe
    from app.engines.options.premarket_scheduler import _build_config
    universe_list = getattr(strategy, "watch_universe", None) or get_universe("expanded")
    if not universe_list:
        return []
    cfg = await _build_config(strategy, universe_list[0])
    return await scan_universe(cfg, universe_list, top_k=1)


# signal_mode -> handler. Unknown / "universe_scan" -> the universe handler
# (matches the old dispatcher's `else:` default).
_REGISTRY: dict[str, Handler] = {
    "low_float_squeeze": _h_low_float_squeeze,
    "fifty_two_week_breakout": _h_fifty_two_week_breakout,
    "premarket_gap_runner": _h_premarket_gap_runner,
    "oracle_opening_candle": _h_oracle_opening_candle,
    "momentum_scanner": _h_momentum_scanner,
    "universe_scan": _h_universe_scan,
}


def get_scanner(mode: Optional[str]) -> Handler:
    """Return the handler for a signal_mode. Unknown modes fall back to the
    universe (ICT) scanner, exactly like the legacy dispatcher's else-branch."""
    return _REGISTRY.get((mode or "").lower(), _h_universe_scan)


def registered_modes() -> list:
    return sorted(_REGISTRY.keys())
