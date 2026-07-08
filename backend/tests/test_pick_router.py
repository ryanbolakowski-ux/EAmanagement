"""route_pick_to_live safety ladder — pure/monkeypatched matrix.

NO network, NO redis, NO DB, NO broker: every I/O helper on
app.engines.options.pick_router is monkeypatched. Each safety rung is broken
one at a time and must skip with its specific reason (fail-CLOSED); the
full-pass test asserts the fake broker receives the entry order
(ticker/shares) and the exit registration receives stop/target.

Also covers LIVE-PRICE ENTRY HONESTY (theta_scanner._entry_basis): the entry
basis must be the enrichment live quote when present (FDUS 2026-07-07:
emailed $20.41 off the delayed snapshot while the stock traded $19.84).
"""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.engines.options import pick_router as pr
from app.engines.live_trading.broker_base import (
    OrderResponse, OrderSide, OrderStatus, OrderType)

ET = ZoneInfo("America/New_York")
UID = "11111111-1111-1111-1111-111111111111"
EMAIL = "unit@test.local"

PICK = {"ticker": "FDUS", "price": 20.41, "entry": 19.84,
        "stop": 19.20, "target": 21.30, "watch_only": False}

ACCT = {"id": "22222222-2222-2222-2222-222222222222",
        "account_type": "margin", "allocation_usd": 5000.0,
        "cached_cash": 99809.31, "cached_buying_power": 199618.62,
        "is_sandbox": True}


def _run(coro):
    return asyncio.run(coro)


class FakeBroker:
    def __init__(self, reject=False):
        self.orders = []
        self.reject = reject

    async def place_order(self, order):
        self.orders.append(order)
        if self.reject:
            return OrderResponse(broker_order_id="", status=OrderStatus.REJECTED,
                                 message="rejected by fake")
        return OrderResponse(broker_order_id="FAKE-1", status=OrderStatus.PENDING)


@pytest.fixture
def audits(monkeypatch):
    rec = []

    async def fake_audit(user_id, detail):
        rec.append((user_id, detail))

    monkeypatch.setattr(pr, "_audit", fake_audit)
    return rec


@pytest.fixture
def happy(monkeypatch, audits):
    """Every rung passes; individual tests then break exactly one rung."""
    monkeypatch.setenv("THETA_LIVE_PICK_ROUTING", "1")

    async def claim(user_id, date_str, ticker):
        return True

    async def acct(user_id):
        return dict(ACCT)

    async def guard(user_id, broker_account_id, *, context=None):
        return True, "ok"

    async def quote(ticker):
        return 19.84

    async def clear(date_str, user_id):
        clear.calls.append((date_str, user_id))
    clear.calls = []

    async def register(**kw):
        register.calls.append(kw)
    register.calls = []

    fake = FakeBroker()

    async def get_broker(broker_account_id):
        return fake

    monkeypatch.setattr(pr, "_claim_daily_slot", claim)
    monkeypatch.setattr(pr, "_lookup_live_account", acct)
    monkeypatch.setattr(pr, "auto_trade_allowed", guard)
    monkeypatch.setattr(pr, "_fetch_live_quote", quote)
    monkeypatch.setattr(pr, "_market_open_now", lambda now_et=None: True)
    monkeypatch.setattr(pr, "_clear_legacy_pending", clear)
    monkeypatch.setattr(pr, "_register_position", register)
    monkeypatch.setattr(pr, "_get_broker", get_broker)
    return {"broker": fake, "register": register, "clear": clear,
            "monkeypatch": monkeypatch}


# ── safety-ladder skip matrix (one broken rung per test) ────────────────────

def test_rung1_env_kill_switch(happy, audits, monkeypatch):
    monkeypatch.setenv("THETA_LIVE_PICK_ROUTING", "0")
    placed, reason = _run(pr.route_pick_to_live(UID, EMAIL, dict(PICK)))
    assert (placed, reason) == (False, "routing_disabled_by_env")
    assert happy["broker"].orders == []
    assert audits and audits[-1][1]["reason"] == "routing_disabled_by_env"


def test_env_default_is_on():
    import os
    os.environ.pop("THETA_LIVE_PICK_ROUTING", None)
    assert pr._routing_enabled() is True


