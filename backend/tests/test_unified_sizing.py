"""Unit tests for unified_size() — the min-of sizing function (#136).
Pure, no DB/network."""
from app.core.sizing import unified_size, rr_ratio


def test_risk_per_trade_usd_binds_when_smallest():
    # Stock: entry 100, stop 99 -> $1 risk/share. $500 risk -> 500 shares.
    # No tighter cap -> risk model binds.
    r = unified_size(entry_price=100, stop_loss=99, risk_per_trade_usd=500,
                     account_equity=100_000, point_value=1.0, symbol="ABC")
    assert r.ok and r.final_size == 500
    assert r.binding_constraint == "risk_per_trade_usd"
    assert abs(r.actual_risk_usd - 500) < 1e-6


def test_buying_power_caps_below_risk_target():
    # Risk target wants 500 shares ($50k notional) but only $20k BP -> 200 shares.
    r = unified_size(entry_price=100, stop_loss=99, risk_per_trade_usd=500,
                     cached_buying_power=20_000, account_type="margin", point_value=1.0)
    assert r.final_size == 200
    assert r.binding_constraint == "buying_power"


def test_cash_account_bound_by_cash_not_bp():
    r = unified_size(entry_price=50, stop_loss=49, risk_per_trade_usd=1000,
                     cached_cash=5_000, cached_buying_power=999_999,
                     account_type="cash", point_value=1.0)
    # cash $5000 / $50 = 100 shares (cash binds, BP ignored for cash acct)
    assert r.final_size == 100
    assert r.binding_constraint == "cash"


def test_max_position_usd_caps():
    r = unified_size(entry_price=100, stop_loss=98, risk_per_trade_usd=10_000,
                     max_position_usd=3_000, point_value=1.0)
    # $3000 / $100 = 30 shares
    assert r.final_size == 30
    assert r.binding_constraint == "max_position_usd"


def test_max_units_caps():
    r = unified_size(entry_price=20000, stop_loss=19980, risk_per_trade_usd=100_000,
                     max_units=3, point_value=20.0, symbol="NQ")  # NQ $20/pt
    assert r.final_size == 3
    assert r.binding_constraint == "max_units"


def test_futures_point_value_risk_math():
    # NQ: 20-pt stop * $20/pt = $400 risk/contract. $1200 risk -> 3 contracts.
    r = unified_size(entry_price=20000, stop_loss=19980, risk_per_trade_usd=1200,
                     point_value=20.0, symbol="NQ")
    assert r.final_size == 3
    assert abs(r.risk_per_unit - 400) < 1e-6


def test_allocation_sizes_by_notional():
    # $10k allocation at $100 entry -> 100 shares (notional-based, not risk).
    r = unified_size(entry_price=100, stop_loss=95, allocation_usd=10_000, point_value=1.0)
    assert r.final_size == 100
    assert r.risk_model == "allocation_usd"


def test_pct_of_equity_fallback():
    r = unified_size(entry_price=100, stop_loss=99, risk_per_trade_pct=1.0,
                     account_equity=50_000, point_value=1.0)
    # 1% of 50k = $500 risk / $1 = 500 shares
    assert r.final_size == 500
    assert r.risk_model == "risk_per_trade_pct"


def test_commission_increases_risk_per_unit():
    r = unified_size(entry_price=100, stop_loss=99, risk_per_trade_usd=500,
                     commission_per_unit=0.5, point_value=1.0)
    # risk/unit = $1 + 2*0.5 = $2 -> 250 shares
    assert r.final_size == 250
    assert abs(r.risk_per_unit - 2.0) < 1e-6


def test_zero_size_when_capital_too_small():
    r = unified_size(entry_price=1000, stop_loss=995, risk_per_trade_usd=10_000,
                     cached_cash=100, account_type="cash", point_value=1.0)
    assert r.final_size == 0 and not r.ok
    assert "cash" in r.reason


def test_invalid_inputs():
    assert not unified_size(entry_price=0, stop_loss=99, risk_per_trade_usd=100).ok
    assert not unified_size(entry_price=100, stop_loss=100, risk_per_trade_usd=100).ok


def test_no_basis_returns_not_ok():
    r = unified_size(entry_price=100, stop_loss=99)  # no allocation/risk/equity
    assert not r.ok and r.reason == "no_sizing_basis"


def test_binding_is_exactly_one_constraint():
    r = unified_size(entry_price=100, stop_loss=99, risk_per_trade_usd=500,
                     max_position_usd=20_000, cached_buying_power=20_000, account_type="margin",
                     point_value=1.0)
    applied = [c for c in r.constraints if c.applied]
    assert len(applied) == 1
    assert applied[0].name == r.binding_constraint


def test_rr_ratio():
    assert rr_ratio(100, 99, 102) == 2.0     # 2 reward / 1 risk
    assert rr_ratio(100, 98, 101) == 0.5     # 1 / 2
    assert rr_ratio(100, 100, 102) is None   # zero risk
    assert rr_ratio(100, 99, None) is None
