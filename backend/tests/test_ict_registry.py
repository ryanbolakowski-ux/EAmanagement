"""Tests for the ICT strategy registry + the SAFE FALLBACK property.

Run standalone (inside the backend container / image):
    pytest backend/tests/test_ict_registry.py -v -p no:cacheprovider

Covers:
  - get_setup returns None for an unknown / un-ported name (and for an
    unknown rule_tree['ict_setup'] override).
  - a registered dummy setup is returned by name and by rule_tree override,
    and its evaluate() is actually invoked.
  - FALLBACK REGRESSION: with NOTHING registered, ICTStrategy.on_bar produces
    EXACTLY the same signal as the generic model did before step 2 (a frozen
    golden value captured from the real engine) on a deterministic synthetic
    bar set - and the same None on the no-signal scenario. This is the
    non-negotiable "zero behavior change" guarantee.
"""
import numpy as np
import pandas as pd
import pytest

from app.engines.ict.base import ICTSetup
from app.engines.ict.context import ICTContext
from app.engines.ict import registry as reg
from app.engines.strategy_engine.base_strategy import (
    StrategyConfig, TradeSignal, SignalType,
)
from app.engines.backtest_engine.ict_strategy import ICTStrategy


# ---------------------------------------------------------------------------
# Registry isolation: snapshot & restore the global registry around each test
# so a dummy registration never leaks into the fallback regression test.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clean_registry():
    saved = dict(reg._REGISTRY)
    try:
        yield
    finally:
        reg._REGISTRY.clear()
        reg._REGISTRY.update(saved)


# ---------------------------------------------------------------------------
# get_setup fallback / resolution
# ---------------------------------------------------------------------------
def test_unknown_name_returns_none():
    assert reg.get_setup("Totally Unknown Strategy") is None
    assert reg.get_setup("") is None
    assert reg.get_setup(None) is None  # type: ignore[arg-type]


def test_unknown_rule_tree_override_returns_none():
    assert reg.get_setup("anything", {"ict_setup": "does_not_exist"}) is None


def test_registered_setup_resolved_and_evaluated():
    calls = {"n": 0}

    @reg.register("Dummy Setup")
    class _Dummy(ICTSetup):
        def evaluate(self, ctx):
            calls["n"] += 1
            return TradeSignal(
                signal=SignalType.LONG, instrument=ctx.instrument,
                entry_price=1.0, stop_loss=0.5, take_profit=2.0,
            )

    # Resolved by (normalized) name.
    s = reg.get_setup("dummy setup")
    assert isinstance(s, _Dummy)
    assert s.name == "dummy_setup"

    # Resolved by rule_tree override (takes precedence over the name).
    s2 = reg.get_setup("some other name", {"ict_setup": "Dummy Setup"})
    assert isinstance(s2, _Dummy)

    # evaluate() is actually invoked and returns the signal.
    ctx = ICTContext.from_bars(
        {"1m": _flat_df(20)}, "ES",
        StrategyConfig(name="x", instruments=["ES"]),
    )
    out = s.evaluate(ctx)
    assert out is not None and out.signal == SignalType.LONG
    assert calls["n"] == 1


def test_register_rejects_non_setup():
    with pytest.raises(TypeError):
        @reg.register("bad")
        class _NotASetup:  # noqa: D401 - not an ICTSetup
            pass


# ---------------------------------------------------------------------------
# Deterministic synthetic data (matches the probe used to capture the golden).
# ---------------------------------------------------------------------------
def _flat_df(n, start="2024-03-04 14:30"):
    idx = pd.date_range(start=start, periods=n, freq="1min", tz="UTC")
    rows = [[5000.0, 5000.5, 4999.5, 5000.0, 1000 + i] for i in range(n)]
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close", "volume"])


def _df(rows, start="2024-03-04 14:30"):
    idx = pd.date_range(start=start, periods=len(rows), freq="1min", tz="UTC")
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close", "volume"])


