"""Tests for the V2 dashboard SSE stream (app/api/routes/stream.py).

These run fully in-process against a throwaway FastAPI app — no live server
and no database. The JWT decode path is exercised for real (same SECRET_KEY /
decode_token as prod); the DB user lookup and the data-gather helpers are
monkeypatched because they're module-level seams designed exactly for this.

httpx's ASGITransport only hands the response back once the body generator
terminates, so the "client disconnect" is simulated by monkeypatching
stream._client_disconnected to flip True after a few ticks — which drives the
same break + finally-cleanup path a real disconnect does.

Run with: pytest tests/test_sse_dashboard_stream.py -q
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api.routes import stream as stream_mod
from app.core.security import create_access_token


@pytest.fixture
def anyio_backend():
    # anyio ships its own pytest plugin — no pytest-asyncio dependency needed.
    return "asyncio"


FAKE_UID = "11111111-1111-1111-1111-111111111111"


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(stream_mod.router, prefix="/api/v1/stream")
    return app


def _fake_user():
    return SimpleNamespace(
        id=FAKE_UID,
        email="sse-test@thetaalgos.test",
        is_active=True,
        kyc_status="verified",
    )


def _patch_auth_user(monkeypatch):
    user = _fake_user()

    async def fake_load(user_id):
        assert user_id == FAKE_UID
        return user

    monkeypatch.setattr(stream_mod, "_load_stream_user", fake_load)
    return user


def _patch_disconnect_after(monkeypatch, ticks: int):
    seen = {"n": 0}

    async def fake_disconnected(request):
        seen["n"] += 1
        return seen["n"] > ticks

    monkeypatch.setattr(stream_mod, "_client_disconnected", fake_disconnected)


def _client(app):
    # Explicit timeout on EVERY client: no stream test may ever await an
    # unbounded read. The streams themselves are bounded server-side by
    # _patch_disconnect_after, this is the second belt-and-braces layer.
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        timeout=httpx.Timeout(15.0, connect=5.0, read=15.0),
    )


# ── Happy path: events flow, disconnect cleans up ────────────────────────────

@pytest.mark.anyio
async def test_stream_yields_events_then_disconnect_cleans_up(monkeypatch):
    calls = {"n": 0}

    async def fake_gather(user, db):
        calls["n"] += 1
        return {"tick": calls["n"]}  # changes every tick → emits every tick

    monkeypatch.setattr(stream_mod, "_SOURCES", (("positions", fake_gather, 1),))
    monkeypatch.setattr(stream_mod, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(stream_mod, "HEARTBEAT_SECONDS", 0.0)  # heartbeat every tick
    _patch_auth_user(monkeypatch)
    _patch_disconnect_after(monkeypatch, ticks=4)

    token = create_access_token({"sub": FAKE_UID})
    events, heartbeats, datas = [], 0, []
    async with _client(_make_app()) as client:
        async with client.stream("GET", f"/api/v1/stream/dashboard?token={token}") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            assert resp.headers.get("x-accel-buffering") == "no"
            # Consume EXACTLY 2 complete named events, then break out and let
            # the context managers close the response + client explicitly —
            # never drain an event stream to exhaustion in a test.
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    events.append(line.removeprefix("event: "))
                elif line.startswith("data: "):
                    datas.append(line.removeprefix("data: "))
                elif line == ": heartbeat":
                    heartbeats += 1
                if len(datas) >= 2:
                    break

    assert len(events) == 2, f"expected exactly 2 named events, got {events}"
    assert set(events) == {"positions"}
    assert any('"tick": 1' in d or '"tick":1' in d for d in datas)
    assert heartbeats >= 1
    # try/finally released the per-user concurrency slot
    assert stream_mod._ACTIVE_STREAMS[FAKE_UID] == 0


@pytest.mark.anyio
async def test_unchanged_payload_is_suppressed(monkeypatch):
    async def constant_gather(user, db):
        return {"static": True}  # identical every tick → hash-compare suppresses

    monkeypatch.setattr(stream_mod, "_SOURCES", (("pnl", constant_gather, 1),))
    monkeypatch.setattr(stream_mod, "TICK_SECONDS", 0.01)
    _patch_auth_user(monkeypatch)
    _patch_disconnect_after(monkeypatch, ticks=5)

    token = create_access_token({"sub": FAKE_UID})
    events = []
    async with _client(_make_app()) as client:
        async with client.stream("GET", f"/api/v1/stream/dashboard?token={token}") as resp:
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    events.append(line)

    assert events == ["event: pnl"], f"unchanged payload must emit exactly once, got {events}"
    assert stream_mod._ACTIVE_STREAMS[FAKE_UID] == 0


# ── Auth: bad / expired / missing tokens are 401 before any DB touch ─────────

@pytest.mark.anyio
async def test_bad_and_expired_tokens_rejected_401(monkeypatch):
    async def must_not_be_called(user_id):  # bad tokens die at decode
        raise AssertionError("_load_stream_user must not run for invalid tokens")

    monkeypatch.setattr(stream_mod, "_load_stream_user", must_not_be_called)

    expired = create_access_token({"sub": FAKE_UID}, expires_delta=timedelta(minutes=-5))
    no_sub = create_access_token({"not_sub": "x"})

    async with _client(_make_app()) as client:
        for bad in ("not-a-jwt", expired, no_sub):
            resp = await client.get(f"/api/v1/stream/dashboard?token={bad}")
            assert resp.status_code == 401, f"token {bad[:20]!r} should be rejected"
        # Missing token entirely
        resp = await client.get("/api/v1/stream/dashboard")
        assert resp.status_code == 401


# ── Feature flag: off ⇒ 404 ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_flag_off_is_404(monkeypatch):
    monkeypatch.setenv("ENABLE_SSE_DASHBOARD", "0")
    token = create_access_token({"sub": FAKE_UID})
    async with _client(_make_app()) as client:
        resp = await client.get(f"/api/v1/stream/dashboard?token={token}")
    assert resp.status_code == 404


# ── Concurrency cap: 3rd stream for a user is refused ────────────────────────

@pytest.mark.anyio
async def test_per_user_stream_cap(monkeypatch):
    _patch_auth_user(monkeypatch)
    token = create_access_token({"sub": FAKE_UID})
    stream_mod._ACTIVE_STREAMS[FAKE_UID] = stream_mod.MAX_STREAMS_PER_USER
    try:
        async with _client(_make_app()) as client:
            resp = await client.get(f"/api/v1/stream/dashboard?token={token}")
        assert resp.status_code == 429
    finally:
        stream_mod._ACTIVE_STREAMS.pop(FAKE_UID, None)


# ── Slot-leak race: disconnect before the body generator ever starts ─────────
# Starlette's listen_for_disconnect can consume an already-queued
# http.disconnect and cancel the response task group BEFORE stream_response
# starts iterating the body — and an unstarted async generator's finally never
# runs. The slot release therefore also rides the response's BackgroundTask
# (which Starlette runs after the task group exits either way) and is
# idempotent so the finally+background double-call in the normal path is a
# no-op. We drive the route function directly so the body is never iterated —
# exactly what that race looks like from the generator's point of view.

@pytest.mark.anyio
async def test_slot_released_when_generator_never_starts(monkeypatch):
    from fastapi import Request

    _patch_auth_user(monkeypatch)
    token = create_access_token({"sub": FAKE_UID})
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/stream/dashboard",
        "query_string": b"",
        "headers": [],
    }
    try:
        resp = await stream_mod.stream_dashboard(Request(scope), token=token)
        # Slot was claimed in the route body...
        assert stream_mod._ACTIVE_STREAMS[FAKE_UID] == 1
        # ...and the release is wired as the response's background task, so it
        # fires even if the body generator never starts.
        assert resp.background is not None
        await resp.background()
        assert stream_mod._ACTIVE_STREAMS[FAKE_UID] == 0
        # Idempotent: the normal path calls it twice (finally + background).
        await resp.background()
        assert stream_mod._ACTIVE_STREAMS[FAKE_UID] == 0
        # Tidy up the never-started generator (finally is a no-op release now).
        await resp.body_iterator.aclose()
        assert stream_mod._ACTIVE_STREAMS[FAKE_UID] == 0
    finally:
        stream_mod._ACTIVE_STREAMS.pop(FAKE_UID, None)
