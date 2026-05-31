"""Unit tests for the asset-class classifier and the broker-support matrix.

These tests are pure — no DB, no app, no fixtures, no network — so they run
in milliseconds and never flake on test-DB or env state. They pin the rules
that drive (a) the live-deploy strategy dropdowns and (b) the server-side
validation in start_live_session.
"""

import pytest

from app.engines.strategy_classification import (
    BROKER_ASSET_CLASSES,
    OCC_OPTION_RE,
    broker_supports,
    classify_asset_class,
    supported_classes,
)


# ── classify_asset_class ────────────────────────────────────────────────

def test_futures_instruments():
    """ES/NQ are futures roots → 'futures'."""
    assert classify_asset_class(["ES", "NQ"]) == "futures"


def test_stock_instruments():
    """Plain US tickers → 'stock'."""
    assert classify_asset_class(["SPY", "NVDA", "AAPL"]) == "stock"


def test_options_instruments():
    """OCC-formatted option symbol → 'options'."""
    assert classify_asset_class(["SPY240517C00500000"]) == "options"


def test_empty_instruments():
    """Template strategy (no instruments) → 'unknown' — not deployable."""
    assert classify_asset_class([]) == "unknown"


def test_none_instruments():
    """None is treated like empty."""
    assert classify_asset_class(None) == "unknown"


def test_lowercase_instruments():
    """Symbols normalize to upper-case before classification."""
    assert classify_asset_class(["es", "nq"]) == "futures"


def test_whitespace_instruments():
    """Symbols are stripped before classification."""
    assert classify_asset_class([" SPY ", "  NVDA"]) == "stock"


def test_blank_strings_classify_as_unknown():
    """A list of blank strings is effectively empty → 'unknown'."""
    assert classify_asset_class(["", "  ", None]) == "unknown"


def test_mixed_majority_wins():
    """Mixed list with at least one futures root → 'futures'.

    This is the explicit design choice that protects against a futures
    order being routed to a stock-only broker — better to over-classify as
    futures (and route to a futures-capable broker only) than to under-
    classify and let an ES order through to Tradier.
    """
    assert classify_asset_class(["ES", "SPY"]) == "futures"


def test_micro_futures_classify_as_futures():
    """Micros (MES/MNQ/M2K/MYM) are also futures roots."""
    for sym in ("MES", "MNQ", "M2K", "MYM"):
        assert classify_asset_class([sym]) == "futures", sym


def test_options_takes_precedence_over_futures_root_in_same_list():
    """OCC formatting wins over a co-occurring futures root."""
    assert classify_asset_class(["ES", "SPY240517C00500000"]) == "options"


def test_occ_regex_directly():
    """OCC regex sanity — accepts well-formed symbols and rejects look-alikes."""
    assert OCC_OPTION_RE.match("SPY240517C00500000")
    assert OCC_OPTION_RE.match("A240517P00000001")  # min-length root, low strike
    # Reject: missing date
    assert not OCC_OPTION_RE.match("SPYC00500000")
    # Reject: missing C/P
    assert not OCC_OPTION_RE.match("SPY24051700500000")
    # Reject: short strike
    assert not OCC_OPTION_RE.match("SPY240517C0050000")


# ── broker_supports + supported_classes ────────────────────────────────

def test_broker_supports_tradier_stock_and_options_but_not_futures():
    assert broker_supports("tradier", "stock") is True
    assert broker_supports("tradier", "options") is True
    assert broker_supports("tradier", "futures") is False


def test_broker_supports_tradovate_only_futures():
    assert broker_supports("tradovate", "futures") is True
    assert broker_supports("tradovate", "stock") is False
    assert broker_supports("tradovate", "options") is False


def test_broker_supports_case_insensitive():
    assert broker_supports("TRADIER", "stock") is True
    assert broker_supports("Tradovate", "futures") is True


def test_unknown_broker_supports_nothing():
    assert broker_supports("robinhood", "stock") is False
    assert broker_supports("", "stock") is False
    assert broker_supports(None, "stock") is False  # type: ignore[arg-type]


def test_supported_classes_returns_list_for_known_broker():
    assert supported_classes("tradier") == ["stock", "options"]
    assert supported_classes("tradovate") == ["futures"]


def test_supported_classes_returns_empty_for_unknown_broker():
    assert supported_classes("robinhood") == []
    assert supported_classes("") == []


def test_broker_table_is_in_lockstep_with_real_brokers():
    """Sanity: the brokers we care about are in the table.

    If anyone removes Tradier or Tradovate the live deploy will silently
    reject every strategy — fail loudly here instead.
    """
    for b in ("tradier", "tradovate"):
        assert b in BROKER_ASSET_CLASSES, f"missing broker entry: {b}"
