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


def test_tight_es_stop_warns_but_valid():
    g = validate_geometry("long", 5000.0, 4999.5, 5020.0, "ES")
    assert g["valid"] is True            # geometry is fine
    assert any("tight" in w for w in g["warnings"])  # but flagged


def test_unknown_direction_rejected():
    assert validate_geometry("sideways", 5000, 4990, 5020, "ES")["valid"] is False


def test_idempotency_key_stable():
    ts = datetime(2026, 5, 28, 14, 30, tzinfo=timezone.utc)
    a = make_idempotency_key("w1", "s1", "NQ", "long", ts, 30043.5, 30033.5, 30073.5)
    b = make_idempotency_key("w1", "s1", "NQ", "long", ts, 30043.5, 30033.5, 30073.5)
    assert a == b


def test_idempotency_key_differs_on_price():
    ts = datetime(2026, 5, 28, 14, 30, tzinfo=timezone.utc)
    a = make_idempotency_key("w1", "s1", "NQ", "long", ts, 30043.5, 30033.5, 30073.5)
    b = make_idempotency_key("w1", "s1", "NQ", "long", ts, 30044.0, 30033.5, 30073.5)
    assert a != b


def test_idempotency_key_differs_on_bar():
    ts1 = datetime(2026, 5, 28, 14, 30, tzinfo=timezone.utc)
    ts2 = datetime(2026, 5, 28, 14, 35, tzinfo=timezone.utc)
    a = make_idempotency_key("w1", "s1", "NQ", "long", ts1, 30043.5, 30033.5, 30073.5)
    b = make_idempotency_key("w1", "s1", "NQ", "long", ts2, 30043.5, 30033.5, 30073.5)
    assert a != b
