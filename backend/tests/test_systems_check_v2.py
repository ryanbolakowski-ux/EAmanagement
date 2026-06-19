"""SYSTEMS-CHECK-V2 tests — the admin System Check green/yellow/red matrix,
fixable vs non-fixable config issues, and stale-warning clearing. Pure logic
(no DB), so it's fast and deterministic. Also runnable as a plain script."""
from datetime import datetime, timedelta, timezone

from app.core import sc_logic as sc

NOW = datetime(2026, 6, 19, 16, 0, tzinfo=timezone.utc)


def _ago(minutes):
    return NOW - timedelta(minutes=minutes)


# ── open-position monitor (FIXABLE issue + stale-warning clearing) ──
def test_open_monitor_no_positions_green():
    assert sc.open_monitor_status(0, True, None, NOW) == "green"


def test_open_monitor_market_closed_is_green():
    # Stale, but the market is CLOSED — positions legitimately don't re-price.
    assert sc.open_monitor_status(3, False, _ago(6000), NOW) == "green"


def test_open_monitor_stale_while_open_is_yellow():
    # Market open + 4-day-stale -> a REAL, auto-fixable degraded state.
    assert sc.open_monitor_status(1, True, _ago(6000), NOW) == "yellow"


def test_stale_warning_clears_after_reprice():
    # Before Fix: stale -> yellow. After Fix re-prices (last_priced=now) -> green.
    assert sc.open_monitor_status(1, True, _ago(6000), NOW) == "yellow"
    assert sc.open_monitor_status(1, True, _ago(0), NOW) == "green"


# ── KYC webhooks (NON-fixable config issue; rare events are normal) ──
def test_kyc_configured_is_green_even_with_no_recent_events():
    assert sc.kyc_status(True) == "green"


def test_kyc_unconfigured_is_yellow():
    assert sc.kyc_status(False) == "yellow"


# ── Tradier (phantom TRADIER_API_KEY env must NOT drive status) ──
def test_tradier_not_in_use_is_green():
    assert sc.tradier_status(0) == "green"


def test_tradier_realmoney_live_without_creds_is_red():
    assert sc.tradier_status(1) == "red"


# ── Job queue (terminal/expired rows excluded by caller) ──
def test_queue_empty_is_green():
    assert sc.queue_status(0) == "green"


def test_queue_under_threshold_is_green():
    assert sc.queue_status(9) == "green"


def test_queue_real_backlog_is_yellow():
    assert sc.queue_status(15) == "yellow"


# ── Broker balance sync (lenient + market-aware freshness) ──
def test_broker_no_accounts_green():
    assert sc.broker_status(0, True, None, NOW) == "green"


def test_broker_market_closed_green():
    assert sc.broker_status(1, False, _ago(600), NOW) == "green"


def test_broker_recent_sync_green():
    assert sc.broker_status(1, True, _ago(20), NOW) == "green"


def test_broker_stalled_refresh_yellow():
    assert sc.broker_status(1, True, _ago(120), NOW) == "yellow"


# ── Overall status matrix (Green / Yellow / Red) ──
def test_overall_all_green():
    assert sc.overall_status([("green", False), ("green", True)]) == "green"


def test_overall_noncritical_degraded_is_yellow():
    assert sc.overall_status([("green", True), ("yellow", False)]) == "yellow"


def test_overall_noncritical_red_only_yellow():
    # A NON-critical component being red contributes only YELLOW to the overall.
    assert sc.overall_status([("red", False), ("green", True)]) == "yellow"


def test_overall_critical_red_is_red():
    assert sc.overall_status([("red", True), ("yellow", False)]) == "red"


def test_overall_optional_service_does_not_force_yellow():
    # Tradier not-in-use (green) + everything else green -> GREEN, not yellow.
    comps = [("green", False)] * 5  # kyc, tradier, queue, open-monitor, broker all green
    assert sc.overall_status(comps) == "green"


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
