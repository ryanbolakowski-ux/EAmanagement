"""Equivalence test for the premarket stock-order sizing migration (#136).

`_compute_qty_from_sizing` in app/engines/options/premarket_scheduler.py was
migrated to delegate its core min-of share math to app.core.sizing.unified_size.
This test REIMPLEMENTS the OLD formula inline (exactly as it was before the
migration) and asserts the migrated function returns the SAME share count
across many cases: cash vs margin accounts, with/without max_position_usd,
fixed-$ vs pct risk, and missing stop (the default-2% path).

behavior_change = NONE.

Pure test: the DB lookup inside the migrated function is monkeypatched to hand
back an in-memory fake BrokerAccount, so there is no DB, broker, or network.
"""
import asyncio
from types import SimpleNamespace

import pytest

import app.database as appdb
import app.engines.options.premarket_scheduler as sched


# ─────────────────────────────────────────────────────────────────────────
# OLD formula, reimplemented verbatim from the pre-migration source.
# ─────────────────────────────────────────────────────────────────────────
def old_compute_qty(acct, *, entry: float, stop: float, default: int = 100) -> int:
    if acct is None:
        return max(1, default)

    risk_usd = acct.risk_per_trade_usd
    if not risk_usd:
        pct = acct.risk_per_trade_pct or 1.0
        eq = acct.cached_equity or 0.0
        risk_usd = (eq * pct / 100.0) if eq else None
    if not risk_usd or risk_usd <= 0:
        return max(1, default)

    per_share_risk = abs(entry - stop) if (stop and stop > 0) else (entry * 0.02)
    if per_share_risk <= 0:
        return max(1, default)

    shares = int(risk_usd // per_share_risk)
    position_usd = shares * entry

    if acct.max_position_usd and position_usd > acct.max_position_usd:
        shares = int(acct.max_position_usd // entry)
        position_usd = shares * entry

    bp = acct.cached_buying_power or 0.0
    if bp > 0 and position_usd > bp:
        shares = int(bp // entry)

    if (acct.account_type or "cash").lower() == "cash":
        cash = acct.cached_equity or bp
        if cash and shares * entry > cash:
            shares = int(cash // entry)

    return max(1, shares) if shares >= 1 else 0


# ─────────────────────────────────────────────────────────────────────────
# Harness: run the REAL migrated _compute_qty_from_sizing with the DB
# session factory monkeypatched to return our fake account.
# ─────────────────────────────────────────────────────────────────────────
def make_acct(**kw):
    return SimpleNamespace(
        id="acct-1",
        risk_per_trade_usd=kw.get("risk_per_trade_usd"),
        risk_per_trade_pct=kw.get("risk_per_trade_pct"),
        cached_equity=kw.get("cached_equity"),
        cached_buying_power=kw.get("cached_buying_power"),
        max_position_usd=kw.get("max_position_usd"),
        account_type=kw.get("account_type", "cash"),
    )


class _FakeResult:
    def __init__(self, acct):
        self._acct = acct
    def scalar_one_or_none(self):
        return self._acct


class _FakeSession:
    def __init__(self, acct):
        self._acct = acct
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def execute(self, *a, **k):
        return _FakeResult(self._acct)


def new_compute_qty(acct, *, entry, stop, default=100, monkeypatch):
    # The migrated function does `from app.database import async_session_factory`
    # INSIDE its body, so we must patch the name on the SOURCE module
    # (app.database), not on the scheduler module's namespace.
    monkeypatch.setattr(appdb, "async_session_factory",
                        lambda: _FakeSession(acct), raising=False)
    return asyncio.run(sched._compute_qty_from_sizing(
        broker_account_id="acct-1", ticker="TEST",
        entry=entry, stop=stop, default=default,
    ))


# ─────────────────────────────────────────────────────────────────────────
# Cases: (label, acct kwargs, entry, stop)
# ─────────────────────────────────────────────────────────────────────────
CASES = [
    # 1. cash acct, fixed $ risk, real stop, no caps binding
    ("cash_fixed_risk_basic",
     dict(risk_per_trade_usd=500.0, cached_equity=100_000.0,
          cached_buying_power=100_000.0, account_type="cash"),
     150.0, 147.0),
    # 2. cash acct, max_position binds tighter than risk
    ("cash_maxpos_binds",
     dict(risk_per_trade_usd=5000.0, cached_equity=100_000.0,
          cached_buying_power=100_000.0, max_position_usd=2000.0,
          account_type="cash"),
     50.0, 49.0),
    # 3. cash acct, cash (equity) binds (small account, cheap stop)
    ("cash_cash_binds",
     dict(risk_per_trade_usd=100_000.0, cached_equity=3000.0,
          cached_buying_power=50_000.0, account_type="cash"),
     20.0, 19.9),
    # 4. margin acct, buying_power binds
    ("margin_bp_binds",
     dict(risk_per_trade_usd=100_000.0, cached_equity=10_000.0,
          cached_buying_power=8000.0, account_type="margin"),
     40.0, 39.5),
    # 5. margin acct, risk binds (plenty of BP)
    ("margin_risk_binds",
     dict(risk_per_trade_usd=300.0, cached_equity=50_000.0,
          cached_buying_power=200_000.0, account_type="margin"),
     25.0, 24.0),
    # 6. pct-based risk (no fixed $), cash acct
    ("cash_pct_risk",
     dict(risk_per_trade_pct=1.0, cached_equity=50_000.0,
          cached_buying_power=50_000.0, account_type="cash"),
     100.0, 98.0),
    # 7. pct-based risk, margin acct, max_position binds
    ("margin_pct_maxpos",
     dict(risk_per_trade_pct=2.0, cached_equity=80_000.0,
          cached_buying_power=160_000.0, max_position_usd=5000.0,
          account_type="margin"),
     75.0, 73.0),
    # 8. missing stop → default 2% per-share risk, cash acct
    ("cash_missing_stop",
     dict(risk_per_trade_usd=1000.0, cached_equity=100_000.0,
          cached_buying_power=100_000.0, account_type="cash"),
     200.0, 0.0),
    # 9. missing stop, margin, bp binds
    ("margin_missing_stop_bp",
     dict(risk_per_trade_usd=100_000.0, cached_equity=10_000.0,
          cached_buying_power=6000.0, account_type="margin"),
     30.0, 0.0),
    # 10. wide stop → sizes to <1 share by risk → 0 (no max(1) rescue here)
    ("cash_under_one_share",
     dict(risk_per_trade_usd=10.0, cached_equity=100_000.0,
          cached_buying_power=100_000.0, account_type="cash"),
     500.0, 100.0),
    # 11. cash acct, equity None but bp set → cash falls back to bp
    ("cash_equity_none_bp_fallback",
     dict(risk_per_trade_usd=2000.0, cached_equity=None,
          cached_buying_power=4000.0, account_type="cash"),
     20.0, 19.0),
    # 12. high-priced stock, tight stop, margin, risk binds
    ("margin_highprice_risk",
     dict(risk_per_trade_usd=800.0, cached_equity=500_000.0,
          cached_buying_power=1_000_000.0, account_type="margin"),
     1200.0, 1180.0),
    # 13. account_type missing (defaults cash), max_position + cash both present
    ("default_type_maxpos_and_cash",
     dict(risk_per_trade_usd=9000.0, cached_equity=15_000.0,
          cached_buying_power=15_000.0, max_position_usd=10_000.0,
          account_type=None),
     30.0, 29.0),
]


@pytest.mark.parametrize("label,kw,entry,stop", CASES, ids=[c[0] for c in CASES])
def test_equivalence(label, kw, entry, stop, monkeypatch):
    acct = make_acct(**kw)
    expected = old_compute_qty(acct, entry=entry, stop=stop)
    got = new_compute_qty(acct, entry=entry, stop=stop, monkeypatch=monkeypatch)
    assert got == expected, f"{label}: new={got} old={expected}"


def test_no_account_id_returns_default(monkeypatch):
    # broker_account_id falsy → max(1, default), no DB touched
    got = asyncio.run(sched._compute_qty_from_sizing(
        broker_account_id=None, ticker="X", entry=10.0, stop=9.0, default=100))
    assert got == 100


def test_account_not_found_returns_default(monkeypatch):
    monkeypatch.setattr(appdb, "async_session_factory",
                        lambda: _FakeSession(None), raising=False)
    got = asyncio.run(sched._compute_qty_from_sizing(
        broker_account_id="missing", ticker="X", entry=10.0, stop=9.0, default=7))
    assert got == 7
