"""Pure helpers for per-session paper-engine capital allocation (ALLOC-V1).

Owner request 2026-07-06: "need a way to change how much allocation it has on
paper trading engine" + "kill the multiplier, it should just size up the
position size". The engine sizes contracts off equity (see PaperTrader), so
allocation is simply the session's starting balance — no output multiplier.

Kept dependency-free so the clamp/resolve logic is unit-testable without the
FastAPI app or a database.
"""

import math

MIN_STARTING_BALANCE = 1_000.0
MAX_STARTING_BALANCE = 1_000_000.0
DEFAULT_STARTING_BALANCE = 10_000.0  # PaperTrader.__init__ default


def clamp_starting_balance(value) -> float:
    """Clamp a requested paper-session starting balance to [$1k, $1M].

    Raises ValueError/TypeError on non-numeric or non-finite input (the
    API layer converts that to a 400).
    """
    v = float(value)
    if not math.isfinite(v):
        raise ValueError("starting_balance must be a finite number")
    return min(max(v, MIN_STARTING_BALANCE), MAX_STARTING_BALANCE)


def resolve_starting_balance(raw) -> float:
    """Fail-safe mapping of a trade_sessions.starting_balance row value to the
    balance handed to PaperTrader. NULL / unparseable / non-finite / <= 0 all
    fall back to the engine default — a bad DB value must never stop a session
    from starting."""
    if raw is None:
        return DEFAULT_STARTING_BALANCE
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_STARTING_BALANCE
    if not math.isfinite(v) or v <= 0:
        return DEFAULT_STARTING_BALANCE
    return v
