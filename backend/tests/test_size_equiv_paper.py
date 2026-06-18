"""Equivalence gate for the paper-futures sizing migration (#136).

`PaperTrader._pick_contract_size` was rewritten to delegate its per-contract
size math to `app.core.sizing.unified_size`. This test reimplements the ORIGINAL
formula inline and asserts the migrated method returns the SAME contract count
across many (entry, stop, equity, risk_pct, caps, instrument) combinations.

behavior_change = NONE — this is a pure refactor guard.

We bind the real (unbound) method to a minimal shim carrying only the three
instance attributes it reads, so we exercise the actual migrated code path
without constructing the full PaperTrader (redis/async deps).
"""
from types import SimpleNamespace

from app.engines.paper_trading.paper_trader import PaperTrader


# ── OLD formula, reimplemented inline (the pre-migration body) ──────────────
def old_pick_contract_size(equity, risk_pct, commission, max_cap,
                           entry, stop, tick_size, tick_value, strategy_cap):
    stop_dist_ticks = abs(entry - stop) / tick_size
    if stop_dist_ticks <= 0:
        return 0
    loss_per_contract = stop_dist_ticks * tick_value + (commission * 2)
    if loss_per_contract <= 0:
        return 0
    risk_dollars = equity * (risk_pct / 100.0)
    if risk_dollars <= 0:
        return 0
    raw = int(risk_dollars // loss_per_contract)
    return max(0, min(raw, strategy_cap, max_cap))


def new_pick_contract_size(equity, risk_pct, commission, max_cap,
                           entry, stop, tick_size, tick_value, strategy_cap):
    shim = SimpleNamespace(
        _equity=float(equity),
        _risk_per_trade_pct=float(risk_pct),
        _max_contracts_cap=int(max_cap),
        commission=float(commission),
    )
    # Call the real migrated method, unbound, against the shim.
    return PaperTrader._pick_contract_size(
        shim, entry, stop, tick_size, tick_value, strategy_cap)


# (tick_size, tick_value) per instrument, mirroring the engine tables.
ES  = (0.25, 12.50)   # point_value 50
NQ  = (0.25, 5.00)    # point_value 20
MNQ = (0.25, 0.50)    # point_value 2

# Each case: (label, equity, risk_pct, commission, max_cap,
#             entry, stop, instrument, strategy_cap)
CASES = [
    # ES, normal sizing, cap not binding
    ("ES base",          100_000, 1.0, 2.09, 100, 5000.0, 4990.0, ES, 50),
    # ES, strategy_cap binds
    ("ES strat-cap",     500_000, 2.0, 2.09, 100, 5000.0, 4995.0, ES, 3),
    # ES, max_contracts_cap binds (small strategy_cap larger than max)
    ("ES maxcap",        500_000, 5.0, 2.09, 5,   5000.0, 4998.0, ES, 80),
    # ES, account too small -> zero
    ("ES too-small",     2_000,   1.0, 2.09, 100, 5000.0, 4900.0, ES, 50),
    # NQ, normal
    ("NQ base",          100_000, 1.0, 2.09, 100, 18000.0, 17950.0, NQ, 50),
    # NQ, wide stop, zero risk dollars edge via tiny equity
    ("NQ wide-stop",     50_000,  0.5, 2.09, 100, 18000.0, 17800.0, NQ, 50),
    # NQ, big risk pct, cap binds at max
    ("NQ maxcap",        1_000_000, 10.0, 2.09, 8, 18000.0, 17990.0, NQ, 100),
    # MNQ, micro, small account still sizes several
    ("MNQ base",         10_000,  1.0, 0.52, 100, 18000.0, 17980.0, MNQ, 100),
    # MNQ, account very small -> 1 or few
    ("MNQ tiny",         1_500,   1.0, 0.52, 100, 18000.0, 17900.0, MNQ, 100),
    # ES, zero commission
    ("ES nocomm",        250_000, 1.0, 0.0,  100, 5000.0, 4980.0, ES, 50),
    # ES, fractional contract floors to exact int boundary
    ("ES floor",         63_000,  1.0, 0.0,  100, 5000.0, 4990.0, ES, 50),
    # NQ, strategy_cap = 1
    ("NQ cap1",          200_000, 3.0, 2.09, 1,  18000.0, 17900.0, NQ, 50),
    # ES, short side (stop above entry) — abs() distance
    ("ES short",         100_000, 1.0, 2.09, 100, 4990.0, 5000.0, ES, 50),
    # MNQ, large equity, max_cap binds high
    ("MNQ maxcap",       5_000_000, 1.0, 0.52, 40, 18000.0, 17995.0, MNQ, 1000),
]


def test_paper_sizing_equivalence():
    mismatches = []
    for label, eq, pct, comm, maxc, entry, stop, instr, scap in CASES:
        ts, tv = instr
        old = old_pick_contract_size(eq, pct, comm, maxc, entry, stop, ts, tv, scap)
        new = new_pick_contract_size(eq, pct, comm, maxc, entry, stop, ts, tv, scap)
        if old != new:
            mismatches.append(f"{label}: old={old} new={new}")
    assert not mismatches, "Sizing diverged:\n" + "\n".join(mismatches)


def test_at_least_one_nonzero_and_one_capped():
    # Sanity: the suite actually exercises real sizing, not all-zero.
    results = []
    for label, eq, pct, comm, maxc, entry, stop, instr, scap in CASES:
        ts, tv = instr
        results.append(new_pick_contract_size(eq, pct, comm, maxc, entry, stop, ts, tv, scap))
    assert any(r > 0 for r in results)
    assert any(r == 0 for r in results)  # the too-small case
