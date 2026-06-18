"""Systems-check fix: tiny futures stop ranges are HARD-rejected; stocks warn."""
from app.engines.account_signals.signal_guard import validate_geometry, MIN_STOP_POINTS


def test_tiny_futures_stop_rejected():
    # NQ floor is 8pt. A 0.5pt stop is noise -> hard reject.
    g = validate_geometry("long", 20000.0, 19999.5, 20001.5, "NQ")
    assert g["valid"] is False
    assert "too tight" in g["error"]


def test_tiny_es_stop_rejected():
    g = validate_geometry("short", 7000.0, 7000.5, 6997.0, "ES")  # 0.5pt < 2.0 floor
    assert g["valid"] is False


def test_normal_futures_stop_ok():
    # NQ 20pt stop, 3:1 -> valid.
    g = validate_geometry("long", 20000.0, 19980.0, 20060.0, "NQ")
    assert g["valid"] is True
    assert g["rr"] == 3.0


def test_stock_small_dollar_stop_warns_not_rejected():
    # AAPL-like: $0.50 stop on a $150 stock is legitimate (not in MIN_STOP_POINTS).
    g = validate_geometry("long", 150.0, 149.5, 151.5, "AAPL")
    assert g["valid"] is True  # NOT rejected
    # it may carry a soft warning, that's fine


def test_bad_ordering_still_rejected():
    g = validate_geometry("long", 100.0, 101.0, 105.0, "NQ")  # stop above entry on a long
    assert g["valid"] is False