def test_rung2_duplicate_day(happy, audits, monkeypatch):
    async def claim(user_id, date_str, ticker):
        return False
    monkeypatch.setattr(pr, "_claim_daily_slot", claim)
    placed, reason = _run(pr.route_pick_to_live(UID, EMAIL, dict(PICK)))
    assert (placed, reason) == (False, "already_routed_today")
    assert happy["broker"].orders == []


def test_rung3_no_broker_account(happy, audits, monkeypatch):
    async def acct(user_id):
        return None
    monkeypatch.setattr(pr, "_lookup_live_account", acct)
    placed, reason = _run(pr.route_pick_to_live(UID, EMAIL, dict(PICK)))
    assert (placed, reason) == (False, "no_live_broker_account")
    assert happy["broker"].orders == []


def test_rung4_no_allocation(happy, audits, monkeypatch):
    for bad in (None, 0, -5):
        a = dict(ACCT)
        a["allocation_usd"] = bad

        async def acct(user_id, _a=a):
            return _a
        monkeypatch.setattr(pr, "_lookup_live_account", acct)
        placed, reason = _run(pr.route_pick_to_live(UID, EMAIL, dict(PICK)))
        assert placed is False
        assert reason == "no daily allocation set — set it on the Live Trading page"
    assert happy["broker"].orders == []


def test_rung5_guard_blocks(happy, audits, monkeypatch):
    async def guard(user_id, broker_account_id, *, context=None):
        return False, "account_trading_enabled_off"
    monkeypatch.setattr(pr, "auto_trade_allowed", guard)
    placed, reason = _run(pr.route_pick_to_live(UID, EMAIL, dict(PICK)))
    assert (placed, reason) == (False, "auto_trade_blocked:account_trading_enabled_off")
    assert happy["broker"].orders == []


def test_rung6_quote_missing(happy, audits, monkeypatch):
    async def quote(ticker):
        return None
    monkeypatch.setattr(pr, "_fetch_live_quote", quote)
    placed, reason = _run(pr.route_pick_to_live(UID, EMAIL, dict(PICK)))
    assert (placed, reason) == (False, "live_quote_unavailable")
    assert happy["broker"].orders == []


def test_rung6_quote_drift_over_3pct(happy, audits, monkeypatch):
    async def quote(ticker):
        return 20.90  # vs pick entry 19.84 -> ~5.3% drift
    monkeypatch.setattr(pr, "_fetch_live_quote", quote)
    placed, reason = _run(pr.route_pick_to_live(UID, EMAIL, dict(PICK)))
    assert placed is False
    assert reason.startswith("quote_drift_") and reason.endswith("exceeds_3pct")
    assert happy["broker"].orders == []


def test_rung7_below_one_share(happy, audits, monkeypatch):
    a = dict(ACCT)
    a["allocation_usd"] = 10.0  # 10 / 19.84 < 1 share

    async def acct(user_id):
        return a
    monkeypatch.setattr(pr, "_lookup_live_account", acct)
    placed, reason = _run(pr.route_pick_to_live(UID, EMAIL, dict(PICK)))
    assert placed is False
    assert reason.startswith("allocation_below_1_share")
    assert happy["broker"].orders == []


def test_rung8_market_closed(happy, audits, monkeypatch):
    monkeypatch.setattr(pr, "_market_open_now", lambda now_et=None: False)
    placed, reason = _run(pr.route_pick_to_live(UID, EMAIL, dict(PICK)))
    assert (placed, reason) == (False, "market_closed")
    assert happy["broker"].orders == []


def test_rung0_watch_only_never_trades(happy, audits):
    p = dict(PICK)
    p["watch_only"] = True
    placed, reason = _run(pr.route_pick_to_live(UID, EMAIL, p))
    assert (placed, reason) == (False, "watch_only_pick_never_trades")
    assert happy["broker"].orders == []


def test_rung0_invalid_levels(happy, audits):
    p = dict(PICK)
    p["stop"] = 25.00  # stop above entry — nonsense for a long
    placed, reason = _run(pr.route_pick_to_live(UID, EMAIL, p))
    assert (placed, reason) == (False, "pick_levels_invalid")
    assert happy["broker"].orders == []


def test_every_skip_is_audited(happy, audits, monkeypatch):
    monkeypatch.setenv("THETA_LIVE_PICK_ROUTING", "0")
    _run(pr.route_pick_to_live(UID, EMAIL, dict(PICK)))
    uid, detail = audits[-1]
    assert uid == UID
    assert detail["placed"] is False
    assert detail["ticker"] == "FDUS"


