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
    from sqlalchemy import select
    from app.database import async_session_factory
    from app.models.user import User, SubscriptionTier
    from app.core.security import create_access_token

    async def go():
        async with async_session_factory() as db:
            res = await db.execute(select(User).where(User.email == TEST_EMAIL))
            u = res.scalar_one_or_none()
            if u is None:
                u = User(
                    email=TEST_EMAIL,
                    username=TEST_USERNAME,
                    hashed_password="!login-disabled-test-fixture!",
                    is_active=True,
                    subscription_tier=SubscriptionTier.TIER_5.value,
                )
                db.add(u)
                await db.commit()
                await db.refresh(u)
            else:
                # Make sure the tier is high enough even if a prior run left it lower
                if u.subscription_tier != SubscriptionTier.TIER_5.value:
                    u.subscription_tier = SubscriptionTier.TIER_5.value
                    await db.commit()
            return str(u.id)

    uid = asyncio.run(go())
    token = create_access_token({"sub": uid})
    return uid, token


@pytest.fixture(scope="session")
def auth():
    uid, token = _provision_user_and_token()
    return {"user_id": uid, "token": token}


@pytest.fixture(scope="session")
def client(auth):
    headers = {"Authorization": f"Bearer {auth['token']}"}
    with httpx.Client(base_url=BASE, headers=headers, timeout=60.0) as c:
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
