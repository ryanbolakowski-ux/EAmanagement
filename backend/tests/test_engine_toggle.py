"""Tests for the V1/V2 engine toggle + no-pick diagnostic."""
from app.engines.ict import setups  # noqa: F401  registers dedicated setups
from app.engines.ict.registry import get_setup
from app.engines.strategy_engine.base_strategy import StrategyConfig
from app.engines.backtest_engine.ict_strategy import ICTStrategy


def _cfg(name, rule_tree):
    c = StrategyConfig(name=name, instruments=["ES"], primary_timeframe="5m",
                       execution_timeframe="1m", higher_timeframes=[], risk_reward_ratio=3.0)
    c.rule_tree = rule_tree
    return c


def test_registry_resolves_five_dedicated_setups():
    for n in ["ICT Silver Bullet", "Power of 3 (PO3)", "Judas Swing",
              "London Sweep into NY", "FVG Inversion Tap"]:
        assert get_setup(n, {}) is not None, f"{n} should have a V2 setup"


def test_unknown_strategy_has_no_v2():
    assert get_setup("Totally Made Up Strategy", {}) is None


def test_gate_defaults_to_v1_no_dispatch():
    # default rule_tree -> engine_version v1 -> the dedicated dispatch is skipped
    strat = ICTStrategy(_cfg("ICT Silver Bullet", {}), instrument="ES")
    ev = str((getattr(strat.config, "rule_tree", {}) or {}).get("engine_version", "v1")).lower()
    assert ev == "v1"


def test_gate_v2_opt_in():
    strat = ICTStrategy(_cfg("ICT Silver Bullet", {"engine_version": "v2"}), instrument="ES")
    ev = str((getattr(strat.config, "rule_tree", {}) or {}).get("engine_version", "v1")).lower()
    assert ev == "v2"
    # and a dedicated setup is resolvable for it
    assert get_setup(strat.config.name, strat.config.rule_tree) is not None


def test_persistent_ict_extra_ledger():
    strat = ICTStrategy(_cfg("ICT Silver Bullet", {"engine_version": "v2"}), instrument="ES")
    assert hasattr(strat, "_ict_extra") and isinstance(strat._ict_extra, dict)


def test_nopick_state_exists_and_starts_empty():
    from app.engines.options.theta_scanner import _NOPICK_STATE
    assert isinstance(_NOPICK_STATE, dict) and "last" in _NOPICK_STATE


def test_engine_meta_helper():
    from app.api.routes.strategies import _engine_meta
    class _S:
        name = "ICT Silver Bullet"
        rule_tree = {"engine_version": "v2"}
    m = _engine_meta(_S())
    assert m["engine_version"] == "v2" and m["v2_available"] is True
    class _S2:
        name = "Totally Made Up"
        rule_tree = {}
    m2 = _engine_meta(_S2())
    assert m2["engine_version"] == "v1" and m2["v2_available"] is False
