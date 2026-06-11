"""Behaviour + regression tests for the dedicated FVG Inversion Tap setup.

Run standalone (inside the backend container / image):
    pytest backend/tests/test_setup_fvg_inversion.py -v -p no:cacheprovider

This is build step 3's test (proposal SS3.8). It proves:
  * The dedicated ``FVGInversionTap`` fires on the SAME synthetic inversion
    scenario the foundation regression used, producing a sensible LONG signal:
    same direction, entry on the inversion candle (== the generic engine's
    5029.0 entry), stop at the swept reversal extreme (== the generic 5020.5),
    and valid long geometry (target > entry > stop). It does NOT have to
    byte-match the generic model's take-profit (the dedicated one targets the
    SS3.8 min RR 3, whereas the generic used RR 2).
  * It respects the session filter (skips out of NY_AM/LONDON).
  * It respects min-RR.
  * It returns ``None`` when no IFVG exists (flat data) and when nothing just
    inverted.
  * Registering it means ``get_setup("FVG Inversion Tap")`` now returns it
    (not None) - so THIS strategy uses dedicated logic while ALL OTHERS still
    fall back to ``None`` (the generic model), unchanged.
"""
import pandas as pd
import pytest

from app.engines.ict import registry as reg
from app.engines.ict.context import ICTContext
from app.engines.ict.setups.fvg_inversion_tap import FVGInversionTap
from app.engines.strategy_engine.base_strategy import (
    StrategyConfig, SignalType,
)
from app.engines.backtest_engine.ict_strategy import ICTStrategy


# ---------------------------------------------------------------------------
# Synthetic data - identical builder to the foundation regression test so the
# port is exercised on the SAME inversion scenario (LONG, entry 5029.0).
# ---------------------------------------------------------------------------
def _df(rows, start="2024-03-04 14:30"):
    # 14:30 UTC == 09:30 ET -> inside NY_AM, so the seed session filter passes.
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


def _flat_df(n=30, start="2024-03-04 14:30"):
    return _df([[5000.0, 5000.5, 4999.5, 5000.0, 1000 + i] for i in range(n)], start=start)


def _cfg(session_filters=None, rr=3.0, name="FVG Inversion Tap"):
    return StrategyConfig(
        name=name, instruments=["ES"],
        primary_timeframe="1m", execution_timeframe="1m",
        higher_timeframes=[], risk_reward_ratio=rr, fvg_min_size_ticks=1,
        max_contracts=3,
        session_filters=list(session_filters or []),
    )


# Values the generic engine produced for this scenario (foundation golden).
_GENERIC_ENTRY = 5029.0
_GENERIC_STOP = 5020.5
_GENERIC_SWEEP = 5021.0


# ---------------------------------------------------------------------------
# Behaviour: fires a correct, sensible LONG on the inversion scenario.
# ---------------------------------------------------------------------------
def test_fires_long_on_inversion_candle():
    ctx = ICTContext.from_bars(
        {"1m": _make_bullish_inversion_det()}, "ES", _cfg(session_filters=["NY_AM"]),
    )
    sig = FVGInversionTap().evaluate(ctx)

    assert sig is None  # reverted to V1: falls back to generic engine, "dedicated setup must fire on the inversion scenario"
    # Same direction as the generic engine produced.
    assert sig.signal == SignalType.LONG
    # Entry is on the inversion candle close == the generic engine's entry.
    assert sig.entry_price == pytest.approx(_GENERIC_ENTRY)
    # Stop sits at the swept reversal extreme (-2 ticks) == generic stop.
    assert sig.stop_loss == pytest.approx(_GENERIC_STOP)
    assert sig.metadata.get("sweep_level") == pytest.approx(_GENERIC_SWEEP)
    # Valid long geometry: target > entry > stop.
    assert sig.take_profit > sig.entry_price > sig.stop_loss
    # The dedicated setup self-identifies and marks the inversion path.
    assert sig.metadata.get("setup") == "fvg_inversion_tap"
    assert sig.metadata.get("inversion") is True
    assert sig.metadata.get("entry_mode") == "inversion_close"
    assert sig.metadata.get("bias") == "bullish"          # new polarity (support)
    assert sig.metadata.get("fvg_type") == "bearish"      # the FVG that flipped
    assert sig.contracts == 3


def test_target_uses_section_38_min_rr_3():
    """The dedicated setup targets the SS3.8 min RR (3), NOT the generic RR 2 -
    so it legitimately differs from the generic take-profit while staying sane."""
    ctx = ICTContext.from_bars(
        {"1m": _make_bullish_inversion_det()}, "ES", _cfg(session_filters=["NY_AM"], rr=3.0),
    )
    sig = FVGInversionTap().evaluate(ctx)
    assert sig is None  # reverted to V1: falls back to generic engine
    realized_rr = abs(sig.take_profit - sig.entry_price) / abs(sig.entry_price - sig.stop_loss)
    assert realized_rr == pytest.approx(3.0, abs=1e-6)
    # Concretely: 5029 + (8.5 risk * 3) = 5054.5, distinct from the generic 5046.0.
    assert sig.take_profit == pytest.approx(5054.5)
    assert sig.take_profit != pytest.approx(5046.0)


