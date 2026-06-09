"""Stock-pick quality filters (theta_scanner) — unit tests.

These exercise the new pre-market quality gate WITHOUT touching Postgres or
Polygon: we monkeypatch the Polygon 1-min bar fetch + the 8-K catalyst lookup
and the market snapshot, so the logic is tested deterministically.

Spec mapping:
  • below-VWAP long          -> NOT a clean trade (watch_only=True)
  • >8% above VWAP           -> hard reject (verdict 'reject')
  • clean candidate          -> accepted, quality_reasons populated
  • failing-continuation     -> watch_only=True (soft)

Run: pytest backend/tests/test_stock_quality_filters.py -v -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import pytest

from app.engines.options import theta_scanner as ts


# ── helpers to build synthetic Polygon 1-min bars ───────────────────────────
# Polygon bar = {t: epoch_ms, o,h,l,c, v, vw}. We place bars in the pre-market
# window (08:00 ET on a fixed weekday) so _bar_is_premarket_et() passes.
def _et_premarket_ms(minute_offset: int) -> int:
    """epoch ms for 2026-06-08 08:00 ET + minute_offset (a Monday)."""
    import datetime as _dt
    try:
        import zoneinfo
        base = _dt.datetime(2026, 6, 8, 8, 0, tzinfo=zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        base = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    base = base + _dt.timedelta(minutes=minute_offset)
    return int(base.timestamp() * 1000)


def _bars(seq, vol=50_000):
    """seq: list of (h, l, c) per minute. vw defaults to c. Each bar carries
    `vol` shares so pre-market $-vol clears $1M easily (50k * ~$20 * N)."""
    out = []
    for i, (h, l, c) in enumerate(seq):
        out.append({"t": _et_premarket_ms(i), "o": c, "h": h, "l": l, "c": c,
                    "v": vol, "vw": c})
    return out


def _patch_bars(monkeypatch, bars):
    async def fake_1m(ticker, date_et):
        return bars
    monkeypatch.setattr(ts, "_polygon_1min_bars", fake_1m, raising=False)
    # _polygon_1min_bars / _today_et_date_str are imported INSIDE the function
    # from premarket_scheduler, so patch there too.
    import app.engines.options.premarket_scheduler as ps
    monkeypatch.setattr(ps, "_polygon_1min_bars", fake_1m, raising=False)
    monkeypatch.setattr(ps, "_today_et_date_str", lambda: "2026-06-08", raising=False)


def _candidate(price, gap=12.0, rel_vol=4.2, score=20.0):
    return {"ticker": "TEST", "price": price, "gap_pct": gap,
            "rel_vol": rel_vol, "today_vol": 5_000_000, "score": score,
            "catalyst_reason": "8-K item 8.01"}


# ── 1. below-VWAP long -> watch_only (not a clean trade) ─────────────────────
def test_below_vwap_long_is_watch_only(monkeypatch):
    # VWAP of a flat ~$20 tape is ~$20; price $19 sits below it. Higher highs
    # present so the ONLY failing filter is VWAP.
    bars = _bars([(20.0, 19.8, 20.0)] * 5 + [(20.1, 19.9, 20.05),
                                             (20.2, 20.0, 20.1),
                                             (20.3, 20.1, 20.2)])
    _patch_bars(monkeypatch, bars)
    cand = _candidate(price=19.0)
    verdict, reasons = asyncio.run(ts._apply_quality_filters(None, cand))
    assert verdict == "watch", f"below-VWAP should be watch-only, got {verdict} ({reasons})"
    assert any("below VWAP" in r for r in reasons), reasons


# ── 2. >8% above VWAP -> hard reject ────────────────────────────────────────
def test_overextended_above_vwap_is_rejected(monkeypatch):
    # VWAP ~$20, price $25 => +25% above => overextension hard reject.
    bars = _bars([(20.0, 19.8, 20.0)] * 8)
    _patch_bars(monkeypatch, bars)
    cand = _candidate(price=25.0)
    verdict, reasons = asyncio.run(ts._apply_quality_filters(None, cand))
    assert verdict == "reject", f"overextended should reject, got {verdict} ({reasons})"


# ── 3. clean candidate -> accept + quality_reasons populated ────────────────
def test_clean_candidate_accepts_with_reasons(monkeypatch):
    # Rising tape, price just above VWAP (~+2%), last 3 bars higher highs.
    bars = _bars([(19.0, 18.8, 18.9), (19.2, 19.0, 19.1), (19.4, 19.2, 19.3),
                  (19.6, 19.4, 19.5), (19.8, 19.6, 19.7), (20.0, 19.8, 19.9)])
    _patch_bars(monkeypatch, bars)
    cand = _candidate(price=20.2)  # just above the ~19.x VWAP, < 8% over
    verdict, reasons = asyncio.run(ts._apply_quality_filters(None, cand))
    assert verdict == "accept", f"clean candidate should accept, got {verdict} ({reasons})"
    assert reasons, "quality_reasons must be populated"
    assert any("above VWAP" in r for r in reasons), reasons
    assert any("HH x3" in r for r in reasons), reasons


# ── 4. failing continuation (fading) but otherwise OK -> watch_only ─────────
def test_failing_continuation_is_watch_only(monkeypatch):
    # Price above VWAP and < 8% over, but the last 3 bars are LOWER highs
    # (fading off the gap high) => continuation fail => watch-only.
    bars = _bars([(21.0, 19.0, 20.5), (20.8, 19.5, 20.3), (20.6, 19.6, 20.1),
                  (20.4, 19.6, 20.0), (20.2, 19.6, 19.9)])
    _patch_bars(monkeypatch, bars)
    # VWAP ~20.1; pick price 20.3 is above VWAP and < 8% over.
    cand = _candidate(price=20.3)
    verdict, reasons = asyncio.run(ts._apply_quality_filters(None, cand))
    assert verdict == "watch", f"fading should be watch-only, got {verdict} ({reasons})"
    assert any("fading" in r for r in reasons), reasons


# ── 5. graceful degrade: no bars -> accept, filters skipped ─────────────────
def test_no_bars_degrades_gracefully(monkeypatch):
    _patch_bars(monkeypatch, [])
    cand = _candidate(price=20.0)
    verdict, reasons = asyncio.run(ts._apply_quality_filters(None, cand))
    assert verdict == "accept"
    assert any("n/a" in r for r in reasons), reasons


# ── 6. low premarket liquidity -> hard reject ───────────────────────────────
def test_thin_premarket_liquidity_is_rejected(monkeypatch):
    # tiny volume per bar so pre-mkt $-vol < $1M
    bars = _bars([(20.0, 19.8, 20.0)] * 5, vol=100)  # 5 * 100 * $20 = $10k
    _patch_bars(monkeypatch, bars)
    cand = _candidate(price=20.1)
    verdict, reasons = asyncio.run(ts._apply_quality_filters(None, cand))
    assert verdict == "reject", f"thin liquidity should reject, got {verdict} ({reasons})"


# ── 7. end-to-end: find_best_premarket_pick prefers clean over watch ────────
def test_find_best_prefers_clean_pick(monkeypatch):
    """Two qualifying gappers: a CLEAN one (above VWAP, HH) and a fading one.
    The scanner must return the clean one with watch_only=False."""
    snapshot_rows = [
        {"ticker": "CLEAN", "day": {"c": 20.2, "v": 6_000_000},
         "prevDay": {"c": 18.0, "v": 1_000_000}},
        {"ticker": "FADER", "day": {"c": 30.5, "v": 8_000_000},
         "prevDay": {"c": 27.0, "v": 1_000_000}},
    ]

    async def fake_snapshot():
        return snapshot_rows
    monkeypatch.setattr(ts, "_fetch_market_snapshot", fake_snapshot, raising=False)
    import app.engines.options.momentum_scanner as ms
    monkeypatch.setattr(ms, "_fetch_market_snapshot", fake_snapshot, raising=False)

    async def fake_catalyst(db, ticker):
        return 2.0, "8-K item 8.01"
    monkeypatch.setattr(ts, "_get_8k_catalyst", fake_catalyst, raising=False)

    import app.engines.options.premarket_scheduler as ps
    monkeypatch.setattr(ps, "_today_et_date_str", lambda: "2026-06-08", raising=False)

    clean_bars = _bars([(19.0, 18.8, 18.9), (19.3, 19.0, 19.2),
                        (19.6, 19.3, 19.5), (19.9, 19.6, 19.8),
                        (20.1, 19.8, 20.0)])
    fade_bars = _bars([(31.0, 28.0, 30.8), (30.8, 28.5, 30.4),
                      (30.6, 28.6, 30.1)])

    async def fake_1m(ticker, date_et):
        return clean_bars if ticker == "CLEAN" else fade_bars
    monkeypatch.setattr(ps, "_polygon_1min_bars", fake_1m, raising=False)

    # Redis persistence is best-effort + wrapped in try/except; force it to
    # no-op so the test never reaches a real Redis.
    import sys, types
    fake_redis_mod = types.ModuleType("redis.asyncio")
    class _FakeRedis:
        @classmethod
        def from_url(cls, *a, **k): return cls()
        async def setex(self, *a, **k): return True
        async def set(self, *a, **k): return True
    fake_redis_mod.from_url = _FakeRedis.from_url
    monkeypatch.setitem(sys.modules, "redis.asyncio", fake_redis_mod)

    pick = asyncio.run(ts.find_best_premarket_pick(None))
    assert pick is not None
    assert pick["ticker"] == "CLEAN", f"expected CLEAN, got {pick['ticker']}"
    assert pick.get("watch_only") is False
    assert pick.get("quality_reasons"), "quality_reasons must be set on the pick"
