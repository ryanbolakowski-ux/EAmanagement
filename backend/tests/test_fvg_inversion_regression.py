"""Regression guard for FVG Inversion Tap.

Root cause of the 2026-06 result collapse: the strategy was silently migrated
from the proven **v1** ICT engine to a **v2** "rangeTP+vwap+be" engine
(engine_code_version 2026-06-19-rangeTP+vwap+be), changing the exit model
entirely — 91.7% / PF 5.41 / 228 break-evens  ->  30.3% / PF 1.14 / 0 break-evens.

Reference baseline (run 2ee0363f, NQ 15m, 2025-05-11..2026-05-11, v1 engine):
  527 trades | win_rate 91.65% | effective_wr 85.28% | breakeven 228
  PF 5.41 | net $1,800,429 | maxDD 1.6% | sharpe 9.33 | avg R:R 0.49

IMPORTANT honesty note baked into the asserts below: the 91.7% headline counts
the 228 break-evens (43% of trades) as non-losses. The true target-hit split is
~48% real wins / 43% scratches / 8% losses. Both rates are legitimate per the
house convention, but "91.7% win rate" is the BE-inclusive number, not the
target-hit rate.

These are pure-unit guards (no market data) so they run in CI and fail loudly if
the canonical config or the win-rate math ever drifts again.
"""
import pytest
from app.scripts.seed_strategies import SEED
from app.engines.backtest_engine.metrics import win_rate_stats

REFERENCE = {
    "total_trades": 527, "win_rate_pct": 91.65, "effective_wr_pct": 85.28,
    "breakeven": 228, "winning": 483, "losing": 44,
}


def _fvg():
    for t in SEED:
        if t["name"] == "FVG Inversion Tap":
            return t
    raise AssertionError("FVG Inversion Tap missing from the canonical SEED list")


def test_fvg_canonical_config_stays_on_proven_v1():
    """The canonical FVG Inversion Tap must stay on the proven v1 config and must
    NOT carry the v2 exit-model overrides that broke it (engine_version=v2,
    range take-profit, or a max-FVG-size filter the reference never had)."""
    f = _fvg()
    assert f["risk_reward_ratio"] == 3
    assert f["primary_timeframe"] == "15m"
    assert f["execution_timeframe"] == "1m"
    assert f["higher_timeframes"] == ["1H", "4H"]
    assert set(f["session_filters"]) == {"NY_AM", "LONDON"}
    rt = f.get("rule_tree") or {}
    assert str(rt.get("engine_version", "v1")).lower() == "v1", \
        "canonical FVG must default to the proven v1 engine, not v2 (the swap broke it)"
    assert f.get("take_profit_mode", "auto") in (None, "auto"), \
        "v1 reference used the swing/HTF-FVG/RR target hierarchy, not range-TP"
    assert "fvg_max_size_ticks" not in f, "v1 reference had NO max-FVG-size filter"


def test_win_rate_counts_breakevens_as_non_loss():
    """Pin the canonical win-rate math against the reference run so the metric
    definition can't silently change underneath the strategy."""
    s = win_rate_stats(winning_trades=REFERENCE["winning"],
                       losing_trades=REFERENCE["losing"],
                       breakeven_trades=REFERENCE["breakeven"])
    assert s["total_trades"] == REFERENCE["total_trades"]
    assert s["real_wins"] == REFERENCE["winning"] - REFERENCE["breakeven"]   # 255
    assert round(s["win_rate"] * 100, 2) == REFERENCE["win_rate_pct"]         # 91.65 (BE = non-loss)
    assert round(s["effective_win_rate"] * 100, 2) == REFERENCE["effective_wr_pct"]  # 85.28 (BE excluded)


def test_breakeven_cannot_exceed_wins():
    """A break-even is a kind of win in the headline; BE > wins would mean the
    metric pipeline drifted and the win rate is being double-counted."""
    with pytest.raises(AssertionError):
        win_rate_stats(winning_trades=10, losing_trades=5, breakeven_trades=11)
