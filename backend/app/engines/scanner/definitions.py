"""Approved StrategyTemplate definitions (SCANNER-V1, P2).

In-code form of the signed-off specs (strategy_template_signoff.md). All ship
watch_only_default=True and enabled=False — they run in SHADOW and only fire live
after real stats clear the quality gate + an operator promotion. `approved=True`
reflects the user's 'approve all' sign-off. More templates from the doc are added
here incrementally; this is the first batch (liquid stock setups).
"""
from app.engines.scanner.templates import (
    StrategyTemplate, LiquidityReqs, StructureLevels, OptionsEligibility,
)

TEMPLATES: dict = {}


def _reg(t: StrategyTemplate) -> StrategyTemplate:
    TEMPLATES[t.key] = t
    return t


_reg(StrategyTemplate(
    key="high_relvol_breakout", display_name="High Relative-Volume Breakout",
    family="breakout", direction="long", hold_horizon="intraday",
    thesis="Unusual volume expansion through a clear resistance level; participation confirms the move.",
    daily_filters={"gap_min": 2.0, "gap_max": 30.0, "rel_vol_min": 3.0,
                   "price_min": 5.0, "price_max": 600.0, "dollar_vol_min": 20_000_000},
    liquidity=LiquidityReqs(min_price=5.0, max_price=600.0, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=20_000_000, max_spread_bps=30,
                            min_float=5_000_000, max_float=500_000_000, min_mktcap=300_000_000),
    atr_min_pct=2.0, atr_max_pct=9.0,
    confirmation=["close_above_resistance", "rel_vol>=3.0", "above_vwap", "breakout_bar>=1.5x"],
    levels=StructureLevels(entry_basis="breakout_of_prior_high", stop_basis="swing_low",
                           target_basis="measured_move", atr_period=14, atr_stop_mult=1.0, rr_ratio=2.0),
    options=OptionsEligibility(eligible=False),
    watch_only_default=True, validation_method="forward_test",
    min_score_consider=15.0, min_score_confirm=20.0, approved=True, enabled=False, version=1,
))

_reg(StrategyTemplate(
    key="momentum_breakout", display_name="Momentum Breakout (Rel-Vol Base Break)",
    family="momentum", direction="long", hold_horizon="intraday",
    thesis="High rel-vol break of a tight intraday base in a name already in motion.",
    daily_filters={"gap_min": 2.0, "gap_max": 25.0, "rel_vol_min": 2.5,
                   "price_min": 3.0, "price_max": 400.0, "dollar_vol_min": 20_000_000},
    liquidity=LiquidityReqs(min_price=3.0, max_price=400.0, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=20_000_000, max_spread_bps=30,
                            min_float=10_000_000, min_mktcap=300_000_000),
    atr_min_pct=2.0, atr_max_pct=12.0,
    confirmation=["close_above_pivot", "rel_vol>=2.5", "above_vwap", "tight_base"],
    levels=StructureLevels(entry_basis="breakout", stop_basis="swing_low",
                           target_basis="measured_move", atr_period=14, atr_stop_mult=1.0, rr_ratio=2.0),
    options=OptionsEligibility(eligible=False),
    watch_only_default=True, validation_method="forward_test",
    min_score_consider=15.0, min_score_confirm=20.0, approved=True, enabled=False, version=1,
))

_reg(StrategyTemplate(
    key="fiftytwo_wk_high_breakout", display_name="52-Week High Breakout",
    family="breakout", direction="long", hold_horizon="swing",
    thesis="Break to new 52-week highs on volume — accumulation with clean overhead.",
    daily_filters={"gap_min": 0.0, "gap_max": 20.0, "rel_vol_min": 2.0,
                   "price_min": 5.0, "price_max": 1000.0, "dollar_vol_min": 20_000_000},
    liquidity=LiquidityReqs(min_price=5.0, max_price=1000.0, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=20_000_000, max_spread_bps=25, min_mktcap=500_000_000),
    atr_min_pct=1.5, atr_max_pct=8.0,
    confirmation=["new_52wk_high", "vol_spike>=2x"],
    levels=StructureLevels(entry_basis="52wk_break", stop_basis="prior_day_low",
                           target_basis="measured_move", atr_period=14, atr_stop_mult=1.5, rr_ratio=2.5),
    options=OptionsEligibility(eligible=False),
    watch_only_default=True, validation_method="backtest_daily",
    min_score_consider=15.0, min_score_confirm=20.0, approved=True, enabled=False, version=1,
))


def approved_templates() -> list:
    return [t for t in TEMPLATES.values() if t.approved]
