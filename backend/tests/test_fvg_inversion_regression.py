"""Regression guard for FVG Inversion Tap.

What broke (2026-06): the strategy was silently migrated from the v1 ICT engine to
a v2 "rangeTP+vwap+be" engine, changing the exit model and collapsing the headline
metrics. The config has since been restored to v1.

What we also learned restoring it: the famous reference run (2ee0363f, 2026-05-11)
showing 527 trades / 91.65% WR / PF 5.41 / $1.8M / sharpe 9.33 / 228 break-evens
is NOT reproducible on today's engine. A fresh v1 run over the IDENTICAL config,
window (2025-05-11..2026-05-11) and data now yields:
    478 trades | 64.0% WR | PF 1.46 | $18.5k | sharpe 0.78 | 74 break-evens.
Same config + same data => the difference is the v1 ENGINE CODE, corrected after
2026-05-11 (the June overtrade / break-even / re-entry fixes cut BE-rescued trades
228 -> 74). The 91.7% / sharpe-9.33 figure was an artifact of the looser pre-June
engine (over-aggressive break-even scratching 43% of trades), NOT a robust edge.
Honest achievable v1 result: ~64% WR, PF ~1.46 (a modest positive edge).

These pure-unit guards (no market data) run in CI and fail loudly if:
  1. the canonical config drifts off the proven v1 engine again, or
  2. the win-rate FORMULA (break-even = non-loss) silently changes.
They intentionally do NOT assert the 91.7% strategy result — that number was an
engine artifact and must not be "locked in".
"""
import pytest
from app.scripts.seed_strategies import SEED
from app.engines.backtest_engine.metrics import win_rate_stats


def _fvg():
    for t in SEED:
        if t["name"] == "FVG Inversion Tap":
            return t
    raise AssertionError("FVG Inversion Tap missing from the canonical SEED list")


def test_fvg_canonical_config_stays_on_proven_v1():
    """Canonical FVG Inversion Tap must stay on v1 and must NOT carry the v2
    exit-model overrides (engine_version=v2, range take-profit, max-FVG filter)."""
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
        "v1 used the swing/HTF-FVG/RR target hierarchy, not range-TP"
    assert "fvg_max_size_ticks" not in f, "v1 reference had NO max-FVG-size filter"


def test_win_rate_formula_counts_breakevens_as_non_loss():
    """Pin the canonical win-rate FORMULA (independent of any engine/strategy):
    break-evens roll into the headline win_rate as non-losses, and are excluded
    from effective_win_rate. Uses the reference run's trade split to lock the math."""
    s = win_rate_stats(winning_trades=483, losing_trades=44, breakeven_trades=228)
    assert s["total_trades"] == 527
    assert s["real_wins"] == 255
    assert round(s["win_rate"] * 100, 2) == 91.65          # BE = non-loss
    assert round(s["effective_win_rate"] * 100, 2) == 85.28  # BE excluded


def test_breakeven_cannot_exceed_wins():
    with pytest.raises(AssertionError):
        win_rate_stats(winning_trades=10, losing_trades=5, breakeven_trades=11)
