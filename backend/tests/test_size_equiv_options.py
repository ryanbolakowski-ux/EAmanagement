"""Equivalence gate for the options-paper sizing migration (#136).

Reimplements the ORIGINAL `_size_position` formula inline (`_old_size`) and
asserts the migrated method (which now delegates to `unified_size`) returns the
IDENTICAL contract count across a broad matrix of premiums, equities, risk %,
stop-loss %, and commissions. behavior_change = NONE.

Pure unit test: constructs the trader directly, never touches DB/network.
"""
import math
import pytest

from app.engines.options.options_paper import OptionsPaperTrader


def _old_size(entry_premium: float, *, equity: float, risk_pct: float,
              stop_loss_pct: float, commission: float) -> int:
    """Verbatim reimplementation of the pre-migration formula.

        loss_per_contract = premium*100*(stop_pct/100) + commission*2
        risk_dollars      = equity*(risk_pct/100)
        n = int(risk_dollars // loss_per_contract); bounded >= 0
    """
    if entry_premium <= 0:
        return 0
    loss_per_contract = entry_premium * 100 * (stop_loss_pct / 100.0) + (commission * 2)
    if loss_per_contract <= 0:
        return 0
    risk_dollars = equity * (risk_pct / 100.0)
    if risk_dollars <= 0:
        return 0
    n = int(risk_dollars // loss_per_contract)
    return max(0, n)


def _make_trader(*, equity, risk_pct, stop_loss_pct, commission):
    t = OptionsPaperTrader(
        underlying="SPY",
        chain=[],
        starting_balance=equity,
        risk_per_trade_pct=risk_pct,
        commission_per_contract=commission,
        stop_loss_premium_pct=stop_loss_pct,
    )
    # starting_balance seeds _equity; make sure the field reflects our case
    t._equity = float(equity)
    return t


# (premium, equity, risk_pct, stop_loss_pct, commission)
CASES = [
    (2.50, 10_000, 1.5, 50.0, 0.65),    # baseline defaults
    (0.50, 10_000, 1.5, 50.0, 0.65),    # cheap premium -> many contracts
    (12.00, 10_000, 1.5, 50.0, 0.65),   # pricey premium -> few/zero
    (2.50, 250_000, 2.0, 50.0, 0.65),   # large account
    (1.20, 50_000, 1.0, 30.0, 0.65),    # tighter stop %
    (1.20, 50_000, 1.0, 80.0, 0.65),    # wider stop %
    (3.33, 100_000, 0.5, 50.0, 0.00),   # zero commission
    (3.33, 100_000, 0.5, 50.0, 1.50),   # high commission
    (4.75, 7_500, 1.5, 50.0, 0.65),     # awkward floor boundary
    (0.07, 10_000, 1.5, 50.0, 0.65),    # very cheap premium
    (50.0, 10_000, 1.5, 50.0, 0.65),    # premium too big -> 0 contracts
    (2.50, 100, 1.5, 50.0, 0.65),       # tiny equity -> 0 contracts
    (1.00, 33_333, 1.0, 25.0, 0.65),    # non-round equity
    (2.50, 10_000, 3.0, 60.0, 0.65),    # higher risk %, wider stop
]


@pytest.mark.parametrize("premium,equity,risk_pct,stop_pct,comm", CASES)
def test_options_size_matches_old_formula(premium, equity, risk_pct, stop_pct, comm):
    trader = _make_trader(equity=equity, risk_pct=risk_pct,
                          stop_loss_pct=stop_pct, commission=comm)
    new = trader._size_position(premium)
    old = _old_size(premium, equity=equity, risk_pct=risk_pct,
                    stop_loss_pct=stop_pct, commission=comm)
    assert new == old, (
        f"MISMATCH premium={premium} equity={equity} risk={risk_pct}% "
        f"stop={stop_pct}% comm={comm}: new={new} old={old}"
    )


def test_zero_and_negative_premium_return_zero():
    trader = _make_trader(equity=10_000, risk_pct=1.5, stop_loss_pct=50.0, commission=0.65)
    assert trader._size_position(0.0) == 0
    assert trader._size_position(-1.0) == 0
    assert _old_size(0.0, equity=10_000, risk_pct=1.5, stop_loss_pct=50.0, commission=0.65) == 0


def test_dense_random_matrix_equivalence():
    """Brute-force many combinations to catch any floor-boundary drift."""
    premiums = [0.10, 0.37, 0.95, 1.50, 2.50, 3.80, 5.25, 9.99]
    equities = [500, 2_500, 10_000, 47_777, 100_000]
    risks = [0.5, 1.0, 1.5, 2.0]
    stops = [25.0, 40.0, 50.0, 75.0]
    comms = [0.0, 0.65, 1.50]
    for p in premiums:
        for e in equities:
            for rp in risks:
                for sp in stops:
                    for c in comms:
                        trader = _make_trader(equity=e, risk_pct=rp,
                                              stop_loss_pct=sp, commission=c)
                        new = trader._size_position(p)
                        old = _old_size(p, equity=e, risk_pct=rp,
                                        stop_loss_pct=sp, commission=c)
                        assert new == old, (
                            f"MISMATCH p={p} e={e} rp={rp} sp={sp} c={c}: "
                            f"new={new} old={old}"
                        )