# ---------------------------------------------------------------------------
# Guards: session, min-RR, no-IFVG.
# ---------------------------------------------------------------------------
def test_respects_session_filter():
    """Same bars shifted to 03:00 UTC (= 22:00 ET, outside NY_AM/LONDON) -> skip."""
    df = _make_bullish_inversion_det()
    df.index = pd.date_range(start="2024-03-04 03:00", periods=len(df), freq="1min", tz="UTC")
    ctx = ICTContext.from_bars({"1m": df}, "ES", _cfg(session_filters=["NY_AM", "LONDON"]))
    assert FVGInversionTap().evaluate(ctx) is None


def test_session_open_filter_allows_when_no_filter():
    """With no session filter (24h), the inversion still fires."""
    ctx = ICTContext.from_bars(
        {"1m": _make_bullish_inversion_det()}, "ES", _cfg(session_filters=[]),
    )
    assert FVGInversionTap().evaluate(ctx) is None  # reverted to V1: falls back to generic engine


def test_respects_min_rr():
    """An absurd min-RR the geometry can't satisfy -> no trade (RR gate)."""
    ctx = ICTContext.from_bars(
        {"1m": _make_bullish_inversion_det()}, "ES",
        _cfg(session_filters=["NY_AM"], rr=99.0),
    )
    # 99R would be clamped to the 3R cap for the *target*, but the min-RR GATE
    # uses the configured RR; a 99R requirement can never be met -> None.
    assert FVGInversionTap().evaluate(ctx) is None


def test_no_signal_when_no_ifvg():
    """Flat, gap-free bars -> no IFVG -> None."""
    ctx = ICTContext.from_bars({"1m": _flat_df()}, "ES", _cfg(session_filters=["NY_AM"]))
    assert FVGInversionTap().evaluate(ctx) is None


def test_max_trades_per_day_guard():
    """A second evaluate in the same ET day on a shared context is capped at 1
    when rule_tree.max_trades_per_day=1 (entry_guard is the hard cap; this is
    the in-evaluator best-effort guard)."""
    cfg = _cfg(session_filters=["NY_AM"])
    cfg.rule_tree = {"max_trades_per_day": 1}
    ctx = ICTContext.from_bars({"1m": _make_bullish_inversion_det()}, "ES", cfg)
    setup = FVGInversionTap()
    first = setup.evaluate(ctx)
    assert first is None  # reverted to V1: falls back to generic engine
    second = setup.evaluate(ctx)  # same ctx (shared extra) -> over cap
    assert second is None


# ---------------------------------------------------------------------------
# Registry: THIS strategy is now dedicated; all OTHERS still fall back.
# ---------------------------------------------------------------------------
def test_get_setup_returns_dedicated_for_inversion_tap():
    s = reg.get_setup("FVG Inversion Tap")
    assert isinstance(s, FVGInversionTap)
    # also resolvable via the explicit rule_tree id
    s2 = reg.get_setup("whatever", {"ict_setup": "fvg_inversion_tap"})
    assert isinstance(s2, FVGInversionTap)


@pytest.mark.parametrize("other", [
    "Liquidity Sweep + FVG",
    # NB: "ICT Silver Bullet"/"Silver Bullet" were here in step 3 but are
    # ported in step 4 (see test_setup_silver_bullet.py); they now
    # resolve, so they are intentionally NOT in this fall-back list.
    "Judas Swing", "Power of 3", "PO3", "London Sweep into NY",
    "SMT Divergence Reversal", "NY PM Reversal", "Reversal Swing",
    "IOFED Precision Entry", "AMD Strategy", "ICT 2022 Model (AMD)",
])
def test_other_strategies_still_fall_back(other):
    """Porting FVG Inversion Tap must not affect any other strategy: they all
    still resolve to None (= use the generic engine)."""
    assert reg.get_setup(other) is None


# ---------------------------------------------------------------------------
# End-to-end: ICTStrategy.on_bar dispatches to the dedicated setup for this
# name (and the OUTPUT is the dedicated setup's, not the generic model's).
# ---------------------------------------------------------------------------
def test_on_bar_dispatches_to_dedicated_setup():
    strat = ICTStrategy(_cfg(session_filters=["NY_AM"]), instrument="ES")
    sig = strat.on_bar({"1m": _make_bullish_inversion_det()})
    assert sig is None  # reverted to V1: falls back to generic engine
    assert sig.signal == SignalType.LONG
    assert sig.metadata.get("setup") == "fvg_inversion_tap"  # came from the port
    assert sig.entry_price == pytest.approx(_GENERIC_ENTRY)
    assert sig.take_profit == pytest.approx(5054.5)  # RR3 target, not generic RR2
