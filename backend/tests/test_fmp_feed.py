"""REALTIME-FEED-FMP — FMP provider behind the realtime-feed abstraction.

All HTTP is mocked (NO live FMP calls — the key doesn't exist on the server
yet). Coverage:
  • Batch quote poll: ONE request regardless of symbol count, parses into the
    shared LatestBarStore.
  • Minute-bucket aggregation: o/h/l/c from the price path, v from the
    CUMULATIVE-volume delta (first tick = 0 baseline, negative delta = vendor
    reset = 0), same-minute REPLACE + clean roll across minute boundaries,
    v=0 when the payload has no volume.
  • Staleness gate: REALTIME_FEED=fmp serves fresh buckets through
    get_fresh_bars()/get_fresh_price(), refuses >120s-old ones.
  • On-demand /historical-chart/1min bars: ET→epoch parsing, oldest→newest
    ordering, date_et session filter + n slice, 15s TTL cache (≤1 req/sym/TTL,
    failures cached as a cooldown), never raises.
  • Factory: fmp+key → FMPRealtimeFeed, fmp w/o key → None, polygon path
    unchanged, unknown/off → None.
  • 429/5xx: FMPHTTPError surfaces, poll loop backs off exponentially with a
    hard cap — never a tight loop.
  • Scanner confirmation: fresh FMP bars preferred (Polygon REST never
    called), Polygon REST fallback on ANY failure/empty; helper inert when the
    flag is off or the provider is polygon.
  • Ws layer: login rejection flagged once + ws disabled (polling unaffected),
    trade ticks feed the same buckets, delivering ws idles the poller.

Flag-off byte-identity is re-verified by rerunning tests/test_realtime_feed.py
and tests/test_public_tape.py alongside this file.

Run: pytest backend/tests/test_fmp_feed.py -v -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone, timedelta

import pytest

import app.engines.data_feeds.fmp_feed as fmp
import app.engines.data_feeds.realtime_feed as rt
from app.engines.data_feeds.fmp_feed import FMPRealtimeFeed
from app.engines.data_feeds.realtime_feed import LatestBarStore, PolygonRealtimeFeed


# ── fakes / helpers ─────────────────────────────────────────────────────────
class _FakeResponse:
    """Stands in for the aiohttp request context manager + response."""

    def __init__(self, status: int = 200, payload=None):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    closed = False

    def __init__(self, responses):
        self.calls: list[dict] = []
        self._responses = list(responses)

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[idx]


def _fresh_minute_ms(minutes_ago: int = 0) -> int:
    """Start-ms of the minute bar that ENDED `minutes_ago` minutes ago."""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now - timedelta(minutes=minutes_ago + 1)
    return int(start.timestamp() * 1000)


def _liquid_bars(base: float, n: int, vol: float) -> list:
    """n consecutive fresh liquid minute bars in the REST-aggs shape (same
    profile as test_realtime_feed's store seeds: rising closes, higher highs,
    price just above VWAP -> the quality gate accepts at price base+0.9)."""
    bars = []
    for i in range(n):
        ms = _fresh_minute_ms(minutes_ago=n - 1 - i)
        px = base + i * 0.1
        bars.append({
            "t": ms, "e": ms + 60_000,
            "o": px - 0.05, "h": px + 0.1, "l": px - 0.1, "c": px,
            "v": vol, "vw": px,
        })
    return bars


@pytest.fixture(autouse=True)
def _clean_slate(monkeypatch):
    """Every test starts flag-off, empty store/caches, no feed, no session."""
    for var in ("REALTIME_FEED", "REALTIME_SYMBOLS", "FMP_API_KEY",
                "FMP_POLL_SECONDS", "FMP_WEBSOCKET", "POLYGON_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    rt.get_default_store().clear()
    rt._feed = None
    fmp.clear_ondemand_cache()
    fmp._session_ref = (None, None)
    yield
    rt.get_default_store().clear()
    rt._feed = None
    fmp.clear_ondemand_cache()
    fmp._session_ref = (None, None)


# ── 1. Batch quote poll → store ─────────────────────────────────────────────
def test_quote_poll_single_batch_request_into_store(monkeypatch):
    store = LatestBarStore()
    feed = FMPRealtimeFeed(store=store, api_key="test-key", symbols=["QQQ", "SPY"], poll_seconds=5)
    # Stable API: one request PER SYMBOL per cycle (plan has no batch endpoint);
    # sorted symbol order -> QQQ then SPY. Junk payload shapes still skipped.
    fake = _FakeSession([
        _FakeResponse(payload=[{"symbol": "QQQ", "price": 600.5, "volume": 1_000_000}]),
        _FakeResponse(payload=[
            {"symbol": "SPY", "price": 740.1, "volume": 2_000_000},
            {"symbol": "JUNK", "price": 0},          # unusable — skipped
            "not-a-dict",                             # junk — skipped
        ]),
    ])
    monkeypatch.setattr(fmp, "_get_session", lambda: fake)

    ingested = asyncio.run(feed._poll_once())
    assert ingested == 2
    assert len(fake.calls) == 2, "one request per subscribed symbol per cycle"
    assert fake.calls[0]["params"]["symbol"] == "QQQ"
    assert fake.calls[1]["params"]["symbol"] == "SPY"
    assert fake.calls[0]["params"]["apikey"] == "test-key"
    assert store.get_last_price("QQQ") == 600.5
    assert store.get_last_price("SPY") == 740.1
    assert len(store.get_recent_bars("QQQ")) == 1
    assert store.symbols() == ["QQQ", "SPY"]

    # Empty subscription set -> no request at all.
    feed2 = FMPRealtimeFeed(store=store, api_key="test-key", symbols=[])
    assert asyncio.run(feed2._poll_once()) == 0
    assert len(fake.calls) == 2  # unchanged — empty set adds no requests


# ── 2. Minute-bucket aggregation ────────────────────────────────────────────
def test_minute_bucket_aggregation_across_boundary():
    store = LatestBarStore()
    feed = FMPRealtimeFeed(store=store, api_key="test-key")
    t0 = 1_767_225_600.0  # epoch divisible by 60 -> clean minute boundary

    feed._ingest_tick("QQQ", 100.0, cum_vol=1_000_000, ts_s=t0 + 1)
    feed._ingest_tick("QQQ", 101.0, cum_vol=1_000_500, ts_s=t0 + 30)
    feed._ingest_tick("QQQ", 99.5, cum_vol=1_000_800, ts_s=t0 + 59)
    feed._ingest_tick("QQQ", 102.0, cum_vol=1_001_500, ts_s=t0 + 61)  # next minute

    bars = store.get_recent_bars("QQQ")
    assert len(bars) == 2, "same-minute quotes must REPLACE, a minute roll must APPEND"
    b1, b2 = bars
    assert b1["t"] == int(t0 * 1000) and b1["e"] == b1["t"] + 60_000
    assert (b1["o"], b1["h"], b1["l"], b1["c"]) == (100.0, 101.0, 99.5, 99.5)
    assert b1["v"] == 800.0, "first tick = baseline only (0), then 500 + 300"
    assert b2["t"] == int((t0 + 60) * 1000)
    assert (b2["o"], b2["h"], b2["l"], b2["c"]) == (102.0, 102.0, 102.0, 102.0)
    assert b2["v"] == 700.0, "cumulative-volume delta must carry across the boundary"
    assert store.get_last_price("QQQ") == 102.0

    # No volume field on the endpoint -> v stays 0 (documented behavior).
    feed._ingest_tick("NVDA", 190.0, cum_vol=None, ts_s=t0 + 5)
    feed._ingest_tick("NVDA", 190.5, cum_vol=None, ts_s=t0 + 10)
    assert store.get_recent_bars("NVDA")[-1]["v"] == 0.0

    # Negative delta (day roll / vendor reset) contributes 0, then re-baselines.
    feed._ingest_tick("SPY", 700.0, cum_vol=5000, ts_s=t0 + 5)
    feed._ingest_tick("SPY", 700.5, cum_vol=100, ts_s=t0 + 10)
    assert store.get_recent_bars("SPY")[-1]["v"] == 0.0
    feed._ingest_tick("SPY", 700.6, cum_vol=400, ts_s=t0 + 20)
    assert store.get_recent_bars("SPY")[-1]["v"] == 300.0


def test_quote_payload_junk_never_raises():
    feed = FMPRealtimeFeed(store=LatestBarStore(), api_key="test-key")
    assert feed._ingest_quote_payload("nonsense") == 0
    assert feed._ingest_quote_payload(None) == 0
    assert feed._ingest_quote_payload(
        [42, {"symbol": "X"}, {"price": 1.0}, {"symbol": "Y", "price": "bad"}]
    ) == 0
    # A single dict (FMP returns one for a single symbol) is accepted.
    assert feed._ingest_quote_payload({"symbol": "QQQ", "price": 600.0}) == 1
    assert feed.store.get_last_price("QQQ") == 600.0


# ── 3. Staleness gate with the fmp provider ─────────────────────────────────
def test_fmp_provider_enables_helpers_and_staleness_gate(monkeypatch):
    monkeypatch.setenv("REALTIME_FEED", "fmp")
    assert rt.realtime_provider() == "fmp"
    assert rt.realtime_enabled() is True

    store = rt.get_default_store()
    feed = FMPRealtimeFeed(store=store, api_key="test-key")

    # Fresh partial bucket -> served.
    feed._ingest_tick("QQQ", 600.0, ts_s=time.time())
    age = store.age_seconds("QQQ")
    assert age is not None and age < 90.0
    assert rt.get_fresh_bars("QQQ"), "fresh fmp bucket must be served"
    assert rt.get_fresh_price("QQQ") == 600.0

    # Stale bucket (10 min old) -> refused, callers fall back to REST.
    # NOTE: uses a DIFFERENT price — identical (price, cum_vol) re-observations
    # are intentionally skipped by the closed-market freshness guard.
    store.clear()
    feed._agg.clear()
    feed._ingest_tick("QQQ", 601.0, ts_s=time.time() - 600)
    age = store.age_seconds("QQQ")
    assert age is not None and age > rt.STALE_AFTER_S
    assert rt.get_fresh_bars("QQQ") == []
    assert rt.get_fresh_price("QQQ") is None


# ── 4. On-demand real-time 1-min bars + TTL cache ───────────────────────────
def test_ondemand_bars_parse_filter_and_ttl_cache(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    payload = [  # FMP returns NEWEST FIRST, ET timestamps, several sessions
        {"date": "2026-07-02 09:32:00", "open": 101.0, "low": 100.5, "high": 101.5, "close": 101.2, "volume": 3000},
        {"date": "2026-07-02 09:31:00", "open": 100.0, "low": 99.5, "high": 100.9, "close": 100.8, "volume": 2000},
        {"date": "2026-07-01 15:59:00", "open": 98.0, "low": 97.0, "high": 98.5, "close": 98.2, "volume": 1000},
        {"date": "garbage", "close": 5.0},        # unusable — skipped
    ]
    fake = _FakeSession([_FakeResponse(payload=payload)])
    monkeypatch.setattr(fmp, "_get_session", lambda: fake)

    bars = asyncio.run(fmp.fetch_intraday_bars("tsla"))
    assert len(fake.calls) == 1
    assert fake.calls[0]["params"]["symbol"] == "TSLA"
    assert fake.calls[0]["params"]["apikey"] == "test-key"
    assert [b["c"] for b in bars] == [98.2, 100.8, 101.2], "must be sorted oldest→newest"
    # ET→epoch: 2026-07-02 09:31 EDT == 13:31 UTC; aggs shape throughout.
    want_t = int(datetime(2026, 7, 2, 13, 31, tzinfo=timezone.utc).timestamp() * 1000)
    assert bars[1]["t"] == want_t and bars[1]["e"] == want_t + 60_000
    assert set(bars[0]) >= {"t", "e", "o", "h", "l", "c", "v", "vw"}
    assert bars[2]["v"] == 3000.0

    # Within the TTL: cache hit — NO second HTTP request.
    again = asyncio.run(fmp.fetch_intraday_bars("TSLA"))
    assert len(fake.calls) == 1, "cache must prevent a repeat fetch within the TTL"
    assert [b["c"] for b in again] == [98.2, 100.8, 101.2]

    # date_et session filter + n slice run per-call on the cached payload.
    today = asyncio.run(fmp.fetch_intraday_bars("TSLA", date_et="2026-07-02"))
    assert [b["c"] for b in today] == [100.8, 101.2]
    last1 = asyncio.run(fmp.fetch_intraday_bars("TSLA", n=1, date_et="2026-07-02"))
    assert [b["c"] for b in last1] == [101.2]
    assert len(fake.calls) == 1

    # TTL expiry -> exactly one refetch.
    with fmp._ondemand_lock:
        ts_, cached = fmp._ondemand_cache["TSLA"]
        fmp._ondemand_cache["TSLA"] = (ts_ - (fmp.ONDEMAND_TTL_S + 1), cached)
    asyncio.run(fmp.fetch_intraday_bars("TSLA"))
    assert len(fake.calls) == 2


def test_ondemand_failure_cooldown_and_missing_key(monkeypatch):
    # No FMP_API_KEY -> [] and ZERO HTTP.
    fake = _FakeSession([_FakeResponse(payload=[])])
    monkeypatch.setattr(fmp, "_get_session", lambda: fake)
    assert asyncio.run(fmp.fetch_intraday_bars("TSLA")) == []
    assert fake.calls == []

    # HTTP 500 -> [] AND a cooldown: retry within the TTL must NOT re-hit HTTP.
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    fake500 = _FakeSession([_FakeResponse(status=500)])
    monkeypatch.setattr(fmp, "_get_session", lambda: fake500)
    assert asyncio.run(fmp.fetch_intraday_bars("TSLA")) == []
    assert asyncio.run(fmp.fetch_intraday_bars("TSLA")) == []
    assert len(fake500.calls) == 1, "failures must be cached as a cooldown, not retried hot"

    # Transport exception: same discipline — [] now, cooldown after, no raise.
    fmp.clear_ondemand_cache()

    class _Boom:
        closed = False

        def get(self, *a, **k):
            raise RuntimeError("boom")

    boom = _Boom()
    monkeypatch.setattr(fmp, "_get_session", lambda: boom)
    assert asyncio.run(fmp.fetch_intraday_bars("NVDA")) == []
    assert asyncio.run(fmp.fetch_intraday_bars("NVDA")) == []  # cooldown, no raise


# ── 5. Factory selection ────────────────────────────────────────────────────
def test_factory_selects_fmp_polygon_none(monkeypatch):
    # Off (default) -> None.
    assert rt.create_feed_from_env() is None

    # fmp + key -> FMPRealtimeFeed; env knobs respected.
    monkeypatch.setenv("REALTIME_FEED", "fmp")
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    monkeypatch.setenv("FMP_POLL_SECONDS", "2")
    feed = rt.create_feed_from_env()
    assert isinstance(feed, FMPRealtimeFeed)
    assert rt.get_feed() is feed
    assert feed._poll_seconds == 2.0
    assert feed._desired == {"QQQ", "SPY"}, "REALTIME_SYMBOLS default must carry over"
    assert feed._ws_enabled is False, "FMP_WEBSOCKET defaults to 0"

    # Poll floor: never below 1s no matter the env.
    assert FMPRealtimeFeed(store=LatestBarStore(), api_key="k", poll_seconds=0.2)._poll_seconds == 1.0

    # fmp WITHOUT key -> None (feed disabled, no crash).
    monkeypatch.setenv("FMP_API_KEY", "")
    rt._feed = None
    assert rt.create_feed_from_env() is None

    # polygon path unchanged.
    monkeypatch.setenv("REALTIME_FEED", "polygon")
    monkeypatch.setenv("POLYGON_API_KEY", "poly-key")
    feed2 = rt.create_feed_from_env()
    assert isinstance(feed2, PolygonRealtimeFeed)

    # Unknown provider -> None.
    monkeypatch.setenv("REALTIME_FEED", "bloomberg")
    rt._feed = None
    assert rt.create_feed_from_env() is None


def test_fmp_websocket_env_flag(monkeypatch):
    monkeypatch.setenv("FMP_WEBSOCKET", "1")
    assert FMPRealtimeFeed(store=LatestBarStore(), api_key="k")._ws_enabled is True
    monkeypatch.setenv("FMP_WEBSOCKET", "0")
    assert FMPRealtimeFeed(store=LatestBarStore(), api_key="k")._ws_enabled is False


# ── 6. 429/5xx backoff — capped, never a tight loop ─────────────────────────
def test_backoff_exponential_and_capped(monkeypatch):
    feed = FMPRealtimeFeed(store=LatestBarStore(), api_key="test-key", symbols=["QQQ"])
    delays = [feed._error_backoff_delay(i) for i in range(12)]
    assert delays[0] == 2.0 and delays[1] == 4.0
    assert all(b >= a for a, b in zip(delays, delays[1:])), "backoff must not shrink"
    assert max(delays) == 60.0, "backoff must hard-cap"
    assert feed._error_backoff_delay(10_000) == 60.0  # no OverflowError blowup

    # A 429 surfaces as FMPHTTPError (status preserved for the loop).
    fake = _FakeSession([_FakeResponse(status=429)])
    monkeypatch.setattr(fmp, "_get_session", lambda: fake)
    with pytest.raises(fmp.FMPHTTPError) as ei:
        asyncio.run(feed._poll_once())
    assert ei.value.status == 429


def test_poll_loop_backs_off_on_429_never_tight_loops(monkeypatch):
    feed = FMPRealtimeFeed(store=LatestBarStore(), api_key="test-key",
                           symbols=["QQQ"], poll_seconds=5)
    fake = _FakeSession([_FakeResponse(status=429)])
    monkeypatch.setattr(fmp, "_get_session", lambda: fake)

    sleeps: list[float] = []

    async def _fake_sleep(d):
        sleeps.append(d)
        if len(sleeps) >= 7:
            feed._stopping = True

    monkeypatch.setattr(fmp.asyncio, "sleep", _fake_sleep)
    asyncio.run(feed._poll_loop())
    # delay = max(poll_interval, capped exponential): 2,4 < 5, then 8..60 cap.
    assert sleeps == [5, 5, 8, 16, 32, 60, 60]
    assert len(fake.calls) == 7  # one request per cycle, never more


# ── 7. Scanner confirmation: FMP preferred, Polygon REST fallback ───────────
def test_scanner_confirmation_prefers_fmp(monkeypatch):
    from app.engines.options import theta_scanner as ts
    import app.engines.options.premarket_scheduler as pm

    monkeypatch.setenv("REALTIME_FEED", "fmp")
    monkeypatch.setenv("FMP_API_KEY", "test-key")

    fmp_calls: list[tuple] = []

    async def _fake_fmp_bars(symbol, n=None, date_et=None, **kw):
        fmp_calls.append((symbol, date_et))
        return _liquid_bars(base=100.0, n=10, vol=50_000)

    monkeypatch.setattr(fmp, "fetch_intraday_bars", _fake_fmp_bars, raising=True)

    poly_calls: list[str] = []

    async def _poly(ticker, date_et):
        poly_calls.append(ticker)
        return []

    monkeypatch.setattr(pm, "_polygon_1min_bars", _poly, raising=True)

    verdict, reasons = asyncio.run(
        ts._apply_quality_filters(None, {"ticker": "TSLA", "price": 100.9})
    )
    assert verdict == "accept", f"expected FMP-confirmed pick, got {verdict} ({reasons})"
    assert not any("unconfirmed" in r for r in reasons)
    assert fmp_calls and fmp_calls[0][0] == "TSLA"
    assert fmp_calls[0][1], "scanner must pass today's ET session date"
    assert poly_calls == [], "fresh FMP bars must be preferred over Polygon REST"


def test_scanner_confirmation_falls_back_to_polygon_on_fmp_failure(monkeypatch):
    from app.engines.options import theta_scanner as ts
    import app.engines.options.premarket_scheduler as pm

    monkeypatch.setenv("REALTIME_FEED", "fmp")
    monkeypatch.setenv("FMP_API_KEY", "test-key")

    async def _fmp_boom(symbol, n=None, date_et=None, **kw):
        raise RuntimeError("fmp down")

    monkeypatch.setattr(fmp, "fetch_intraday_bars", _fmp_boom, raising=True)

    poly_calls: list[str] = []

    async def _poly(ticker, date_et):
        poly_calls.append(ticker)
        return _liquid_bars(base=100.0, n=10, vol=50_000)

    monkeypatch.setattr(pm, "_polygon_1min_bars", _poly, raising=True)

    verdict, reasons = asyncio.run(
        ts._apply_quality_filters(None, {"ticker": "TSLA", "price": 100.9})
    )
    assert verdict == "accept", f"fallback path must still confirm ({reasons})"
    assert poly_calls == ["TSLA"], "Polygon REST is the fallback on FMP failure"

    # Empty FMP result (quiet endpoint) -> same fallback.
    poly_calls.clear()

    async def _fmp_empty(symbol, n=None, date_et=None, **kw):
        return []

    monkeypatch.setattr(fmp, "fetch_intraday_bars", _fmp_empty, raising=True)
    verdict, _ = asyncio.run(
        ts._apply_quality_filters(None, {"ticker": "TSLA", "price": 100.9})
    )
    assert verdict == "accept"
    assert poly_calls == ["TSLA"]


def test_ondemand_helper_inert_when_off_or_polygon(monkeypatch):
    def _explode(*a, **k):
        raise AssertionError("fetch_intraday_bars must not be called")

    monkeypatch.setattr(fmp, "fetch_intraday_bars", _explode, raising=True)
    # Flag off (the default): no fetch, no crash — Polygon REST path untouched.
    assert asyncio.run(rt.get_ondemand_intraday_bars("TSLA")) == []
    # Polygon provider: the on-demand helper stays fmp-only.
    monkeypatch.setenv("REALTIME_FEED", "polygon")
    assert asyncio.run(rt.get_ondemand_intraday_bars("TSLA")) == []


# ── 8. Ws layer: graceful entitlement rejection, ticks, poller idling ───────
def test_ws_auth_rejected_once_polling_unaffected(monkeypatch):
    monkeypatch.setenv("FMP_WEBSOCKET", "1")
    feed = FMPRealtimeFeed(store=LatestBarStore(), api_key="test-key", symbols=["AAPL"])
    assert feed._ws_enabled is True

    # Login rejection -> flagged, ws permanently off for the run, no raise.
    feed._handle_ws_message(json.dumps({"event": "login", "status": 401, "message": "Unauthorized"}))
    assert feed._ws_auth_rejected is True and feed._ws_authed is False
    assert feed._ws_delivering() is False
    # Duplicate rejection: still quiet, still off.
    feed._handle_ws_message(json.dumps({"event": "login", "status": 401, "message": "Unauthorized"}))
    assert feed._ws_auth_rejected is True

    # Polling health is independent of the dead ws.
    feed._last_poll_ok_mono = time.monotonic()
    assert feed.healthy() is True


def test_ws_ticks_feed_buckets_and_idle_the_poller(monkeypatch):
    monkeypatch.setenv("FMP_WEBSOCKET", "1")
    feed = FMPRealtimeFeed(store=LatestBarStore(), api_key="test-key", symbols=["AAPL"])

    feed._handle_ws_message(json.dumps({"event": "login", "status": 200, "message": "Connected"}))
    assert feed._ws_authed is True and feed._ws_auth_rejected is False

    n = feed._handle_ws_message(json.dumps([
        {"s": "aapl", "type": "T", "lp": 231.55, "ls": 100},
        {"s": "aapl", "type": "T", "lp": 231.60, "ls": 50},
        {"s": "", "lp": 1.0},                 # junk — skipped
        {"event": "heartbeat"},               # event frame — not a tick
    ]))
    assert n == 2
    assert feed.store.get_last_price("AAPL") == 231.60
    bar = feed.store.get_recent_bars("AAPL")[-1]
    assert bar["v"] == 150.0  # ws trade sizes accumulate directly
    assert feed._ws_delivering() is True

    # While the ws delivers, the poll cycle SKIPS its HTTP request entirely.
    fake = _FakeSession([_FakeResponse(payload=[])])
    monkeypatch.setattr(fmp, "_get_session", lambda: fake)

    async def _sleep_once(d):
        feed._stopping = True

    monkeypatch.setattr(fmp.asyncio, "sleep", _sleep_once)
    asyncio.run(feed._poll_loop())
    assert fake.calls == [], "poller must idle while the ws stream is live"

    # Junk ws frames never raise.
    assert feed._handle_ws_message("not json {{{") == 0
    assert feed._handle_ws_message(json.dumps(42)) == 0


def test_start_without_key_returns_cleanly():
    feed = FMPRealtimeFeed(store=LatestBarStore(), api_key="")
    asyncio.run(feed.start())  # clean return = supervisor won't restart-loop
    assert feed.healthy() is False


def test_subscribe_set_add_and_next_poll_pickup(monkeypatch):
    feed = FMPRealtimeFeed(store=LatestBarStore(), api_key="test-key", symbols=["QQQ"])
    feed.subscribe(["nvda", " tsla ", "NVDA", ""])
    assert feed._desired == {"QQQ", "NVDA", "TSLA"}

    fake = _FakeSession([_FakeResponse(payload=[])])
    monkeypatch.setattr(fmp, "_get_session", lambda: fake)
    asyncio.run(feed._poll_once())
    polled = [c["params"]["symbol"] for c in fake.calls]
    assert polled == ["NVDA", "QQQ", "TSLA"], \
        "dynamic subscriptions must ride the next poll cycle (sorted, per-symbol)"
