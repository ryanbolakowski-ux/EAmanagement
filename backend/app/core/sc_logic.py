"""Pure decision helpers for the admin System Check (SYSTEMS-CHECK-V2).

Extracted from the big systems_check() endpoint so the green/yellow/red matrix
is unit-testable WITHOUT a database. systems_check() calls these; the tests in
backend/tests/test_systems_check_v2.py exercise them directly.

Status vocabulary: "green" (healthy) | "yellow" (degraded/non-critical) |
"red" (critical down). The overall rolls up criticality-aware.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional, Tuple


def _minutes_since(ts: Optional[datetime], now: datetime) -> Optional[float]:
    if ts is None:
        return None
    try:
        return (now - ts).total_seconds() / 60.0
    except Exception:
        return None


def staleness(ts: Optional[datetime], now: datetime, crit: bool = True) -> str:
    """green <5m, yellow <30m, then red (crit) / yellow (non-crit). Unknown ts -> yellow."""
    m = _minutes_since(ts, now)
    if m is None:
        return "yellow"
    if m < 5:
        return "green"
    if m < 30:
        return "yellow"
    return "red" if crit else "yellow"


def open_monitor_status(open_count: int, market_open: bool,
                        last_priced: Optional[datetime], now: datetime) -> str:
    """Open-position monitor. No open positions OR market closed -> green
    (positions legitimately don't re-price when the market is shut). Otherwise
    flag staleness (capped at yellow — never critical)."""
    if int(open_count or 0) <= 0:
        return "green"
    if not market_open:
        return "green"
    return staleness(last_priced, now, crit=False)


def kyc_status(configured: bool) -> str:
    """KYC webhook health = signing secret CONFIGURED. Rare events are normal,
    so 'no recent events' is never degraded."""
    return "green" if configured else "yellow"


def tradier_status(real_live_without_creds: int) -> str:
    """Tradier live execution uses PER-ACCOUNT credentials (the global
    TRADIER_API_KEY env is unused). RED only when a real-money live Tradier
    session is active but its account has no stored credentials; else green."""
    return "red" if int(real_live_without_creds or 0) > 0 else "green"


def queue_status(live_depth: int, threshold: int = 10) -> str:
    """Job queue. live_depth = genuinely-actionable pending trades (terminal /
    expired rows already excluded by the caller). Yellow only on a real backlog."""
    return "green" if int(live_depth or 0) < threshold else "yellow"


def broker_status(account_count: int, market_open: bool,
                  last_sync: Optional[datetime], now: datetime, window_sec: int = 3600) -> str:
    """Broker balance sync. No accounts OR market closed -> green (balances
    don't move). Green within `window_sec` of the last sync; yellow only if the
    refresh genuinely stalls during market hours."""
    if int(account_count or 0) <= 0 or not market_open:
        return "green"
    if last_sync is not None:
        try:
            if (now - last_sync).total_seconds() < window_sec:
                return "green"
        except Exception:
            pass
    return "yellow"


def overall_status(components: Iterable[Tuple[str, bool]]) -> str:
    """Roll up (status, is_critical) pairs. Red iff a CRITICAL component is red;
    yellow if any component is non-green; else green. Non-critical red -> yellow."""
    comps = list(components)
    has_critical_red = any(bool(crit) and status == "red" for status, crit in comps)
    has_issue = any(status in ("red", "yellow") for status, crit in comps)
    return "red" if has_critical_red else ("yellow" if has_issue else "green")
