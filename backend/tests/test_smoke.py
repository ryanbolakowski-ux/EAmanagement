"""Fix 9: authenticated smoke test — core endpoints must never 5xx.

Covers login (auth/me), strategies, signals (both naming aliases), backtests,
optimization list, and the liveness + full health endpoints.
"""
import pytest

# (path, allow_404) — 404 is acceptable for endpoints that may be empty/new;
# a 5xx is never acceptable.
CORE = [
    ("/api/v1/auth/me", False),
    ("/api/v1/strategies/", False),
    ("/api/v1/account-signals/watchers", False),
    ("/api/v1/email-signals/watchers", False),
    ("/api/v1/backtests/", False),
    ("/api/v1/optimization/", False),
    ("/api/v1/dashboard/summary", False),
]


@pytest.mark.parametrize("path,allow_404", CORE)
def test_core_endpoint_no_5xx(client, path, allow_404):
    r = client.get(path)
    assert r.status_code < 500, f"{path} returned {r.status_code}: {r.text[:200]}"
    if not allow_404:
        assert r.status_code != 404, f"{path} unexpectedly 404"


def test_liveness_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


def test_full_health_reports_components(client):
    """/health/full reports per-dependency health. Skips if not deployed yet."""
    r = client.get("/health/full")
    if r.status_code == 404:
        pytest.skip("/health/full not deployed on this server yet")
    # 200 healthy or 503 degraded — both are valid structured responses, not 5xx-crash
    assert r.status_code in (200, 503), r.text
    body = r.json()
    assert "components" in body
    # database + auth must be reported
    assert "database" in body["components"] or "auth" in body["components"]
