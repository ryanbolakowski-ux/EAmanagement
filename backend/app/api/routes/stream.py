"""Live dashboard stream — Server-Sent Events for the V2 dashboard.

WHY SSE AND NOT WEBSOCKET: SSE rides plain HTTP/1.1, so it flows through the
existing nginx reverse proxy on the box and the Vercel-origin CORS setup with
ZERO infra changes — a WebSocket would need an Upgrade-aware proxy block and
a separate wss:// origin story. The dashboard only needs server→client pushes
(no client→server messages), which is exactly SSE's shape. The tradeoff we
accept: no client→server channel, and the browser EventSource API cannot set
an Authorization header — which is why the JWT arrives as a `?token=` query
param below and is validated EXACTLY the way `get_current_user` does it.

The stream re-uses the SAME route helpers the dashboard already polls
(trades.get_open_positions / live_trading.get_portfolio_summary /
scanner.scanner_history) so there is one implementation of each query, and
only emits an event when a payload actually changed (hash compare) — an idle
dashboard costs a heartbeat comment every 15s and nothing else.

Feature flag: ENABLE_SSE_DASHBOARD (default "1"). The route is additive and
harmless — flipping the flag to "0" 404s it and the frontend falls back to
plain polling automatically.
"""
import asyncio
import hashlib
import json
import os
import time
from collections import defaultdict
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from jose import JWTError
from loguru import logger
from sqlalchemy import select
from starlette.background import BackgroundTask

from app.core.security import decode_token
from app.database import AsyncSessionLocal
from app.models.user import User

router = APIRouter()

# ── Tunables ─────────────────────────────────────────────────────────────────
TICK_SECONDS = 3.0        # base gather cadence — positions refresh this fast
HEARTBEAT_SECONDS = 15.0  # ": heartbeat" comment cadence (keeps proxies from
                          # reaping an idle connection)
# pnl + signals gather every Nth tick (~15s). Both of those route helpers make
# best-effort Polygon HTTP calls inside (mark-to-market on open live trades /
# pick-outcome resolution) — at the raw 3s tick rate that would hammer a
# rate-limited key for data that moves on minute timescales. 15s is still
# 4-20x faster than the intervals the dashboard polls those endpoints at.
SLOW_EVERY_TICKS = 5
MAX_STREAMS_PER_USER = 2  # per-user concurrent stream cap (module-level count)

# user_id (str) -> number of currently-open streams. Module-level on purpose:
# one worker process serves the app, so this is the whole truth.
_ACTIVE_STREAMS: dict[str, int] = defaultdict(int)


def _sse_enabled() -> bool:
    # Read at request time (not import time) so tests — and a paranoid ops
    # rollback — never need a module re-import dance to flip it.
    return os.environ.get("ENABLE_SSE_DASHBOARD", "1") == "1"


# ── Auth (query-param JWT) ───────────────────────────────────────────────────
# EventSource cannot set headers, so the JWT rides `?token=`. Validation is a
# faithful copy of core.auth.get_current_user's contract: decode via the same
# decode_token helper (jose validates `exp` inside decode(), so expired tokens
# fail here too), require `sub`, then require an active user row.
# OPS NOTE: query strings land in nginx/uvicorn access logs, so the raw JWT
# is written to disk on every connect/reconnect. Access tokens are
# short-lived, but the nginx vhost should still scrub/truncate the query
# string for /api/v1/stream/ before log rotation ships those lines anywhere.

def _decode_stream_user_id(token: str) -> str:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(token)
        user_id: str = payload.get("sub")
        if not user_id:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    return user_id


