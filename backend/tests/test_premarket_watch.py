"""Saro Premarket Watch — the 08:45 ET watchlist email + Track B pre-lock.

Fully mocked (no live FMP, no real Postgres, no real Redis, NO REAL EMAILS —
_send_email is monkeypatched everywhere). Coverage:
  • Scheduler hook: once-per-day Redis SETNX latch idempotency, ET-anchored
    date keys (00:xx UTC is still the PREVIOUS ET day), 08:45-09:25 ET window,
    weekend + full-holiday skip, env-off short-circuit
    (PREMARKET_WATCH_ENABLED default on), redis-down skip, run-failure
    swallowed with the latch KEPT (no duplicate blast).
  • Filter doctrine: sub-$5 and known-thin names excluded, |gap| < 3%
    excluded, unknown dollar volume passes, gap-downs kept.
  • Rank order: |gap| x catalyst weight, descending.
  • Track B pre-lock: theta:ignition:candidates:{ET-date} JSON shape is
    exactly {ticker, prev_close, premarket_price, gap_pct, catalyst}.
  • End-to-end run: quote budget respected, email top-5 only, subscribers
    each get one send, empty morning writes the pre-lock but sends nothing.
  • Wiring regression guard: the hook IS called from the scheduler loop.

Run: pytest backend/tests/test_premarket_watch.py -v -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

import app.engines.options.premarket_watch as pw


# ── FakeRedis (test_fmp_eod_snapshot pattern) ────────────────────────────────
class _FakeRedis:
    def __init__(self, *, raise_on_set=False):
        self.keys: dict = {}
        self.raise_on_set = raise_on_set

    def set(self, key, value, ex=None, nx=None):
        if self.raise_on_set:
            raise ConnectionError("redis down")
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    def delete(self, key):
        self.keys.pop(key, None)


# ── pure helpers: gap ────────────────────────────────────────────────────────
def test_gap_pct_math_and_never_fabricated():
    assert pw._gap_pct(100.0, 105.0) == 5.0
    assert pw._gap_pct(100.0, 96.5) == -3.5
    assert pw._gap_pct(0, 105.0) is None      # no prev close -> no gap
    assert pw._gap_pct(100.0, None) is None   # no live price -> no gap
    assert pw._gap_pct("junk", 105.0) is None


# ── filter doctrine ──────────────────────────────────────────────────────────
def test_filters_exclude_sub_5_dollar_names():
    assert pw._passes_filters(4.99, 25.0, 50_000_000) is False
    assert pw._passes_filters(5.00, 25.0, 50_000_000) is True


def test_filters_exclude_known_thin_names_but_pass_unknown_volume():
    # KNOWN thin ($5M prev-session dollar volume) -> rejected
    assert pw._passes_filters(20.0, 8.0, 5_000_000) is False
    # UNKNOWN (0/None — snapshot stores below-sweep movers with volume 0)
    # -> passes; a missing denominator is never fabricated into a reject
    assert pw._passes_filters(20.0, 8.0, 0) is True
    assert pw._passes_filters(20.0, 8.0, None) is True
    assert pw._passes_filters(20.0, 8.0, 20_000_000) is True


def test_filters_require_3pct_gap_either_direction():
    assert pw._passes_filters(20.0, 2.9, None) is False
    assert pw._passes_filters(20.0, 3.0, None) is True
    assert pw._passes_filters(20.0, -3.4, None) is True   # gap-downs kept
    assert pw._passes_filters(20.0, None, None) is False


# ── rank order ───────────────────────────────────────────────────────────────
def test_rank_is_abs_gap_times_catalyst_weight():
    a = {"ticker": "AAA", "gap_pct": 10.0, "catalyst_weight": 1.0}   # 10.0
    b = {"ticker": "BBB", "gap_pct": 6.0, "catalyst_weight": 2.0}    # 12.0
    c = {"ticker": "CCC", "gap_pct": -11.0, "catalyst_weight": 1.0}  # 11.0
    d = {"ticker": "DDD", "gap_pct": 4.0}                            # 4.0 (w=1)
    ranked = pw._rank_candidates([a, b, c, d])
    assert [r["ticker"] for r in ranked] == ["BBB", "CCC", "AAA", "DDD"]


# ── Track B pre-lock JSON shape ──────────────────────────────────────────────
def test_candidate_json_shape_is_exactly_the_five_locked_keys():
    rows = [{"ticker": "AAA", "prev_close": 10.0, "premarket_price": 10.5,
             "gap_pct": 5.0, "catalyst": "8-K item 1.01",
             "catalyst_weight": 2.0, "extra_junk": "must not leak"}]
    payload = json.loads(pw._candidate_json(rows))
    assert payload == [{"ticker": "AAA", "prev_close": 10.0,
                        "premarket_price": 10.5, "gap_pct": 5.0,
                        "catalyst": "8-K item 1.01"}]
    assert json.loads(pw._candidate_json([])) == []


# ── ET-date keying: 00:xx UTC is still the PREVIOUS ET day ───────────────────
def test_et_date_at_00xx_utc_is_previous_day(monkeypatch):
    monkeypatch.setattr(pw, "_now_utc",
                        lambda: datetime(2026, 7, 15, 0, 30, tzinfo=timezone.utc))
    et = pw._now_et()
    assert et.strftime("%Y-%m-%d") == "2026-07-14"   # 20:30 ET on the 14th
    assert pw._latch_key(et.strftime("%Y-%m-%d")) == "theta:premarket_watch:2026-07-14"
    assert pw._ignition_key(et.strftime("%Y-%m-%d")) == "theta:ignition:candidates:2026-07-14"


def test_hook_at_00xx_utc_does_not_fire_and_never_burns_next_days_latch(monkeypatch):
    """00:30 UTC = 20:30 ET previous day: outside the window -> no run, and
    critically NO latch write keyed to the (wrong) UTC date."""
    r = _FakeRedis()
    ran = []
    monkeypatch.setattr(pw, "_now_utc",
                        lambda: datetime(2026, 7, 15, 0, 30, tzinfo=timezone.utc))
    monkeypatch.setattr(pw, "_get_redis", lambda: r)

    async def fake_run(d):
        ran.append(d)
        return 1

    monkeypatch.setattr(pw, "run_premarket_watch", fake_run)
    asyncio.run(pw._check_and_run_premarket_watch())
    assert ran == []
    assert r.keys == {}


# ── the scheduler hook ───────────────────────────────────────────────────────
def _wire_hook(monkeypatch, *, et=datetime(2026, 7, 14, 8, 50), redis=None):
    """Tuesday 08:50 ET by default — inside the fire window."""
    ran = []

    async def fake_run(today_et):
        ran.append(today_et)
        return 1

    monkeypatch.setattr(pw, "_now_et", lambda: et)
    monkeypatch.setattr(pw, "_get_redis",
                        lambda: redis if redis is not None else _FakeRedis())
    monkeypatch.setattr(pw, "run_premarket_watch", fake_run)
    return ran


def test_hook_fires_once_per_day_via_setnx_latch(monkeypatch):
    r = _FakeRedis()
    ran = _wire_hook(monkeypatch, redis=r)

    async def run():
        await pw._check_and_run_premarket_watch()
        await pw._check_and_run_premarket_watch()  # same day -> latch blocks

    asyncio.run(run())
    assert ran == ["2026-07-14"]
    assert list(r.keys) == [f"{pw.REDIS_LATCH_PREFIX}2026-07-14"]


def test_hook_time_weekday_and_holiday_gates(monkeypatch):
    # 08:44 ET — one minute early
    ran = _wire_hook(monkeypatch, et=datetime(2026, 7, 14, 8, 44))
    asyncio.run(pw._check_and_run_premarket_watch())
    assert ran == []
    # 09:26 ET — stale, the 9:33 scanner owns the tape now
    ran = _wire_hook(monkeypatch, et=datetime(2026, 7, 14, 9, 26))
    asyncio.run(pw._check_and_run_premarket_watch())
    assert ran == []
    # Saturday — never
    ran = _wire_hook(monkeypatch, et=datetime(2026, 7, 18, 8, 50))
    asyncio.run(pw._check_and_run_premarket_watch())
    assert ran == []
    # Full-day market holiday (2026-07-03 observed Independence Day, a Friday)
    ran = _wire_hook(monkeypatch, et=datetime(2026, 7, 3, 8, 50))
    asyncio.run(pw._check_and_run_premarket_watch())
    assert ran == []
    # 08:45 sharp on a trading Tuesday — fires
    ran = _wire_hook(monkeypatch, et=datetime(2026, 7, 14, 8, 45))
    asyncio.run(pw._check_and_run_premarket_watch())
    assert ran == ["2026-07-14"]


def test_hook_env_gate_default_on(monkeypatch):
    ran = _wire_hook(monkeypatch)
    monkeypatch.setenv("PREMARKET_WATCH_ENABLED", "0")
    asyncio.run(pw._check_and_run_premarket_watch())
    assert ran == []
    monkeypatch.delenv("PREMARKET_WATCH_ENABLED", raising=False)  # default = on
    asyncio.run(pw._check_and_run_premarket_watch())
    assert ran == ["2026-07-14"]


def test_hook_env_off_touches_nothing(monkeypatch):
    """Env-off short-circuits BEFORE redis — no latch, no keys, no run."""
    r = _FakeRedis()
    ran = _wire_hook(monkeypatch, redis=r)
    monkeypatch.setenv("PREMARKET_WATCH_ENABLED", "0")
    asyncio.run(pw._check_and_run_premarket_watch())
    assert ran == [] and r.keys == {}


def test_hook_skips_without_redis_latch(monkeypatch):
    """No redis -> skip (never risk a duplicate morning blast)."""
    ran = _wire_hook(monkeypatch, redis=_FakeRedis(raise_on_set=True))
    asyncio.run(pw._check_and_run_premarket_watch())
    assert ran == []


def test_hook_swallows_run_failure_and_keeps_latch(monkeypatch):
    """A failed run must not raise AND must keep the latch: a partial send
    may already be in inboxes — duplicates are worse than a missed morning."""
    r = _FakeRedis()
    monkeypatch.setattr(pw, "_now_et", lambda: datetime(2026, 7, 14, 8, 50))
    monkeypatch.setattr(pw, "_get_redis", lambda: r)

    async def boom(today_et):
        raise RuntimeError("run exploded")

    monkeypatch.setattr(pw, "run_premarket_watch", boom)
    asyncio.run(pw._check_and_run_premarket_watch())  # must NOT raise
    assert list(r.keys) == [f"{pw.REDIS_LATCH_PREFIX}2026-07-14"]


# ── end-to-end run (all sources mocked) ──────────────────────────────────────
def _wire_run(monkeypatch, *, quotes, prev_map, edgar=None, news=None,
              movers=None, redis=None, subscribers=("ryan@example.com",)):
    r = redis if redis is not None else _FakeRedis()
    sent = []

    async def fake_edgar():
        return dict(edgar or {})

    async def fake_news():
        return dict(news or {})

    async def fake_movers():
        return list(movers or [])

    async def fake_quote(sym):
        return quotes.get(sym)

    async def fake_prev_map(today):
        return dict(prev_map or {})

    async def fake_fallback(sym):
        return None

    async def fake_pace():
        return None

    async def fake_subs():
        return list(subscribers)

    monkeypatch.setattr(pw, "_edgar_catalysts", fake_edgar)
    monkeypatch.setattr(pw, "_fetch_premarket_news", fake_news)
    monkeypatch.setattr(pw, "_fetch_movers_symbols", fake_movers)
    monkeypatch.setattr(pw, "_fetch_quote", fake_quote)
    monkeypatch.setattr(pw, "_load_prev_map", fake_prev_map)
    monkeypatch.setattr(pw, "_fallback_prev_close", fake_fallback)
    monkeypatch.setattr(pw, "_pace", fake_pace)
    monkeypatch.setattr(pw, "_subscriber_emails", fake_subs)
    monkeypatch.setattr(pw, "_get_redis", lambda: r)
    monkeypatch.setattr(pw, "_send_email",
                        lambda to, subject, html: sent.append((to, subject, html)) or True)
    return r, sent


def test_run_filters_ranks_prelocks_and_emails(monkeypatch):
    # GOOD gap+catalyst, BIGG bigger raw gap no catalyst, PENY sub-$5,
    # THIN known-thin tape, FLAT gap under 3%
    quotes = {"GOOD": 21.0, "BIGG": 55.0, "PENY": 4.5, "THIN": 30.0, "FLAT": 101.0}
    prev_map = {
        "GOOD": {"c": 20.0, "v": 2_000_000},   # +5.0% gap, $40M prev $vol
        "BIGG": {"c": 50.0, "v": 1_000_000},   # +10.0% gap, $50M
        "PENY": {"c": 4.0, "v": 50_000_000},   # +12.5% but sub-$5 -> OUT
        "THIN": {"c": 25.0, "v": 100_000},     # +20% but $2.5M prev $vol -> OUT
        "FLAT": {"c": 100.0, "v": 1_000_000},  # +1.0% -> OUT
    }
    r, sent = _wire_run(
        monkeypatch, quotes=quotes, prev_map=prev_map,
        edgar={"GOOD": (2.5, "8-K item 1.01")},
        news={"BIGG": "BIGG wins huge contract"},
        movers=["PENY", "THIN", "FLAT", "BIGG"],
        subscribers=("a@x.com", "b@x.com"),
    )
    n = asyncio.run(pw.run_premarket_watch("2026-07-14"))
    assert n == 2  # both subscribers emailed

    # rank: GOOD 5.0 x 2.5 = 12.5 beats BIGG 10.0 x 1.0 = 10.0
    locked = json.loads(r.keys["theta:ignition:candidates:2026-07-14"])
    assert [c["ticker"] for c in locked] == ["GOOD", "BIGG"]
    assert locked[0] == {"ticker": "GOOD", "prev_close": 20.0,
                         "premarket_price": 21.0, "gap_pct": 5.0,
                         "catalyst": "8-K item 1.01"}
    assert locked[1]["catalyst"] == "BIGG wins huge contract"

    # email content: top rows + the explicit not-a-signal line, Saro subject
    to, subject, html = sent[0]
    assert subject == "🌅 Saro Premarket Watch — 2026-07-14"  # 'Saro' passes killswitch
    assert "Watchlist only — Saro's confirmed pick still fires after 9:33 ET. Not a trade signal." in html
    assert "GOOD" in html and "BIGG" in html and "PENY" not in html
    assert "$20.00" in html and "+5.0%" in html


def test_run_empty_morning_prelocks_empty_list_and_sends_nothing(monkeypatch):
    r, sent = _wire_run(monkeypatch, quotes={}, prev_map={}, movers=["ZZZZ"])
    n = asyncio.run(pw.run_premarket_watch("2026-07-14"))
    assert n == 0
    assert sent == []
    assert json.loads(r.keys["theta:ignition:candidates:2026-07-14"]) == []


def test_run_emails_only_top_5_of_many(monkeypatch):
    syms = [f"S{i:02d}" for i in range(8)]
    quotes = {s: 10.0 + i for i, s in enumerate(syms)}          # all >= $5
    prev_map = {s: {"c": (10.0 + i) / 1.10, "v": 50_000_000}    # all +10% gap
                for i, s in enumerate(syms)}
    r, sent = _wire_run(monkeypatch, quotes=quotes, prev_map=prev_map, movers=syms)
    asyncio.run(pw.run_premarket_watch("2026-07-14"))
    locked = json.loads(r.keys["theta:ignition:candidates:2026-07-14"])
    assert len(locked) == 8                       # pre-lock keeps the FULL list
    _, _, html = sent[0]
    assert sum(1 for s in syms if s in html) == 5  # email shows exactly top 5


def test_build_respects_quote_budget(monkeypatch):
    """More candidates than MAX_QUOTES -> at most MAX_QUOTES quote calls,
    catalyst names first in the budget."""
    movers = [f"M{i:03d}" for i in range(100)]
    calls = []

    async def fake_quote(sym):
        calls.append(sym)
        return None  # no price -> no candidate; we only count the calls

    async def fake_edgar():
        return {"EDG1": (2.0, "8-K item 8.01")}

    async def fake_news():
        return {"NWS1": "headline"}

    async def fake_movers():
        return movers

    async def fake_prev_map(today):
        return {}

    async def fake_pace():
        return None

    monkeypatch.setattr(pw, "_edgar_catalysts", fake_edgar)
    monkeypatch.setattr(pw, "_fetch_premarket_news", fake_news)
    monkeypatch.setattr(pw, "_fetch_movers_symbols", fake_movers)
    monkeypatch.setattr(pw, "_fetch_quote", fake_quote)
    monkeypatch.setattr(pw, "_load_prev_map", fake_prev_map)
    monkeypatch.setattr(pw, "_pace", fake_pace)

    asyncio.run(pw.build_watchlist("2026-07-14"))
    assert len(calls) == pw.MAX_QUOTES
    assert calls[0] == "EDG1" and calls[1] == "NWS1"  # catalysts spend first


# ── wiring: the hook MUST be called from the scheduler loop ──────────────────
def test_hook_is_wired_into_premarket_scheduler_loop():
    """Regression guard (test_fmp_eod_snapshot pattern): the module existing
    and green tests mean nothing if the scheduler never calls the hook."""
    import inspect
    import app.engines.options.premarket_scheduler as sched
    src = inspect.getsource(sched)
    assert "_check_and_run_premarket_watch" in src
