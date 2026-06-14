"""System Check status-logic tests: healthy / warning(non-critical) / critical.
(auto-fix success/failure + admin-only 403 were verified live against the running
app: refresh_health/rerun/clear/resync all returned ok=True; non-admin POST -> 403;
pipeline_alerts._fetch_admin_emails queries WHERE is_admin=true so normal users
never receive system-error emails.)"""
from app.engines.scanner_health import apply_criticality

def _c(ok): return {"ok": ok}

def test_all_healthy():
    r = apply_criticality({"components": {"redis": _c(True), "resend": _c(True), "database": _c(True), "yfinance": _c(True)}})
    assert r["ok"] is True and r["degraded"] is False
    assert r["broken_critical"] == [] and r["broken_degraded"] == []

def test_warning_noncritical_down_is_not_critical():
    # yfinance (fallback feeds exist) down -> degraded yellow, NOT a critical red
    r = apply_criticality({"components": {"redis": _c(True), "resend": _c(True), "database": _c(True), "yfinance": _c(False)}})
    assert r["ok"] is True            # no CRITICAL component down -> system not "down"
    assert r["degraded"] is True
    assert r["broken_critical"] == []
    assert "yfinance" in r["broken_degraded"]

def test_critical_down_flips_ok():
    r = apply_criticality({"components": {"redis": _c(False), "resend": _c(True), "database": _c(True)}})
    assert r["ok"] is False
    assert "redis" in r["broken_critical"]

def test_no_pick_is_not_critical():
    # a no-pick day (theta_scanner_today not ok) must NOT make the system red
    r = apply_criticality({"components": {"redis": _c(True), "resend": _c(True), "database": _c(True), "theta_scanner_today": _c(False)}})
    assert r["ok"] is True
    assert r["components"]["theta_scanner_today"]["critical"] is False
    assert "theta_scanner_today" in r["broken_degraded"]

def test_critical_flags_tagged_correctly():
    r = apply_criticality({"components": {"redis": _c(True), "resend": _c(True), "database": _c(True), "yfinance": _c(True), "polygon": _c(True)}})
    assert r["components"]["redis"]["critical"] is True
    assert r["components"]["resend"]["critical"] is True
    assert r["components"]["database"]["critical"] is True
    assert r["components"]["yfinance"]["critical"] is False
    assert r["components"]["polygon"]["critical"] is False