async def _load_stream_user(user_id: str) -> User:
    """Fetch + validate the user with a SHORT-LIVED session. Deliberately not
    Depends(get_db): a dependency session would stay checked out of the pool
    for the entire (potentially hours-long) life of the stream."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


# ── Data sources (reuse the exact route helpers the dashboard polls) ─────────
# Imports are lazy so importing this module never drags in the heavy route
# modules (pandas, broker engines, ...) — matters for tests and startup time.

async def _gather_positions(user: User, db) -> list:
    from app.api.routes.trades import get_open_positions as _open_positions
    return await _open_positions(current_user=user, db=db)


async def _gather_pnl(user: User, db):
    # Mirror the require_kyc_verified dependency that gates
    # GET /live-trading/portfolio-summary — calling the route function
    # directly bypasses FastAPI's dependency machinery, so the gate must be
    # re-applied by hand. Non-KYC users simply get no `pnl` events, matching
    # the 403 (+retry:false) their poller sees today.
    if (getattr(user, "kyc_status", None) or "not_started") != "verified":
        return None
    from app.api.routes.live_trading import get_portfolio_summary as _portfolio
    return await _portfolio(current_user=user, db=db)


async def _gather_signals(user: User, db):
    from app.api.routes.scanner import scanner_history as _history
    # Same shape the dashboard polls: scannerApi.history(1, 'all')
    return await _history(days=1, asset_type="all", current_user=user, db=db)


# (event name, gather coroutine, gather-every-N-ticks) — module-level so tests
# can swap in fakes without touching the loop.
_SOURCES = (
    ("positions", _gather_positions, 1),
    ("pnl",       _gather_pnl,       SLOW_EVERY_TICKS),
    ("signals",   _gather_signals,   SLOW_EVERY_TICKS),
)


# ── SSE plumbing ─────────────────────────────────────────────────────────────

def _json_default(o):
    """Match FastAPI's jsonable_encoder closely enough that stream payloads
    are byte-compatible with what the REST endpoints return: datetimes →
    isoformat, Decimals → float, UUIDs/anything else → str."""
    if isinstance(o, Decimal):
        return float(o)
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return str(o)


def _dumps(payload) -> str:
    return json.dumps(payload, default=_json_default, sort_keys=True)


def _fingerprint(serialized: str) -> str:
    return hashlib.sha1(serialized.encode()).hexdigest()


def _sse_event(name: str, serialized: str) -> str:
    return f"event: {name}\ndata: {serialized}\n\n"


async def _client_disconnected(request: Request) -> bool:
    """Non-blocking poll of the ASGI receive channel. Module-level (not
    inlined) so tests can monkeypatch a simulated disconnect — httpx's
    ASGITransport only hands back the response once the body generator
    actually terminates."""
    try:
        return await request.is_disconnected()
    except Exception:
        # If we can't even ask, assume the peer is gone — the safe direction.
        return True


def _make_slot_release(uid: str, email: str):
    """One-shot releaser for a claimed concurrency slot.

    The slot is claimed in the route body (so a 3rd stream gets a real 429),
    but the generator's finally alone is NOT enough to release it: if the
    client disconnects in the window between the route returning and Starlette
    starting body iteration (EventSource close racing connect — StrictMode
    double-mount, instant navigation), listen_for_disconnect can cancel the
    response task group before the generator ever starts — and an UNSTARTED
    async generator's finally never runs, leaking the slot until restart.
    So the release also rides the response's BackgroundTask, which Starlette
    runs after the task group exits, disconnect or not. Both callers hit this
    same closure; the `done` flag makes the second call a no-op, so the count
    can neither leak up nor drift below zero."""
    done = {"released": False}

    def _release() -> None:
        if done["released"]:
            return
        done["released"] = True
        _ACTIVE_STREAMS[uid] = max(0, _ACTIVE_STREAMS[uid] - 1)
        logger.info(f"[stream] dashboard stream closed for {email} ({_ACTIVE_STREAMS[uid]} still active)")

    return _release


async def _dashboard_event_source(request: Request, user: User, release_slot):
    """The long-lived generator behind the response. One fresh DB session per
    tick (never one held for the stream's lifetime), per-source hash compare
    so unchanged payloads emit nothing, heartbeat comment every 15s."""
    last_hash: dict[str, str] = {}
    tick = 0
    last_beat = time.monotonic()
    try:
        # Flush something immediately so nginx/browsers commit the connection.
        yield ": stream open\n\n"
        while True:
            if await _client_disconnected(request):
                break
            try:
                async with AsyncSessionLocal() as db:
                    for name, gather, every in _SOURCES:
                        if tick % every != 0:
                            continue
                        try:
                            payload = await gather(user, db)
                        except Exception as e:
                            # One broken source must not kill the stream —
                            # the panel it feeds keeps its last data and the
                            # poller safety-net still exists client-side.
                            logger.warning(f"[stream] {name} gather failed for {user.email}: {type(e).__name__}: {e}")
                            continue
                        if payload is None:  # gated source (e.g. pnl pre-KYC)
                            continue
                        serialized = _dumps(payload)
                        digest = _fingerprint(serialized)
                        if last_hash.get(name) == digest:
                            continue  # unchanged — emit nothing
                        last_hash[name] = digest
                        yield _sse_event(name, serialized)
            except Exception as e:
                # A dead DB degrades to heartbeats rather than tearing the
                # stream down; next tick retries with a fresh session.
                logger.warning(f"[stream] tick failed for {user.email}: {type(e).__name__}: {e}")

            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_SECONDS:
                yield ": heartbeat\n\n"
                last_beat = now

            tick += 1
            await asyncio.sleep(TICK_SECONDS)
    finally:
        # Runs on client disconnect (GeneratorExit / CancelledError) AND on
        # clean break. Idempotent — the response's BackgroundTask calls the
        # same closure to cover the generator-never-started race (see
        # _make_slot_release).
        release_slot()


# ── Route ────────────────────────────────────────────────────────────────────

@router.get("/dashboard")
async def stream_dashboard(request: Request, token: str = ""):
    """SSE feed of the V2 dashboard's live data.

    Named events (each `data:` line is a JSON payload identical to the REST
    endpoint the dashboard polls today):
      positions — GET /api/v1/trades/open-positions
      pnl       — GET /api/v1/live-trading/portfolio-summary (KYC users only)
      signals   — GET /api/v1/scanner/history?days=1&asset_type=all
    """
    if not _sse_enabled():
        # Flag off ⇒ behave like the route was never shipped.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await _load_stream_user(_decode_stream_user_id(token))

    uid = str(user.id)
    if _ACTIVE_STREAMS[uid] >= MAX_STREAMS_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many concurrent dashboard streams (max {MAX_STREAMS_PER_USER}) — close another tab.",
        )
    # Claim the slot HERE (no awaits between check and increment ⇒ no race on
    # a single event loop). Released by a one-shot closure wired into BOTH the
    # generator's finally and the response's BackgroundTask — the finally
    # alone leaks the slot when a disconnect lands before body iteration
    # starts (see _make_slot_release).
    _ACTIVE_STREAMS[uid] += 1
    logger.info(f"[stream] dashboard stream opened for {user.email} ({_ACTIVE_STREAMS[uid]} active)")
    release_slot = _make_slot_release(uid, user.email)

    return StreamingResponse(
        _dashboard_event_source(request, user, release_slot),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx: never buffer an event stream
        },
        # Starlette runs this after the response task group exits — even when
        # a pre-iteration disconnect cancels the body before it ever starts.
        background=BackgroundTask(release_slot),
    )
