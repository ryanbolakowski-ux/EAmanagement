"""Unit tests for compute_stock_position_size — the pure function that powers
the /api/v1/live-trading/sizing-preview endpoint.

These tests do NOT touch the DB or FastAPI app; they only exercise the math.
Worked example we expose to users: NVDA at entry=$1300, stop=$1287, on a
$100k account at 1% risk → buy 76 shares (~$98,800), risk $988 if stopped.
"""
import pytest

from app.api.routes.live_trading import compute_stock_position_size


def test_nvda_pct_risk_model():
    """The headline example. 1% of $100k = $1000 risk budget; $13 risk/share
    → 76 shares × $1300 = $98,800 notional; actual risk = 76 × $13 = $988.
    """
    r = compute_stock_position_size(
        entry_price=1300.0,
        stop_loss=1287.0,
        account_equity=100_000.0,
        buying_power=200_000.0,
        account_type="margin",
        risk_per_trade_usd=None,
        risk_per_trade_pct=1.0,
        max_position_usd=None,
    )
    assert r["risk_per_share"] == 13.0
    assert r["risk_dollars_target"] == 1000.0
    assert r["raw_shares"] == 76
    assert r["final_shares"] == 76
    assert r["final_notional"] == 98_800.0
    assert r["actual_dollar_risk"] == 988.0
    assert r["risk_model"] == "pct"
    assert "Buy 76 shares" in r["summary"].replace("{TICKER}", "NVDA")


def test_nvda_usd_risk_model():
    """Same NVDA setup but explicit $500 risk per trade. floor(500/13) = 38
    shares; actual risk = 38 × $13 = $494."""
    r = compute_stock_position_size(
        entry_price=1300.0,
        stop_loss=1287.0,
        account_equity=100_000.0,
        buying_power=200_000.0,
        account_type="margin",
        risk_per_trade_usd=500.0,
        risk_per_trade_pct=1.0,  # USD wins over pct
        max_position_usd=None,
    )
    assert r["risk_model"] == "usd"
    assert r["final_shares"] == 38
    assert r["actual_dollar_risk"] == 494.0


def test_max_position_cap_applies():
    """Risk math would buy 1000 shares; max_position_usd=$5000 caps at 50."""
    r = compute_stock_position_size(
        entry_price=100.0,
        stop_loss=99.0,
        account_equity=100_000.0,
        buying_power=200_000.0,
        account_type="margin",
        risk_per_trade_usd=1000.0,
        risk_per_trade_pct=None,
        max_position_usd=5_000.0,
    )
    assert r["final_shares"] == 50
    mp = next(c for c in r["constraints"] if c["name"] == "max_position_usd")
    assert mp["applied"] is True


def test_buying_power_cap_applies_when_lower():
    """USD risk would buy 10_000 shares; buying_power=$1000 caps at 10."""
    r = compute_stock_position_size(
        entry_price=100.0,
        stop_loss=99.0,
        account_equity=100_000.0,
        buying_power=1_000.0,
        account_type="cash",
        risk_per_trade_usd=10_000.0,
        risk_per_trade_pct=None,
        max_position_usd=None,
    )
    assert r["final_shares"] == 10
    bp = next(c for c in r["constraints"] if c["name"] == "buying_power")
    assert bp["applied"] is True


def test_zero_risk_per_share_rejected():
    """Entry == stop → risk-per-share is zero → can't size."""
    r = compute_stock_position_size(
        entry_price=100.0,
        stop_loss=100.0,
        account_equity=100_000.0,
        buying_power=200_000.0,
        account_type="margin",
        risk_per_trade_usd=None,
        risk_per_trade_pct=1.0,
        max_position_usd=None,
    )
    assert r["final_shares"] == 0
    assert "error" in r


def test_unaffordable_stock_returns_zero():
    """Entry $5000 with only $1000 buying power → buying_power caps at 0 shares."""
    r = compute_stock_position_size(
        entry_price=5_000.0,
        stop_loss=4_900.0,
        account_equity=100_000.0,
        buying_power=1_000.0,
        account_type="cash",
        risk_per_trade_usd=1_000.0,
        risk_per_trade_pct=None,
        max_position_usd=None,
    )
    assert r["final_shares"] == 0
    assert "Cannot size" in r["summary"]
