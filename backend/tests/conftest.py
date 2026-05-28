"""Shared fixtures for the integration tests.

These run against the LIVE backend on localhost:8000 (the same container).
We provision a single dedicated test user directly in the DB (never via the
public signup flow), mint a JWT for it, and clean up any rows we create.
The test user is TIER_5 so it passes both the paid-user gate (strategies) and
the live-tier gate (optimization).
"""
import os
import asyncio
import pytest
import httpx

BASE = os.environ.get("TEST_BASE_URL", "http://localhost:8000")
TEST_EMAIL = "pytest-fixture@thetaalgos.test"
TEST_USERNAME = "pytest_fixture_user"


def _provision_user_and_token():
    """Provision the test user with SYNC psycopg2 so we never bind the shared
    async engine to a throwaway loop (that previously poisoned later tests)."""
    import os
    import uuid as _uuid
    import psycopg2
    from app.core.security import create_access_token

    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    cn = psycopg2.connect(url, connect_timeout=5)
    try:
        with cn, cn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email = %s", (TEST_EMAIL,))
            row = cur.fetchone()
            if row is None:
                uid = str(_uuid.uuid4())
                cur.execute(
                    "INSERT INTO users (id, email, username, hashed_password, is_active, subscription_tier) "
                    "VALUES (%s, %s, %s, %s, TRUE, %s)",
                    (uid, TEST_EMAIL, TEST_USERNAME, "!login-disabled-test-fixture!", "tier_5"),
                )
            else:
                uid = str(row[0])
                cur.execute("UPDATE users SET subscription_tier='tier_5' WHERE id=%s", (uid,))
    finally:
        cn.close()
    token = create_access_token({"sub": uid})
    return uid, token


@pytest.fixture(scope="session")
def auth():
    uid, token = _provision_user_and_token()
    return {"user_id": uid, "token": token}


class _RetryingClient(httpx.Client):
    """httpx.Client that retries idempotent-ish calls once on transient
    ReadTimeout/ConnectError (the prod backend is CPU-contended by live
    watchers). Never retries on HTTP status — only on transport stalls."""
    def request(self, *args, **kwargs):
        try:
            return super().request(*args, **kwargs)
        except (httpx.ReadTimeout, httpx.ConnectError, httpx.NetworkError):
            import time as _t
            _t.sleep(2)
            return super().request(*args, **kwargs)


@pytest.fixture(scope="session")
def client(auth):
    headers = {"Authorization": f"Bearer {auth['token']}"}
    with _RetryingClient(base_url=BASE, headers=headers, timeout=45.0) as c:
        yield c


@pytest.fixture
def created_strategy_ids():
    """Track strategy ids created during a test so we can clean them up."""
    ids = []
    yield ids
    if ids:
        headers = {"Authorization": ""}
        # cleanup uses a fresh client with the same token via env not available
        # here; tests pass their own client to delete. This is a safety net only.


def cleanup_strategy(client, sid):
    try:
        client.delete(f"/api/v1/strategies/{sid}")
    except Exception:
        pass
