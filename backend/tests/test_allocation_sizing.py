"""Unit tests for the allocation-mode sizing path + validate_account_for_placement.

These cover the exact scenarios the user gave for the Live Trading Risk
Calculator:
  • $1k allocation on a $150 stock → 6 shares
  • $10k allocation on the same stock → 66 shares
  • $25k allocation from a $20k cash account → capped at $20k by the cash
    constraint (not buying_power)
  • Margin account → buying_power, not cash, is the constraint
  • Stale or missing balance → validate_account_for_placement gates placement

Pure-function tests — no DB, no FastAPI, no broker calls.
"""
import pytest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from app.api.routes.live_trading import (
    compute_stock_position_size,
    validate_account_for_placement,
)


# ── 1. $1k allocation → 6 shares (the user's verification case) ───────────
def test_1k_allocation():
    r = compute_stock_position_size(
        entry_price=150.0,
        stop_loss=147.0,
        account_equity=100_000.0,
        buying_power=200_000.0,
        account_type="margin",
        risk_per_trade_usd=None,
        risk_per_trade_pct=None,
        max_position_usd=None,
        allocation_usd=1_000.0,
        cached_cash=50_000.0,
    )
    assert r["risk_model"] == "allocation"
    assert r["final_shares"] == 6                   # floor(1000 / 150)
    assert r["final_notional"] == 900.0             # 6 * 150
    # buying_power=$200k → cap=1333 shares; we only want 6, so BP NOT applied.
    bp = next(c for c in r["constraints"] if c["name"] == "buying_power")
    assert bp["applied"] is False
    # Informational risk fields still populated.
    assert r["risk_per_share"] == 3.0
    assert r["actual_dollar_risk"] == 18.0          # 6 * 3
    assert r["allocation_usd"] == 1_000.0


# ── 2. $10k allocation → 66 shares (the user's verification case) ─────────
def test_10k_allocation():
    r = compute_stock_position_size(
        entry_price=150.0,
        stop_loss=147.0,
        account_equity=100_000.0,
        buying_power=200_000.0,
        account_type="margin",
        risk_per_trade_usd=None,
        risk_per_trade_pct=None,
        max_position_usd=None,
        allocation_usd=10_000.0,
        cached_cash=50_000.0,
    )
    assert r["risk_model"] == "allocation"
    assert r["final_shares"] == 66                  # floor(10000 / 150)
    assert r["final_notional"] == 9_900.0           # 66 * 150
    bp = next(c for c in r["constraints"] if c["name"] == "buying_power")
    assert bp["applied"] is False


# ── 3. Cash account: $25k allocation from $20k cash → capped at 400 ───────
def test_cash_account_blocks_25k_with_20k_cash():
    r = compute_stock_position_size(
        entry_price=50.0,
        stop_loss=48.0,
        account_equity=50_000.0,
        buying_power=20_000.0,           # cash account → BP usually == cash
        account_type="cash",
        risk_per_trade_usd=None,
        risk_per_trade_pct=None,
        max_position_usd=None,
        allocation_usd=25_000.0,
        cached_cash=20_000.0,
    )
    assert r["risk_model"] == "allocation"
    # Raw allocation would buy 500 shares; cash caps at 400.
    assert r["final_shares"] == 400                 # floor(20000 / 50)
    assert r["final_notional"] == 20_000.0
    cash_c = next(c for c in r["constraints"] if c["name"] == "cash")
    assert cash_c["applied"] is True
    assert cash_c["limit_usd"] == 20_000.0
    # For a cash account we report the cash constraint, NOT buying_power.
    names = {c["name"] for c in r["constraints"]}
    assert "cash" in names
    assert "buying_power" not in names


# ── 4. Margin account → buying_power is the constraint, not cash ──────────
def test_margin_account_uses_buying_power():
    r = compute_stock_position_size(
        entry_price=100.0,
        stop_loss=97.0,
        account_equity=30_000.0,
        buying_power=60_000.0,
        account_type="margin",
        risk_per_trade_usd=None,
        risk_per_trade_pct=None,
        max_position_usd=None,
        allocation_usd=50_000.0,
        cached_cash=15_000.0,             # cash is lower but irrelevant for margin
    )
    assert r["risk_model"] == "allocation"
    # Raw allocation would buy 500 shares ($50k / $100). BP $60k → cap=600.
    # Allocation is the smaller of the two → final = 500, BP NOT applied.
    assert r["final_shares"] == 500
    assert r["final_notional"] == 50_000.0
    names = {c["name"] for c in r["constraints"]}
    assert "buying_power" in names
    assert "cash" not in names           # never present on margin path
    bp = next(c for c in r["constraints"] if c["name"] == "buying_power")
    assert bp["limit_usd"] == 60_000.0
    # The user's brief calls out "limited by BP not cash"; verify by raising
    # the allocation to exceed BP and confirming BP applies.
    r2 = compute_stock_position_size(
        entry_price=100.0,
        stop_loss=97.0,
        account_equity=30_000.0,
        buying_power=60_000.0,
        account_type="margin",
        risk_per_trade_usd=None,
        risk_per_trade_pct=None,
        max_position_usd=None,
        allocation_usd=80_000.0,
        cached_cash=15_000.0,
    )
    assert r2["final_shares"] == 600     # capped by BP
    bp2 = next(c for c in r2["constraints"] if c["name"] == "buying_power")
    assert bp2["applied"] is True


# ── 5. Stale balance → validate_account_for_placement blocks ──────────────
def test_stale_balance_blocks_placement():
    stale_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    account = SimpleNamespace(
        cached_equity=100_000.0,
        cached_cash=50_000.0,
        cached_balance_at=stale_at,
        account_type="cash",
    )
    v = validate_account_for_placement(account)
    assert v is not None
    assert v["reason"] == "stale"
    assert "10 minutes stale" in v["error"]


# ── 6. Missing balance (cached_equity is None) → "missing" ────────────────
def test_missing_balance_blocks_placement():
    fresh_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    account = SimpleNamespace(
        cached_equity=None,
        cached_cash=50_000.0,
        cached_balance_at=fresh_at,
        account_type="cash",
    )
    v = validate_account_for_placement(account)
    assert v is not None
    assert v["reason"] == "missing"
    assert "missing" in v["error"].lower() or "sync" in v["error"].lower()
