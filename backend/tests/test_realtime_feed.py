"""REALTIME-FEED-V1 — realtime ws feed, bar store + consumer integration.

These verify the feed WITHOUT any network:
  • Fake Polygon AM ws frames -> LatestBarStore correct, bounded, same-minute
    bars replaced (not duplicated), out-of-order bars dropped.
  • age_seconds()/staleness gate: fresh bars served, >120s-old bars refused.
  • FLAG OFF (the default) -> every consumer path is byte-identical to today:
    helpers return []/None even with a fresh store, the tape overlay returns
    the SAME payload object, the runner store path returns [], the scanner
    quality gate still downgrades to watch-only on empty REST bars.
  • FLAG ON -> tape quotes overlay from the store, the futures runner path
    serves scaled bars ahead of Alpaca/Polygon, the scanner confirms a pick
    from store bars alone (the 09:35 case).
  • Reconnect backoff is exponential with a hard cap; auth-failure (key not
    ws-entitled — today's state) retries on the slow cadence instead.

Run: pytest backend/tests/test_realtime_feed.py -v -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone, timedelta

import pytest

import app.engines.data_feeds.realtime_feed as rt
from app.engines.data_feeds.realtime_feed import LatestBarStore, PolygonRealtimeFeed


# ── helpers ─────────────────────────────────────────────────────────────────
def _am_event(sym: str, start_ms: int, close: float, vol: float = 1000.0) -> dict:
    """One Polygon ws AM (minute aggregate) event."""
    return {
        "ev": "AM", "sym": sym,
        "o": close - 0.2, "h": close + 0.1, "l": close - 0.3, "c": close,
        "v": vol, "vw": close, "s": start_ms, "e": start_ms + 60_000,
    }


def _fresh_minute_ms(minutes_ago: int = 0) -> int:
    """Start-ms of the minute bar that ENDED `minutes_ago` minutes ago."""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now - timedelta(minutes=minutes_ago + 1)
    return int(start.timestamp() * 1000)


def _seed_fresh_bars(store: LatestBarStore, sym: str, n: int, base_price: float, vol: float = 1000.0):
    """n consecutive minute bars for `sym`, the newest ending ~now (age ~0s)."""
    for i in range(n):
        ms = _fresh_minute_ms(minutes_ago=n - 1 - i)
        store.add_bar(sym, {
            "t": ms, "e": ms + 60_000,
            "o": base_price + i * 0.1 - 0.05, "h": base_price + i * 0.1 + 0.1,
            "l": base_price + i * 0.1 - 0.1, "c": base_price + i * 0.1,
            "v": vol, "vw": base_price + i * 0.1,
        })


@pytest.fixture(autouse=True)
def _clean_slate(monkeypatch):
    """Every test starts flag-off with an empty default store + no feed."""
    monkeypatch.delenv("REALTIME_FEED", raising=False)
    rt.get_default_store().clear()
    rt._feed = None
    yield
    rt.get_default_store().clear()
    rt._feed = None


# ── 1. Fake AM messages -> store correct + bounded ──────────────────────────
def test_am_messages_fill_store_bounded():
    store = LatestBarStore(max_bars=5)
    feed = PolygonRealtimeFeed(store=store, api_key="test-key")

    t0 = _fresh_minute_ms(minutes_ago=8)
    events = [_am_event("QQQ", t0 + i * 60_000, 600.0 + i) for i in range(8)]
    ingested = feed._handle_message(json.dumps(events))
    assert ingested == 8

    bars = store.get_recent_bars("QQQ")
    assert len(bars) == 5, "deque must be bounded at max_bars"
    # Oldest 3 evicted; order oldest -> newest preserved.
    assert [b["c"] for b in bars] == [603.0, 604.0, 605.0, 606.0, 607.0]
    assert store.get_last_price("QQQ") == 607.0
    # get_recent_bars(n) slices from the newest end.
    assert [b["c"] for b in store.get_recent_bars("QQQ", 2)] == [606.0, 607.0]


def test_same_minute_bar_replaces_not_duplicates():
    store = LatestBarStore(max_bars=10)
    feed = PolygonRealtimeFeed(store=store, api_key="test-key")
    t0 = _fresh_minute_ms(minutes_ago=1)

    feed._handle_message(json.dumps([_am_event("SPY", t0, 740.00)]))
    feed._handle_message(json.dumps([_am_event("SPY", t0, 740.25)]))  # re-send, same minute
    bars = store.get_recent_bars("SPY")
    assert len(bars) == 1, "same-minute re-send must replace, not append"
    assert bars[0]["c"] == 740.25

    # Out-of-order older bar is dropped — deque stays monotonic.
    feed._handle_message(json.dumps([_am_event("SPY", t0 - 60_000, 739.0)]))
    assert len(store.get_recent_bars("SPY")) == 1
    assert store.get_last_price("SPY") == 740.25


def test_junk_frames_never_raise():
    store = LatestBarStore()
    feed = PolygonRealtimeFeed(store=store, api_key="test-key")
    assert feed._handle_message("not json at all {{{") == 0
    assert feed._handle_message(json.dumps({"ev": "AM"})) == 0          # no sym
    assert feed._handle_message(json.dumps([{"ev": "AM", "sym": "X"}])) == 0  # no t/c
    assert feed._handle_message(json.dumps(42)) == 0
    assert store.symbols() == []


# ── 2. age / staleness logic ────────────────────────────────────────────────
def test_age_seconds_and_staleness_gate(monkeypatch):
    monkeypatch.setenv("REALTIME_FEED", "polygon")
    store = rt.get_default_store()

    # Fresh bar (ended seconds ago) -> tiny age, helpers serve it.
    _seed_fresh_bars(store, "QQQ", n=3, base_price=600.0)
    age = store.age_seconds("QQQ")
    assert age is not None and age < 90.0
    assert rt.get_fresh_bars("QQQ"), "fresh store must be served"
    assert rt.get_fresh_price("QQQ") == pytest.approx(600.2)

    # Stale bar (ended 10 min ago) -> age ~600s, helpers refuse it.
    store.clear()
    ms = _fresh_minute_ms(minutes_ago=10)
    store.add_bar("QQQ", {"t": ms, "e": ms + 60_000, "o": 1, "h": 1, "l": 1, "c": 600.0, "v": 10})
    age = store.age_seconds("QQQ")
    assert age is not None and age > rt.STALE_AFTER_S
    assert rt.get_fresh_bars("QQQ") == []
    assert rt.get_fresh_price("QQQ") is None

    # Unknown symbol -> None age, empty results.
    assert store.age_seconds("NOPE") is None
    assert rt.get_fresh_bars("NOPE") == []


# ── 3. Flag OFF = byte-identical to current behavior ────────────────────────
def test_flag_off_helpers_inert_even_with_fresh_store():
    # NO REALTIME_FEED in env (fixture). Even a fresh store must not leak.
    store = rt.get_default_store()
    _seed_fresh_bars(store, "QQQ", n=3, base_price=600.0)
    assert rt.realtime_enabled() is False
    assert rt.get_fresh_bars("QQQ") == []
    assert rt.get_fresh_price("QQQ") is None
    assert rt.create_feed_from_env() is None
    assert rt.get_feed() is None
    rt.request_symbols(["QQQ"])  # must be a silent no-op


def test_flag_off_runner_store_path_returns_empty():
    from app.engines.account_signals import runner as rn
    _seed_fresh_bars(rt.get_default_store(), "SPY", n=10, base_price=740.0)
    assert rn._fetch_futures_via_store("ES", "1m", 5) == []


def test_flag_off_tape_overlay_returns_same_object():
    from app.api.routes import public_tape as tape
    _seed_fresh_bars(rt.get_default_store(), "QQQ", n=3, base_price=600.0)
    payload = {"as_of": "x", "live": True,
               "quotes": [{"symbol": "QQQ", "price": "500.00", "change_pct": 0.0}]}
    out = asyncio.run(tape._with_realtime_overlay(payload))
    assert out is payload, "flag off must return the identical payload object"


def test_flag_off_scanner_still_downgrades_unconfirmed(monkeypatch):
    from app.engines.options import theta_scanner as ts
    import app.engines.options.premarket_scheduler as pm

    async def _no_rest_bars(ticker, date_et):
        return []
    monkeypatch.setattr(pm, "_polygon_1min_bars", _no_rest_bars, raising=True)

    # Store is FRESH for the ticker but the flag is OFF -> current behavior:
    # no intraday bars => UNCONFIRMED watch-only downgrade.
    _seed_fresh_bars(rt.get_default_store(), "TSLA", n=10, base_price=100.0, vol=50_000)
    verdict, reasons = asyncio.run(
        ts._apply_quality_filters(None, {"ticker": "TSLA", "price": 100.9})
    )
    assert verdict == "watch"
    assert any("unconfirmed" in r for r in reasons)


# ── 4. Flag ON consumer integration ─────────────────────────────────────────
def test_runner_store_path_scaled_fresh_and_preferred(monkeypatch):
    from app.engines.account_signals import runner as rn
    import app.engines.data_feeds.proxy_scale as ps

    monkeypatch.setenv("REALTIME_FEED", "polygon")
    monkeypatch.setattr(ps, "get_proxy_scale", lambda inst: 10.0, raising=True)
    _seed_fresh_bars(rt.get_default_store(), "SPY", n=10, base_price=740.0)

    bars = rn._fetch_futures_via_store("ES", "1m", 5)
    assert len(bars) == 5
    assert bars[-1]["close"] == pytest.approx(740.9 * 10.0)  # SPY * scale
    age = (datetime.now(timezone.utc) - bars[-1]["timestamp"]).total_seconds()
    assert age < 300, "store bars must be fresh"

    # Warm-up guard: can't serve the FULL count yet -> [] (fall through).
    assert rn._fetch_futures_via_store("ES", "1m", 50) == []

    # Dispatch preference: the store path wins BEFORE Alpaca/Polygon run.
    called = []
    monkeypatch.setattr(rn, "_latest_real_close", lambda s: None, raising=True)
    monkeypatch.setattr(rn, "_fetch_futures_via_alpaca",
                        lambda *a, **k: called.append("alpaca") or [], raising=True)
    monkeypatch.setattr(rn, "_fetch_futures_via_polygon",
                        lambda *a, **k: called.append("polygon") or [], raising=True)
    out = rn._fetch_bars_uncached("ES", "1m", 5)
    assert len(out) == 5 and out[-1]["close"] == pytest.approx(7409.0)
    assert called == [], "realtime store must be preferred over REST proxies"


def test_tape_overlay_applies_store_prices(monkeypatch):
    from app.api.routes import public_tape as tape

    monkeypatch.setenv("REALTIME_FEED", "polygon")
    _seed_fresh_bars(rt.get_default_store(), "QQQ", n=3, base_price=599.8)  # last = 600.0
    monkeypatch.setitem(tape._prev_close, "QQQ", 500.0)

    payload = {"as_of": "x", "live": False, "quotes": [
        {"symbol": "QQQ", "price": "500.00", "change_pct": 0.0},
        {"symbol": "ES", "price": "6,000.00", "change_pct": 0.1},   # futures: never overlaid
        {"symbol": "NVDA", "price": "190.00", "change_pct": 1.0},  # not in store: untouched
    ]}
    out = asyncio.run(tape._with_realtime_overlay(payload))
    assert out is not payload, "overlay must build a NEW payload (cache never mutated)"
    q = {x["symbol"]: x for x in out["quotes"]}
    assert q["QQQ"]["price"] == "600.00"
    assert q["QQQ"]["change_pct"] == pytest.approx(20.0)
    assert q["ES"] == payload["quotes"][1]
    assert q["NVDA"] == payload["quotes"][2]
    assert out["live"] is True
    # Original payload untouched.
    assert payload["quotes"][0]["price"] == "500.00" and payload["live"] is False


def test_scanner_confirms_from_store_bars_alone(monkeypatch):
    """The 09:35 case: delayed REST has NOTHING yet, the ws store has the
    opening candles -> the pick is confirmable instead of watch-only."""
    from app.engines.options import theta_scanner as ts
    import app.engines.options.premarket_scheduler as pm

    monkeypatch.setenv("REALTIME_FEED", "polygon")

    async def _no_rest_bars(ticker, date_et):
        return []
    monkeypatch.setattr(pm, "_polygon_1min_bars", _no_rest_bars, raising=True)

    # 10 fresh liquid store bars: $-vol ~ $50M, price just above VWAP.
    _seed_fresh_bars(rt.get_default_store(), "TSLA", n=10, base_price=100.0, vol=50_000)
    verdict, reasons = asyncio.run(
        ts._apply_quality_filters(None, {"ticker": "TSLA", "price": 100.9})
    )
    assert verdict == "accept", f"expected confirmed pick, got {verdict} ({reasons})"
    assert not any("unconfirmed" in r for r in reasons)


# ── 5. Reconnect backoff caps + graceful not-authorized ─────────────────────
def test_backoff_exponential_and_capped():
    feed = PolygonRealtimeFeed(store=LatestBarStore(), api_key="test-key")
    delays = [feed._backoff_delay(i) for i in range(12)]
    assert delays[0] == 2.0
    assert delays[1] == 4.0
    assert all(b >= a for a, b in zip(delays, delays[1:])), "backoff must not shrink"
    assert max(delays) == 60.0, "backoff must hard-cap"
    assert delays[-1] == 60.0
    assert feed._backoff_delay(10_000) == 60.0  # no OverflowError blowup


def test_not_authorized_uses_slow_retry_not_crash_loop():
    feed = PolygonRealtimeFeed(store=LatestBarStore(), api_key="test-key")
    # Normal disconnects walk the exponential ladder…
    feed._attempt = 3
    assert feed._next_delay() == 16.0
    # …but an entitlement rejection (today's key state) switches to the slow
    # 15-min cadence, and never raises.
    n = feed._handle_message(json.dumps([
        {"ev": "status", "status": "auth_failed", "message": "not authorized"}
    ]))
    assert n == 0
    assert feed._auth_failed is True and feed._authed is False
    assert feed._next_delay() == 900.0
    assert feed.healthy() is False

    # auth_success resets both the flag and the backoff ladder.
    feed._handle_message(json.dumps([{"ev": "status", "status": "auth_success"}]))
    assert feed._authed is True and feed._auth_failed is False
    assert feed._attempt == 0 and feed._next_delay() == 2.0


def test_default_symbols_qqq_spy_and_runner_autosubscribes_proxies(monkeypatch):
    """REALTIME_SYMBOLS unset -> the feed boots subscribed to exactly QQQ,SPY
    (the spec default). The other futures proxies (IWM/DIA for RTY/YM) are
    picked up dynamically: the runner's store path returns [] on a miss (the
    caller falls back to REST unchanged) AND requests the subscription so the
    NEXT poll can serve seconds-fresh bars."""
    from app.engines.account_signals import runner as rn

    monkeypatch.setenv("REALTIME_FEED", "polygon")
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    monkeypatch.delenv("REALTIME_SYMBOLS", raising=False)
    feed = rt.create_feed_from_env()
    assert isinstance(feed, PolygonRealtimeFeed)
    assert feed._desired == {"QQQ", "SPY"}

    assert rn._fetch_futures_via_store("RTY", "1m", 5) == []  # store miss -> REST fallback
    assert "IWM" in feed._desired, "runner must auto-subscribe the missed proxy ETF"
    assert rn._fetch_futures_via_store("MYM", "1m", 5) == []
    assert "DIA" in feed._desired


def test_subscribe_is_threadsafe_set_add_and_flag_on_feed_creation(monkeypatch):
    monkeypatch.setenv("REALTIME_FEED", "polygon")
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    monkeypatch.setenv("REALTIME_SYMBOLS", "QQQ, spy")
    feed = rt.create_feed_from_env()
    assert isinstance(feed, PolygonRealtimeFeed)
    assert rt.get_feed() is feed
    assert feed._desired == {"QQQ", "SPY"}
    # Dynamic subscribe (scanner candidates / tape symbols): pure set-add.
    rt.request_symbols(["nvda", "NVDA", " tsla "])
    assert {"NVDA", "TSLA"} <= feed._desired
    assert feed.healthy() is False  # never connected — no sockets in tests

    # Missing key -> feed disabled, clear None (no crash).
    monkeypatch.setenv("POLYGON_API_KEY", "")
    rt._feed = None
    assert rt.create_feed_from_env() is None
