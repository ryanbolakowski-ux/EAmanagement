"""Verifies the idempotency key collapses tick-jitter prices into a single
band and discriminates only on real setup-shape differences.

These are pure unit tests against signal_guard.make_idempotency_key (no DB).
"""
from datetime import datetime, timezone

from app.engines.account_signals.signal_guard import (
    make_idempotency_key, _tick_band, TICK_SIZES_KEY,
)


# Static reference timestamp; the key intentionally ignores it so it can
# be anything.
TS = datetime(2026, 6, 5, 13, 0, tzinfo=timezone.utc)


def test_idem_key_rounds_es_prices_to_quarter_tick():
    """ES tick = 0.25. Three entry prices that all round to the SAME band
    (7540.25) must produce one idem key. Without this the watcher would
    generate 3 distinct keys → 3 distinct signals/emails for one setup."""
    keys = set()
    for entry in (7540.13, 7540.20, 7540.25, 7540.31, 7540.30):
        # All of these round to 7540.25 in the nearest-0.25-tick band
        k = make_idempotency_key(
            "w1", "s1", "ES", "long", TS,
            entry=entry, stop=7535.0, take_profit=7560.0,
        )
        keys.add(k)
    assert len(keys) == 1, f"expected 1 key for tick-band-equivalent prices, got {len(keys)}"


def test_idem_key_rounds_nq_prices_to_quarter_tick():
    """NQ tick = 0.25 too. Same assertion."""
    keys = set()
    for entry in (30183.13, 30183.20, 30183.25, 30183.31, 30183.36):
        k = make_idempotency_key(
            "w1", "s1", "NQ", "short", TS,
            entry=entry, stop=30200.0, take_profit=30150.0,
        )
        keys.add(k)
    assert len(keys) == 1


def test_idem_key_distinct_per_setup_shape():
    """Same price band, different direction → DIFFERENT keys. The dedup
    must not collapse a long and a short setup at the same level."""
    a = make_idempotency_key("w1", "s1", "ES", "long",  TS, 7540.25, 7535.0, 7560.0)
    b = make_idempotency_key("w1", "s1", "ES", "short", TS, 7540.25, 7545.0, 7520.0)
    assert a != b


def test_idem_key_distinct_per_instrument():
    a = make_idempotency_key("w1", "s1", "ES", "long", TS, 7540.25, 7535.0, 7560.0)
    b = make_idempotency_key("w1", "s1", "NQ", "long", TS, 7540.25, 7535.0, 7560.0)
    assert a != b


def test_idem_key_distinct_per_watcher():
    a = make_idempotency_key("w1", "s1", "ES", "long", TS, 7540.25, 7535.0, 7560.0)
    b = make_idempotency_key("w2", "s1", "ES", "long", TS, 7540.25, 7535.0, 7560.0)
    assert a != b


def test_idem_key_bar_ts_ignored():
    """A setup re-detected 1, 5, 30 minutes later must produce the SAME key.
    The cooldown window (15 min default) is what suppresses duplicates, not
    timestamp variance in the key itself.
    """
    ts0 = datetime(2026, 6, 5, 13, 0,  tzinfo=timezone.utc)
    ts1 = datetime(2026, 6, 5, 13, 1,  tzinfo=timezone.utc)
    ts5 = datetime(2026, 6, 5, 13, 5,  tzinfo=timezone.utc)
    ts30 = datetime(2026, 6, 5, 13, 30, tzinfo=timezone.utc)
    keys = {
        make_idempotency_key("w1", "s1", "ES", "long", t, 7540.25, 7535.0, 7560.0)
        for t in (ts0, ts1, ts5, ts30)
    }
    assert len(keys) == 1, "key must be timestamp-independent"


def test_idem_key_buy_normalizes_to_long():
    """`buy` and `long` are synonyms in the wire payload and must map to
    the same key. Same for `sell` → `short`."""
    a = make_idempotency_key("w1", "s1", "ES", "buy",  TS, 7540.25, 7535.0, 7560.0)
    b = make_idempotency_key("w1", "s1", "ES", "long", TS, 7540.25, 7535.0, 7560.0)
    assert a == b
    c = make_idempotency_key("w1", "s1", "ES", "sell",  TS, 7540.25, 7545.0, 7520.0)
    d = make_idempotency_key("w1", "s1", "ES", "short", TS, 7540.25, 7545.0, 7520.0)
    assert c == d


def test_tick_band_basic():
    assert _tick_band(7540.13, 0.25) == "7540.25"
    assert _tick_band(7540.31, 0.25) == "7540.25"
    assert _tick_band(7540.49, 0.25) == "7540.50"
    assert _tick_band(7540.50, 0.25) == "7540.50"


def test_tick_size_table_has_majors():
    for sym in ("ES", "NQ", "MES", "MNQ", "RTY", "YM"):
        assert sym in TICK_SIZES_KEY


def test_reproduces_jaceford_60_signal_collapse():
    """Direct simulation of the bug: feed 60 minutes of an ES short setup
    that drifts by < 0.25 every bar (the real prod data showed 7563/7567.75/
    7557.5 fire every minute for 12 consecutive minutes). After the fix,
    all 60 should collapse to ONE key."""
    keys = set()
    for minute in range(60):
        ts = datetime(2026, 6, 5, 14, minute, tzinfo=timezone.utc)
        # The exact prices jace's watcher logged: ES short 7563 / 7567.75 / 7557.5
        # with very small minute-to-minute jitter inside the 0.25 band.
        entry = 7563.0 + (minute % 2) * 0.05  # 0.05 jitter = inside 1 tick
        stop = 7567.75
        tp = 7557.5
        k = make_idempotency_key("w-jace", "s-fvg", "ES", "short", ts, entry, stop, tp)
        keys.add(k)
    assert len(keys) == 1, (
        f"expected 60 ES short signals on the same setup to collapse to 1 key, "
        f"got {len(keys)} keys — fix is broken"
    )
