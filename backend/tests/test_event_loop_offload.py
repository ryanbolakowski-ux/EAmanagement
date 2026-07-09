"""Event-loop offload regression tests (LOOP-OFFLOAD-V1).

The 24s login / 676 blocking yfinance-calls-per-hour incident: sync network
and bcrypt calls were running directly on the asyncio event loop. Every such
call reachable from an async context must now go through
``await asyncio.to_thread(...)``.

Covers:
  * /login stays responsive while bcrypt verify runs (0.2s fake) — a ticker
    task keeps ticking during the verify, proving the loop is not blocked.
  * every touched module still imports.
  * the specific async functions actually contain the to_thread offload.
  * scanner outcome resolution stops after 3 consecutive empty tickers when
    POLYGON_API_KEY is absent (no pointless hammering).
"""
import asyncio
import importlib
import inspect
import time
import types
from datetime import datetime, timedelta

import pytest


# ── 1. login's verify path must not block the loop ─────────────────────────

def test_login_verify_password_offloaded(monkeypatch):
    from app.api.routes import auth as auth_mod
    from fastapi import HTTPException

    calls = {"n": 0}

    def slow_verify(plain, hashed):
        calls["n"] += 1
        time.sleep(0.2)  # simulates bcrypt cost — must run OFF the loop
        return False

    monkeypatch.setattr(auth_mod, "verify_password", slow_verify)
    # No-op the redis rate limiter so the test needs no Redis.
    monkeypatch.setattr(auth_mod, "_enforce_login_rate_limit", lambda request: None)

    fake_user = types.SimpleNamespace(
        hashed_password="$2b$12$fakefakefakefakefakefake",
        is_active=True, totp_enabled=False, totp_secret=None,
    )

    class FakeResult:
        def scalar_one_or_none(self):
            return fake_user

    class FakeDB:
        async def execute(self, *a, **k):
            return FakeResult()

    form = types.SimpleNamespace(username="user@example.com", password="wrong-pw")

    async def scenario():
        done = asyncio.Event()
        ticks = {"n": 0}

        async def call_login():
            try:
                await auth_mod.login(None, form, FakeDB())
                raise AssertionError("login should 401 on a failed verify")
            except HTTPException as e:
                assert e.status_code == 401
            finally:
                done.set()

        async def ticker():
            # Only count ticks while login is in flight.
            while not done.is_set():
                ticks["n"] += 1
                await asyncio.sleep(0.01)

        await asyncio.gather(call_login(), ticker())
        return ticks["n"]

    ticks = asyncio.run(scenario())
    assert calls["n"] == 1
    # Offloaded 0.2s verify -> the 10ms ticker gets ~15-20 iterations.
    # A blocking (on-loop) verify starves it to ~1-2. Threshold 8 = safe margin.
    assert ticks >= 8, (
        f"event loop appears BLOCKED during login verify (ticker ran {ticks}x; "
        "expected >= 8 — is verify_password still called synchronously?)"
    )


# ── 2. every touched module still imports ───────────────────────────────────

TOUCHED_MODULES = [
    "app.api.routes.auth",
    "app.api.routes.admin",
    "app.api.routes.scanner",
    "app.api.routes.paper_trading",
    "app.api.routes.live_trading",
    "app.engines.data_feeds.polygon_feed",
    "app.engines.data_feeds.local_cache",
    "app.engines.data_feeds.tv_feed",
    "app.engines.options.momentum_scanner",
    "app.engines.options.options_paper_runner",
]


@pytest.mark.parametrize("module_name", TOUCHED_MODULES)
def test_touched_module_imports(module_name):
    mod = importlib.import_module(module_name)
    assert mod is not None


# ── 3. the async offload sites actually offload ─────────────────────────────

def _source_of(module_name, attr):
    mod = importlib.import_module(module_name)
    return inspect.getsource(getattr(mod, attr))


