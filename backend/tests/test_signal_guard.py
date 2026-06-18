"""Bug 6 (geometry validation) + Bug 4 (idempotency key) — pure unit tests, no DB."""
from datetime import datetime, timezone
from app.engines.account_signals.signal_guard import validate_geometry, make_idempotency_key


def test_long_valid_geometry():
    g = validate_geometry("long", 5000, 4990, 5020, "ES")
    assert g["valid"] is True
    assert g["risk"] == 10.0
    assert g["reward"] == 20.0
    assert g["rr"] == 2.0


def test_long_invalid_geometry():
    # stop above entry is invalid for a long
    g = validate_geometry("long", 5000, 5010, 5020, "ES")
    assert g["valid"] is False
    assert "long requires" in g["error"]


def test_short_valid_geometry():
    g = validate_geometry("short", 5000, 5010, 4980, "ES")
    assert g["valid"] is True
    assert g["risk"] == 10.0
    assert g["reward"] == 20.0
    assert g["rr"] == 2.0


def test_short_invalid_geometry():
    # tp above entry is invalid for a short
    g = validate_geometry("short", 5000, 4990, 4980, "ES")
    assert g["valid"] is False
    assert "short requires" in g["error"]


def test_zero_risk_rejected():
    g = validate_geometry("long", 5000, 5000, 5020, "ES")
    assert g["valid"] is False


def test_tight_es_stop_now_hard_rejected():
    # TINY-RANGE-HARD-REJECT: a sub-floor futures stop (0.5pt < ES 2.0
    # floor) is now a HARD reject (meaningless/noise range), not a warning.
    g = validate_geometry("long", 5000.0, 4999.5, 5020.0, "ES")
    assert g["valid"] is False
    assert "too tight" in g["error"]


def test_unknown_direction_rejected():
    assert validate_geometry("sideways", 5000, 4990, 5020, "ES")["valid"] is False


def test_idempotency_key_stable():
    ts = datetime(2026, 5, 28, 14, 30, tzinfo=timezone.utc)
    a = make_idempotency_key("w1", "s1", "NQ", "long", ts, 30043.5, 30033.5, 30073.5)
    b = make_idempotency_key("w1", "s1", "NQ", "long", ts, 30043.5, 30033.5, 30073.5)
    assert a == b


def test_idempotency_key_differs_on_price_outside_tick():
    """Prices that fall in DIFFERENT tick bands produce different keys."""
    ts = datetime(2026, 5, 28, 14, 30, tzinfo=timezone.utc)
    # NQ tick = 0.25 → 30043.5 and 30043.75 are different bands
    a = make_idempotency_key("w1", "s1", "NQ", "long", ts, 30043.50, 30033.5, 30073.5)
    b = make_idempotency_key("w1", "s1", "NQ", "long", ts, 30043.75, 30033.5, 30073.5)
    assert a != b


def test_idempotency_key_same_across_bars():
    """REGRESSION: the same setup detected on a later bar must produce the
    SAME key so the cooldown query catches the duplicate. Previously bar_ts
    was part of the key → every minute fired a fresh signal."""
    ts1 = datetime(2026, 5, 28, 14, 30, tzinfo=timezone.utc)
    ts2 = datetime(2026, 5, 28, 14, 35, tzinfo=timezone.utc)
    a = make_idempotency_key("w1", "s1", "NQ", "long", ts1, 30043.5, 30033.5, 30073.5)
    b = make_idempotency_key("w1", "s1", "NQ", "long", ts2, 30043.5, 30033.5, 30073.5)
    assert a == b, "bar_ts must NOT be part of the idempotency key"
