"""GET /api/v1/admin/systems-check:
  * non-admin gets 403
  * admin gets 200 + every top-level key (overall, scanners, emails,
    trading, integrations, infra, recent_errors, jobs_running, metrics)

Run standalone (against the in-container backend on localhost:8000):
    pytest backend/tests/test_systems_check_admin_only.py -v -p no:cacheprovider
"""
import os
import psycopg2
from app.core.security import create_access_token


def _make_token(is_admin: bool) -> str:
    """Provision (or update) a one-off test user, set is_admin accordingly,
    and mint a JWT. Uses sync psycopg2 to avoid event-loop coupling."""
    email = "pytest-systems-check@thetaalgos.test"
    username = "pytest_systems_check"
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    cn = psycopg2.connect(url, connect_timeout=5)
    try:
        with cn, cn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email=%s", (email,))
            row = cur.fetchone()
            if row is None:
                import uuid as _u
                uid = str(_u.uuid4())
                cur.execute(
                    "INSERT INTO users (id, email, username, hashed_password, is_active, is_admin, subscription_tier) "
                    "VALUES (%s,%s,%s,%s,TRUE,%s,'tier_5')",
                    (uid, email, username, "!login-disabled-test!", is_admin),
                )
            else:
                uid = str(row[0])
                cur.execute("UPDATE users SET is_admin=%s WHERE id=%s", (is_admin, uid))
    finally:
        cn.close()
    return create_access_token({"sub": uid})


def test_non_admin_gets_403(client):
    """Default test fixture user (auth.token in conftest) is NOT an admin."""
    r = client.get("/api/v1/admin/systems-check")
    assert r.status_code == 403, f"non-admin must be denied; got {r.status_code} {r.text}"


def test_admin_gets_full_payload():
    """An is_admin user gets 200 + the full top-level shape."""
    token = _make_token(is_admin=True)
    import httpx
    with httpx.Client(base_url=os.environ.get("TEST_BASE_URL", "http://localhost:8000"),
                       headers={"Authorization": f"Bearer {token}"}, timeout=30.0) as c:
        r = c.get("/api/v1/admin/systems-check")
        assert r.status_code == 200, f"admin must get 200; got {r.status_code} {r.text}"
        data = r.json()
        for key in ("overall", "scanners", "emails", "trading", "integrations",
                    "infra", "recent_errors", "jobs_running", "metrics"):
            assert key in data, f"response missing required top-level key {key!r}"
        # overall must have status + summary
        assert data["overall"]["status"] in ("green", "yellow", "red", "unknown")
        assert isinstance(data["overall"].get("summary"), str)
        # scanners.theta_scanner contract
        assert "theta_scanner" in data["scanners"]
        assert "status" in data["scanners"]["theta_scanner"]
        # infra contracts
        assert "database" in data["infra"]
        assert "redis" in data["infra"]
        # recent_errors and jobs_running are lists
        assert isinstance(data["recent_errors"], list)
        assert isinstance(data["jobs_running"], list)
    # leave is_admin=true on the test user — non_admin test uses the conftest
    # fixture user (different account), so this is independent.