@pytest.mark.parametrize("module_name,attr", [
    ("app.api.routes.auth", "login"),
    ("app.api.routes.auth", "diag_login"),
    ("app.api.routes.admin", "verify_admin_passcode"),
    ("app.api.routes.scanner", "_resolve_email_signal_outcomes"),
    ("app.api.routes.scanner", "open_positions"),
    ("app.api.routes.live_trading", "get_unrealized_pnl"),
    ("app.engines.data_feeds.polygon_feed", "fetch_polygon_data"),
    ("app.engines.data_feeds.local_cache", "fetch_from_cache"),
    ("app.engines.data_feeds.tv_feed", "_fetch_yfinance_robust"),
    ("app.engines.options.momentum_scanner", "_fetch_market_snapshot"),
    ("app.engines.options.options_paper_runner", "_run"),
])
def test_async_site_uses_to_thread(module_name, attr):
    src = _source_of(module_name, attr)
    assert "asyncio.to_thread(" in src, (
        f"{module_name}.{attr} lost its asyncio.to_thread offload"
    )


def test_no_naked_sync_calls_left_at_fixed_sites():
    """The exact patterns that blocked the loop must be gone."""
    src = _source_of("app.api.routes.auth", "login")
    assert "not await asyncio.to_thread(verify_password" in src

    src = _source_of("app.api.routes.scanner", "_resolve_email_signal_outcomes")
    assert "asyncio.to_thread(_polygon_daily_range" in src

    src = _source_of("app.engines.options.options_paper_runner", "_run")
    assert "asyncio.to_thread(_fetch_spot" in src


# ── 4. scanner outcome resolution: no-key bailout after 3 empty tickers ────

def test_resolve_outcomes_bails_out_without_polygon_key(monkeypatch):
    from app.api.routes import scanner as scanner_mod

    # bailout requires BOTH data keys absent (FMP fallback can still resolve)
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    monkeypatch.delenv("FMP_API_KEY", raising=False)

    calls = {"n": 0}

    def always_empty(ticker, start, end):
        calls["n"] += 1
        return []

    monkeypatch.setattr(scanner_mod, "_polygon_daily_range", always_empty)

    picked = datetime.utcnow() - timedelta(days=2)
    rows = [
        types.SimpleNamespace(id=i, ticker=f"TK{i}", entry=10.0, stop=9.0,
                              target=12.0, picked_at=picked)
        for i in range(10)
    ]

    class FakeRes:
        def fetchall(self):
            return rows

    class FakeDB:
        async def execute(self, *a, **k):
            return FakeRes()

        async def commit(self):
            raise AssertionError("nothing should be committed on the bailout path")

    resolved = asyncio.run(scanner_mod._resolve_email_signal_outcomes(FakeDB()))
    assert resolved == 0
    assert calls["n"] == 3, (
        f"expected exactly 3 attempts before the no-key bailout, got {calls['n']}"
    )


def test_resolve_outcomes_keeps_going_with_polygon_key(monkeypatch):
    """With a key present, empty tickers must NOT abort the pass (semantics
    unchanged: a per-ticker miss just skips that row)."""
    from app.api.routes import scanner as scanner_mod

    monkeypatch.setenv("POLYGON_API_KEY", "test-key-present")

    calls = {"n": 0}

    def always_empty(ticker, start, end):
        calls["n"] += 1
        return []

    monkeypatch.setattr(scanner_mod, "_polygon_daily_range", always_empty)

    picked = datetime.utcnow() - timedelta(days=2)
    rows = [
        types.SimpleNamespace(id=i, ticker=f"TK{i}", entry=10.0, stop=9.0,
                              target=12.0, picked_at=picked)
        for i in range(5)
    ]

    class FakeRes:
        def fetchall(self):
            return rows

    class FakeDB:
        async def execute(self, *a, **k):
            return FakeRes()

        async def commit(self):
            pass

    resolved = asyncio.run(scanner_mod._resolve_email_signal_outcomes(FakeDB()))
    assert resolved == 0
    assert calls["n"] == 5, "with a key set, all rows must still be attempted"