def _make_bullish_inversion_det(n=40):
    rows = []
    price = 5000.0
    for i in range(n - 8):
        o = price; c = o + 1.0; h = c + 0.5; l = o - 0.5
        rows.append([o, h, l, c, 1000 + i]); price = c
    c1o = price
    rows.append([c1o, c1o + 0.5, c1o - 6.0, c1o - 5.5, 2000])
    c2o = c1o - 5.5
    rows.append([c2o, c2o - 0.5, c2o - 5.0, c2o - 4.5, 2100])
    c3o = c2o - 4.5
    rows.append([c3o, c1o - 7.0, c3o - 1.0, c3o - 0.5, 2200])
    fvg_low = c1o - 7.0; fvg_high = c1o - 6.0
    sweep_o = c3o - 0.5
    rows.append([sweep_o, sweep_o + 0.5, fvg_low - 3.0, fvg_low - 2.5, 2300])
    inv_o = fvg_low - 2.5; inv_c = fvg_high + 3.0
    rows.append([inv_o, inv_c + 0.5, inv_o - 0.5, inv_c, 2500])
    return _df(rows)


def _core(sig):
    return {
        "signal": sig.signal.value,
        "entry_price": round(float(sig.entry_price), 4),
        "stop_loss": round(float(sig.stop_loss), 4),
        "take_profit": round(float(sig.take_profit), 4),
        "contracts": int(sig.contracts),
        "bias": sig.metadata.get("bias"),
        "fvg_type": sig.metadata.get("fvg_type"),
        "inversion": sig.metadata.get("inversion"),
        "sweep_level": (round(float(sig.metadata["sweep_level"]), 4)
                        if sig.metadata.get("sweep_level") is not None else None),
    }


def _run(rule_tree=None):
    cfg = StrategyConfig(
        name="Liquidity Sweep + FVG", instruments=["ES"],
        primary_timeframe="1m", execution_timeframe="1m",
        higher_timeframes=[], risk_reward_ratio=2.0, fvg_min_size_ticks=1,
        max_contracts=3,
    )
    if rule_tree is not None:
        cfg.rule_tree = rule_tree
    strat = ICTStrategy(cfg, instrument="ES")
    return strat.on_bar({"1m": _make_bullish_inversion_det()})


# Golden value captured from the REAL generic engine BEFORE step 2 wiring.
# If the fallback ever changes this, behavior has regressed -> test fails.
_GOLDEN_BYPASS = {
    "signal": "long",
    "entry_price": 5029.0,
    "stop_loss": 5020.5,
    "take_profit": 5046.0,
    "contracts": 3,
    "bias": "bullish",
    "fvg_type": "bearish",
    "inversion": True,
    "sweep_level": 5021.0,
}


def test_fallback_preserves_signal_unchanged():
    """With nothing registered, the bypass-gates inversion scenario must yield
    the exact same signal the generic model produced before the registry."""
    # Precondition: this strategy name is NOT ported, so dispatch falls back.
    assert reg.get_setup("Liquidity Sweep + FVG") is None

    sig = _run(rule_tree={"bypass_bias_gates": True})
    assert sig is not None, "expected the generic fallback to still fire a signal"
    assert _core(sig) == _GOLDEN_BYPASS


def test_fallback_preserves_no_signal_unchanged():
    """The default-gates scenario rejected (wrong PD zone) before step 2; the
    fallback must still return None - the no-signal path is preserved too."""
    assert reg.get_setup("Liquidity Sweep + FVG") is None
    sig = _run(rule_tree=None)
    assert sig is None


def test_registered_setup_short_circuits_generic():
    """When a setup IS registered for the name, on_bar must use it instead of
    the generic model (proves the dispatch branch is wired)."""
    sentinel = TradeSignal(
        signal=SignalType.SHORT, instrument="ES",
        entry_price=42.0, stop_loss=43.0, take_profit=40.0, contracts=1,
    )

    @reg.register("Liquidity Sweep + FVG")
    class _PortedDummy(ICTSetup):
        def evaluate(self, ctx):
            assert isinstance(ctx, ICTContext)
            assert ctx.instrument == "ES"
            return sentinel

    sig = _run(rule_tree=None)  # default scenario would be None via generic
    assert sig is sentinel  # but the dummy short-circuits and returns its own