# ── sizing math ─────────────────────────────────────────────────────────────

def test_sizing_allocation_5000_at_19_84_is_252_shares():
    assert pr.compute_router_shares(5000, 19.84) == 252


def test_sizing_funds_cap_and_invalid_inputs():
    # cash account capped by cached_cash below allocation
    assert pr.compute_router_shares(5000, 19.84, cached_cash=100.0,
                                    account_type="cash") == 5
    # margin account capped by buying power
    assert pr.compute_router_shares(5000, 19.84, cached_buying_power=200.0,
                                    account_type="margin") == 10
    # fail-closed on garbage
    assert pr.compute_router_shares(None, 19.84) == 0
    assert pr.compute_router_shares(5000, 0) == 0
    assert pr.compute_router_shares(-1, 19.84) == 0
    assert pr.compute_router_shares("x", "y") == 0


# ── market-hours helper ─────────────────────────────────────────────────────

def test_market_hours_window():
    tue_936 = datetime(2026, 7, 7, 9, 36, tzinfo=ET)     # Tuesday
    tue_900 = datetime(2026, 7, 7, 9, 0, tzinfo=ET)
    tue_1556 = datetime(2026, 7, 7, 15, 56, tzinfo=ET)
    sat = datetime(2026, 7, 11, 10, 0, tzinfo=ET)
    assert pr._market_open_now(tue_936) is True
    assert pr._market_open_now(tue_900) is False
    assert pr._market_open_now(tue_1556) is False
    assert pr._market_open_now(sat) is False


# ── full pass with a fake broker ────────────────────────────────────────────

def test_full_pass_places_order_with_ticker_shares_stop_target(happy, audits):
    placed, reason = _run(pr.route_pick_to_live(UID, EMAIL, dict(PICK)))
    assert (placed, reason) == (True, "placed")

    # entry order: ticker + shares (5000 alloc @ 19.84 quote -> 252 shares)
    assert len(happy["broker"].orders) == 1
    order = happy["broker"].orders[0]
    assert order.instrument == "FDUS"
    assert order.quantity == 252
    assert order.side == OrderSide.BUY
    assert order.order_type == OrderType.MARKET

    # exit registration: stop + target handed to the server-side exit manager
    assert len(happy["register"].calls) == 1
    reg = happy["register"].calls[0]
    assert reg["ticker"] == "FDUS"
    assert reg["shares"] == 252
    assert reg["stop"] == pytest.approx(19.20)
    assert reg["target"] == pytest.approx(21.30)
    assert reg["broker_order_id"] == "FAKE-1"

    # legacy $1000 pending-entry queue deduped
    assert len(happy["clear"].calls) == 1

    # placement audited
    uid, detail = audits[-1]
    assert detail["placed"] is True
    assert detail["shares"] == 252
    assert detail["broker_order_id"] == "FAKE-1"


def test_entry_rejected_is_not_placed(happy, audits, monkeypatch):
    fake = FakeBroker(reject=True)

    async def get_broker(broker_account_id):
        return fake
    monkeypatch.setattr(pr, "_get_broker", get_broker)
    placed, reason = _run(pr.route_pick_to_live(UID, EMAIL, dict(PICK)))
    assert placed is False
    assert reason.startswith("entry_rejected:")
    assert happy["register"].calls == []  # never register a rejected entry


# ── LIVE-PRICE ENTRY HONESTY (task C) ───────────────────────────────────────

def test_entry_basis_uses_live_price_when_present():
    from app.engines.options.theta_scanner import _entry_basis
    assert _entry_basis({"price": 20.41, "live_price": 19.84}) == pytest.approx(19.84)


def test_entry_basis_falls_back_to_snapshot():
    from app.engines.options.theta_scanner import _entry_basis
    assert _entry_basis({"price": 20.41}) == pytest.approx(20.41)
    assert _entry_basis({"price": 20.41, "live_price": None}) == pytest.approx(20.41)
    assert _entry_basis({"price": 20.41, "live_price": 0}) == pytest.approx(20.41)
    assert _entry_basis({"price": 20.41, "live_price": "junk"}) == pytest.approx(20.41)
