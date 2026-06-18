"""Equivalence gate for the live-futures sizing migration (#136).

_pick_contract_size on LiveTrader was migrated to delegate its per-contract
size math to app.core.sizing.unified_size. This test pins behaviour:

(a) FALLBACK CASE — when no account risk attrs (_risk_per_trade_usd /
    _risk_per_trade_pct / _equity) are wired, the migrated method must return
    the SAME size as the OLD inline $200 formula across many cases.

(b) ACCOUNT-SETTINGS CASE — when _risk_per_trade_usd is wired, the method must
    size per the account's risk budget (risk_usd / risk_per_contract), capped.

The OLD formula is REIMPLEMENTED inline here (not imported) so the test is a
true equivalence check, independent of the production code.
"""
import types
import pytest

from app.engines.live_trading.live_trader import LiveTrader


# ── OLD formula, reimplemented inline (the pre-migration behaviour) ──
def old_pick_contract_size(entry, stop, tick_size, tick_value, cap, max_risk=200.0):
    if entry == stop or tick_size <= 0 or tick_value <= 0:
        return 0
    risk_per_contract = abs(entry - stop) / tick_size * tick_value
    if risk_per_contract <= 0:
        return 0
    sized = int(max_risk // risk_per_contract)
    return max(0, min(cap, sized))


def make_trader(**attrs):
    """A bare object the unbound _pick_contract_size can run against.
    The method only ever reads attrs via getattr(self, ...), so a plain
    namespace is sufficient and avoids LiveTrader.__init__ (broker, etc.)."""
    t = types.SimpleNamespace(**attrs)
    # Bind the real production method to our stub instance.
    t._pick_contract_size = types.MethodType(
        LiveTrader._pick_contract_size, t)
    return t


# entry, stop, tick_size, tick_value, cap  — mix of ES/NQ/micro economics
CASES = [
    (5000.0, 4990.0, 0.25, 12.50, 10),   # ES: 10pt stop -> risk/contract 500 -> 0
    (5000.0, 4999.0, 0.25, 12.50, 10),   # ES: 1pt stop  -> risk/contract 50  -> 4 (cap 10)
    (15000.0, 14990.0, 0.25, 5.00, 20),  # NQ: 10pt stop -> risk/contract 200 -> 1
    (15000.0, 14998.0, 0.25, 5.00, 20),  # NQ: 2pt stop  -> risk/contract 40  -> 5
    (15000.0, 14999.50, 0.25, 0.50, 50), # MNQ micro: risk/contract 1.0 -> 200 -> cap 50
    (100.0, 99.0, 0.25, 12.50, 3),       # tiny stop, low cap -> cap binds
    (4500.25, 4500.25, 0.25, 12.50, 10), # entry == stop -> 0
]


@pytest.mark.parametrize("entry,stop,ts,tv,cap", CASES)
def test_fallback_matches_old_200(entry, stop, ts, tv, cap):
    """No account attrs wired -> migrated method == OLD $200 formula."""
    t = make_trader()  # no _risk_per_trade_* / _equity, no session_risk_per_trade
    got = t._pick_contract_size(entry, stop, ts, tv, cap)
    exp = old_pick_contract_size(entry, stop, ts, tv, cap, max_risk=200.0)
    assert got == exp, f"{entry}/{stop} cap={cap}: got {got} expected {exp}"


@pytest.mark.parametrize("entry,stop,ts,tv,cap", CASES)
def test_fallback_respects_session_risk_override(entry, stop, ts, tv, cap):
    """session_risk_per_trade still honoured as the fallback budget (e.g. $350)."""
    t = make_trader(session_risk_per_trade=350.0)
    got = t._pick_contract_size(entry, stop, ts, tv, cap)
    exp = old_pick_contract_size(entry, stop, ts, tv, cap, max_risk=350.0)
    assert got == exp, f"{entry}/{stop} cap={cap}: got {got} expected {exp}"


@pytest.mark.parametrize("entry,stop,ts,tv,cap,risk_usd", [
    (15000.0, 14990.0, 0.25, 5.00, 20, 500.0),   # risk/contract 200 -> 500/200 = 2
    (15000.0, 14998.0, 0.25, 5.00, 20, 500.0),   # risk/contract 40  -> 500/40 = 12
    (5000.0, 4999.0, 0.25, 12.50, 10, 1000.0),   # risk/contract 50  -> 1000/50 = 20 -> cap 10
    (5000.0, 4990.0, 0.25, 12.50, 50, 500.0),    # risk/contract 500 -> 500/500 = 1
])
def test_account_risk_usd_sizes_per_budget(entry, stop, ts, tv, cap, risk_usd):
    """_risk_per_trade_usd wired -> size = floor(risk_usd / risk_per_contract), capped."""
    t = make_trader(_risk_per_trade_usd=risk_usd)
    got = t._pick_contract_size(entry, stop, ts, tv, cap)
    risk_per_contract = abs(entry - stop) / ts * tv
    exp = max(0, min(cap, int(risk_usd // risk_per_contract)))
    assert got == exp, f"risk_usd={risk_usd}: got {got} expected {exp}"


def test_account_risk_usd_overrides_200_default():
    """With risk_usd=500 the size is strictly larger than the $200 fallback."""
    entry, stop, ts, tv, cap = 15000.0, 14998.0, 0.25, 5.00, 50
    wired = make_trader(_risk_per_trade_usd=500.0)._pick_contract_size(entry, stop, ts, tv, cap)
    fallback = old_pick_contract_size(entry, stop, ts, tv, cap, max_risk=200.0)
    assert wired == 12          # 500 / 40
    assert fallback == 5        # 200 / 40
    assert wired > fallback


def test_account_risk_pct_sizes_per_equity():
    """_risk_per_trade_pct + _equity wired -> budget = equity * pct/100."""
    entry, stop, ts, tv, cap = 15000.0, 14998.0, 0.25, 5.00, 50
    # 1% of 50_000 = 500 budget; risk/contract = 40 -> 12 contracts
    t = make_trader(_risk_per_trade_pct=1.0, _equity=50_000.0)
    got = t._pick_contract_size(entry, stop, ts, tv, cap)
    assert got == 12


def test_invalid_inputs_return_zero():
    t = make_trader(_risk_per_trade_usd=500.0)
    assert t._pick_contract_size(5000.0, 5000.0, 0.25, 12.50, 10) == 0   # entry==stop
    assert t._pick_contract_size(5000.0, 4990.0, 0.0, 12.50, 10) == 0     # tick_size<=0
    assert t._pick_contract_size(5000.0, 4990.0, 0.25, 0.0, 10) == 0      # tick_value<=0
