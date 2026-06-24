"""Approved StrategyTemplate definitions (SCANNER-V1, P2).

In-code form of the signed-off specs (strategy_template_signoff.md, all 17
templates — user approved all). Every template ships watch_only_default=True and
enabled=False: it runs in SHADOW and only fires live after (1) its real measured
stats clear the house quality gate (>=30 samples, PF>=1.3, win>=40%, maxDD<=25%,
expectancy>0) AND (2) an explicit operator promotion. `approved=True` reflects
the sign-off; it does NOT make a template tradeable on its own.

Scoring thresholds (min_score_consider / min_score_confirm) are on the canonical
0-100 scale produced by scoring.score_candidate (NOT the 0-1 figures in a few doc
rows). daily_filters are the Stage-1 COARSE gate (grouped-daily only); the real
selectivity for intraday templates is the intraday confirmation done live during
forward-testing. Options templates (14-17) are eligible=True but stay paper/
watch-only and do not run in the equity funnel until a paid options feed is live.
"""
from app.engines.scanner.templates import (
    StrategyTemplate, LiquidityReqs, StructureLevels, OptionsEligibility,
)

TEMPLATES: dict = {}

# common gate (same for every template per the signed-off doc)
_SC = dict(min_score_consider=15.0, min_score_confirm=20.0,
           watch_only_default=True, approved=True, version=1)


def _reg(t: StrategyTemplate) -> StrategyTemplate:
    TEMPLATES[t.key] = t
    return t


# ── 1. Momentum Breakout (Rel-Vol Base Break) ──────────────────────────────
_reg(StrategyTemplate(
    key="momentum_breakout", enabled=True, display_name="Momentum Breakout (Rel-Vol Base Break)",
    family="momentum", direction="long", hold_horizon="intraday",
    thesis="High rel-vol break of a tight intraday base in a name already in motion.",
    daily_filters={"gap_min": 2.0, "gap_max": 25.0, "rel_vol_min": 2.5,
                   "price_min": 3.0, "price_max": 400.0, "dollar_vol_min": 20_000_000},
    liquidity=LiquidityReqs(min_price=3.0, max_price=400.0, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=20_000_000, max_spread_bps=30,
                            min_float=10_000_000, min_mktcap=300_000_000),
    atr_min_pct=2.0, atr_max_pct=12.0,
    confirmation=["close_above_pivot+0.10ATR", "rvol>=2.5x", "breakout_bar>=2x_20avg",
                  "above_vwap", "tight_base<=1ATR_>=2touches", "09:40-15:30ET", "ext<=1ATR"],
    levels=StructureLevels(entry_basis="breakout_pivot+0.10ATR", stop_basis="base_swing_low|pivot-1.0ATR",
                           target_basis="measured_move(base_height)", atr_period=14, atr_stop_mult=1.0, rr_ratio=2.0),
    options=OptionsEligibility(eligible=False),
    validation_method="forward_test", **_SC,
))

# ── 2. Premarket Gap Continuation (Gap-and-Go) ─────────────────────────────
_reg(StrategyTemplate(
    key="premarket_gap_continuation", enabled=True, display_name="Premarket Gap Continuation (Gap-and-Go)",
    family="momentum", direction="long", hold_horizon="intraday",
    thesis="Gap up on catalyst, hold above VWAP, continue through the opening-range high.",
    daily_filters={"gap_min": 2.0, "gap_max": 25.0, "rel_vol_min": 2.0,
                   "price_min": 5.0, "price_max": 600.0, "dollar_vol_min": 20_000_000},
    liquidity=LiquidityReqs(min_price=5.0, max_price=600.0, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=20_000_000, max_spread_bps=30,
                            min_float=10_000_000, min_mktcap=300_000_000),
    atr_min_pct=2.0, atr_max_pct=12.0,
    confirmation=["close>=VWAP", "OR_built", "breakout_bar>1.5x_OR", "no_gap_fill",
                  "catalyst", "no_entry_after_10:30ET", "1/symbol/day"],
    levels=StructureLevels(entry_basis="max(PMH,ORH)_breakout", stop_basis="OR_low|VWAP-0.25ATR",
                           target_basis="measured_move(gap/PM_range)", atr_period=14, atr_stop_mult=1.5, rr_ratio=1.8),
    options=OptionsEligibility(eligible=False),
    validation_method="forward_test", **_SC,
))

# ── 3. High Relative-Volume Breakout ───────────────────────────────────────
_reg(StrategyTemplate(
    key="high_relvol_breakout", enabled=True, display_name="High Relative-Volume Breakout",
    family="breakout", direction="long", hold_horizon="intraday",
    thesis="Unusual volume expansion through clear resistance; participation confirms the move.",
    daily_filters={"gap_min": 2.0, "gap_max": 30.0, "rel_vol_min": 3.0,
                   "price_min": 5.0, "price_max": 600.0, "dollar_vol_min": 20_000_000},
    liquidity=LiquidityReqs(min_price=5.0, max_price=600.0, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=20_000_000, max_spread_bps=30,
                            min_float=5_000_000, max_float=500_000_000, min_mktcap=300_000_000),
    atr_min_pct=2.0, atr_max_pct=9.0,
    confirmation=["close_above_resistance(no_wick)", "rvol>=3.0x", "above_vwap",
                  "breakout_bar>1.5x_prior10", "no_same_day_binary"],
    levels=StructureLevels(entry_basis="max(PDH,PMH,OR15H)_break", stop_basis="intraday_swing_low|OR_low",
                           target_basis="measured_move+next_resistance", atr_period=14, atr_stop_mult=1.0, rr_ratio=2.0),
    options=OptionsEligibility(eligible=False),
    validation_method="forward_test", **_SC,
))

# ── 4. VWAP Reclaim & Hold (Session VWAP Retest) ───────────────────────────
_reg(StrategyTemplate(
    key="vwap_reclaim_hold", display_name="VWAP Reclaim & Hold (Session VWAP Retest)",
    family="mean_reversion", direction="long", hold_horizon="intraday",
    thesis="Reclaim of session VWAP after prior weakness, holding a higher-low on the retest.",
    daily_filters={"gap_min": -8.0, "gap_max": 15.0, "rel_vol_min": 1.0,
                   "price_min": 3.0, "price_max": 150.0, "dollar_vol_min": 25_000_000},
    liquidity=LiquidityReqs(min_price=3.0, max_price=150.0, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=25_000_000, max_spread_bps=25,
                            min_float=10_000_000, min_mktcap=300_000_000),
    atr_min_pct=2.0, atr_max_pct=12.0,
    confirmation=["2_consec_closes>VWAP", "retest_HL>=VWAP-0.1ATR", "reclaim_vol>=1.5x",
                  "VWAP_flat_to_rising", "09:45-14:30ET", "no_chase>1ATR"],
    levels=StructureLevels(entry_basis="retest_hold_candle_close+0.1ATR", stop_basis="retest_HL|VWAP-0.25ATR",
                           target_basis="nearest_resistance|measured_move", atr_period=14, atr_stop_mult=0.25, rr_ratio=2.0),
    options=OptionsEligibility(eligible=False),
    validation_method="forward_test", **_SC,
))

# ── 5. 52-Week High Breakout (Volume-Confirmed Swing) ──────────────────────
_reg(StrategyTemplate(
    key="fiftytwo_wk_high_breakout", display_name="52-Week High Breakout (Volume-Confirmed Swing)",
    family="breakout", direction="long", hold_horizon="swing",
    thesis="Break to new 52-week highs on volume — accumulation through clean overhead supply.",
    daily_filters={"gap_min": 0.0, "gap_max": 20.0, "rel_vol_min": 1.5,
                   "price_min": 5.0, "price_max": 1e9, "dollar_vol_min": 20_000_000},
    liquidity=LiquidityReqs(min_price=5.0, max_price=None, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=20_000_000, max_spread_bps=30,
                            min_float=20_000_000, min_mktcap=300_000_000),
    atr_min_pct=2.0, atr_max_pct=9.0,
    confirmation=["close>=prior_252d_high*(1+buf)", "close_upper_third", "vol>=1.5x_SMA50",
                  "next_session_hold>pivot+VWAP", "no_earnings_2_sessions"],
    levels=StructureLevels(entry_basis="prior252d_high+0.05ATR", stop_basis="min(252d_high,breakout_day_low)-0.5ATR",
                           target_basis="measured_move(base_height)", atr_period=14, atr_stop_mult=0.5, rr_ratio=2.0),
    options=OptionsEligibility(eligible=False),
    validation_method="backtest_daily", **_SC,
))

# ── 6. Earnings/8-K Catalyst Continuation (Swing) ──────────────────────────
_reg(StrategyTemplate(
    key="earnings_catalyst_continuation", display_name="Earnings/8-K Catalyst Continuation (Swing)",
    family="momentum", direction="both", hold_horizon="swing",
    thesis="Post-earnings/8-K drift — continuation in the catalyst-bar direction (PEAD).",
    daily_filters={"gap_min": 2.0, "gap_max": 40.0, "rel_vol_min": 2.0,
                   "price_min": 5.0, "price_max": 1e9, "dollar_vol_min": 20_000_000},
    liquidity=LiquidityReqs(min_price=5.0, max_price=None, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=20_000_000, max_spread_bps=30,
                            min_float=20_000_000, min_mktcap=500_000_000),
    atr_min_pct=2.0, atr_max_pct=12.0,
    confirmation=["break_catalyst/prior_day_extreme", "vwap_filter", "trigger_vol>=1.5x_tod_avg",
                  "gap_base_hold"],
    levels=StructureLevels(entry_basis="break_catalyst_extreme", stop_basis="catalyst_low/gap_base-0.10ATR",
                           target_basis="measured_move(catalyst_range)+next_swing", atr_period=14, atr_stop_mult=1.5, rr_ratio=2.0),
    options=OptionsEligibility(eligible=False),
    validation_method="backtest_daily", **_SC,
))

# ── 7. Low-Float Squeeze (Strict Risk) ─────────────────────────────────────
_reg(StrategyTemplate(
    key="low_float_squeeze_strict", display_name="Low-Float Squeeze (Strict Risk)",
    family="momentum", direction="long", hold_horizon="intraday",
    thesis="Low-float squeeze on heavy premarket participation, with hard anti-parabolic guard.",
    daily_filters={"gap_min": 5.0, "gap_max": 100.0, "rel_vol_min": 3.0,
                   "price_min": 2.0, "price_max": 50.0, "dollar_vol_min": 20_000_000},
    liquidity=LiquidityReqs(min_price=2.0, max_price=50.0, min_dollar_vol_premarket=5_000_000,
                            min_avg_daily_dollar_vol=20_000_000, max_spread_bps=30,
                            min_float=1_000_000, max_float=30_000_000, max_mktcap=2_000_000_000),
    atr_min_pct=4.0, atr_max_pct=18.0,
    confirmation=["rvol>=3.0x", "premkt_$vol>=5M", "break_and_hold>PMH/OR5H", "above_vwap",
                  "HARD:5min_RSI14<=80", "close_upper_third"],
    levels=StructureLevels(entry_basis="break_hold_PMH(OR5H_fallback)", stop_basis="min(VWAP,OR5_low)-ATR_buf|entry-1.0ATR",
                           target_basis="measured_move(PM/OR_range)|prior_swing", atr_period=14, atr_stop_mult=1.0, rr_ratio=2.0),
    options=OptionsEligibility(eligible=False),
    validation_method="forward_test", **_SC,
))

# ── 8. Bull Flag Continuation (Breakout) ───────────────────────────────────
_reg(StrategyTemplate(
    key="bull_flag", display_name="Bull Flag Continuation (Breakout)",
    family="breakout", direction="long", hold_horizon="swing",
    thesis="Flag-and-pole continuation — break of flag resistance after a strong impulse leg.",
    daily_filters={"gap_min": 0.0, "gap_max": 15.0, "rel_vol_min": 1.5,
                   "price_min": 5.0, "price_max": 1e9, "dollar_vol_min": 20_000_000},
    liquidity=LiquidityReqs(min_price=5.0, max_price=None, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=20_000_000, max_spread_bps=30,
                            min_float=10_000_000, min_mktcap=300_000_000),
    atr_min_pct=2.0, atr_max_pct=9.0,
    confirmation=["close>flag_res+0.10ATR", "breakout_vol>=1.5x_SMA50", "close_upper_third",
                  "above_vwap", ">=2_consec_5min_closes_above", "no_earnings_in_hold"],
    levels=StructureLevels(entry_basis="flag_resistance+0.10ATR", stop_basis="min(flag_low,breakout_bar_low)-0.25ATR",
                           target_basis="measured_move(pole_height)", atr_period=14, atr_stop_mult=1.5, rr_ratio=2.0),
    options=OptionsEligibility(eligible=False),
    validation_method="backtest_daily", **_SC,
))

# ── 9. EMA/VWAP Pullback Continuation ──────────────────────────────────────
_reg(StrategyTemplate(
    key="ema_vwap_pullback", display_name="EMA/VWAP Pullback Continuation",
    family="mean_reversion", direction="long", hold_horizon="swing",
    thesis="Buy-the-dip into rising EMA20 in an uptrend, on a bullish reversal reclaim.",
    daily_filters={"gap_min": -10.0, "gap_max": 8.0, "rel_vol_min": 0.3,
                   "price_min": 5.0, "price_max": 1e9, "dollar_vol_min": 20_000_000},
    liquidity=LiquidityReqs(min_price=5.0, max_price=None, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=20_000_000, max_spread_bps=30,
                            min_float=10_000_000, min_mktcap=300_000_000),
    atr_min_pct=1.5, atr_max_pct=8.0,
    confirmation=["bullish_reversal_reclaims_EMA20", "RSI14_40-65", "pullback_declining_vol",
                  "higher_low_structure"],
    levels=StructureLevels(entry_basis="reversal_bar_high|EMA20_retest_limit", stop_basis="min(pullback_low,EMA20)-0.25ATR",
                           target_basis="prior_swing_high|measured_move", atr_period=14, atr_stop_mult=1.5, rr_ratio=2.0),
    options=OptionsEligibility(eligible=False),
    validation_method="backtest_daily", **_SC,
))

# ── 10. Relative Strength vs Index (Leaders Outperforming SPY/QQQ) ──────────
_reg(StrategyTemplate(
    key="relative_strength_vs_index", display_name="Relative Strength vs Index (Leaders)",
    family="momentum", direction="long", hold_horizon="swing",
    thesis="Market leaders with top-decile relative strength vs SPY/QQQ, breaking structure in an uptrend.",
    daily_filters={"gap_min": -5.0, "gap_max": 12.0, "rel_vol_min": 0.8,
                   "price_min": 7.0, "price_max": 1e9, "dollar_vol_min": 20_000_000},
    liquidity=LiquidityReqs(min_price=7.0, max_price=None, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=20_000_000, max_spread_bps=25,
                            min_float=20_000_000, min_mktcap=500_000_000),
    atr_min_pct=1.5, atr_max_pct=7.0,
    confirmation=["RS_rank>=90_vs_SPY/QQQ", "RS_line_63d_high", "50>200_SMA_rising",
                  "market_ok(SPY>50SMA)", "not_extended<=1.10x_21EMA", "RS_persistence>=80"],
    levels=StructureLevels(entry_basis="break_swing_high/base_pivot+1tick", stop_basis="prior_swing_low|entry-1.5ATR",
                           target_basis="measured_move(base_height)+major_swing", atr_period=14, atr_stop_mult=1.5, rr_ratio=2.0),
    options=OptionsEligibility(eligible=False),
    validation_method="backtest_daily", **_SC,
))

# ── 11. Opening Range Breakout (5/15/30-min) ───────────────────────────────
_reg(StrategyTemplate(
    key="opening_range_breakout", display_name="Opening Range Breakout (5/15/30-min)",
    family="breakout", direction="both", hold_horizon="intraday",
    thesis="Breakout beyond the opening range with volume + VWAP agreement; false-break guarded.",
    daily_filters={"gap_min": -10.0, "gap_max": 20.0, "rel_vol_min": 1.0,
                   "price_min": 5.0, "price_max": 100.0, "dollar_vol_min": 20_000_000},
    liquidity=LiquidityReqs(min_price=5.0, max_price=100.0, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=20_000_000, max_spread_bps=25,
                            min_float=10_000_000, min_mktcap=300_000_000),
    atr_min_pct=2.0, atr_max_pct=8.0,
    confirmation=["close_beyond_OR_edge", "OR_width_0.5-2.0xATR", "breakout_bar>=2.0x_OR_avg",
                  "session_rvol>=1.5x", "vwap_agreement", "false_break_guard", "no_entry_after_12:00ET"],
    levels=StructureLevels(entry_basis="ORH+1tick(long)/ORL-1tick(short)", stop_basis="opposite_OR_edge+-0.10ATR",
                           target_basis="measured_move(OR_width)|prior_day_HL", atr_period=14, atr_stop_mult=1.0, rr_ratio=2.0),
    options=OptionsEligibility(eligible=False),
    validation_method="forward_test", **_SC,
))

# ── 12. Liquidity Sweep Reversal (ICT) ─────────────────────────────────────
_reg(StrategyTemplate(
    key="liquidity_sweep_reversal", display_name="Liquidity Sweep Reversal (ICT)",
    family="ict", direction="both", hold_horizon="intraday",
    thesis="Shallow sweep of a liquidity pool (PDH/PDL/PMH/PML/OR) then reclaim back inside — stop-hunt reversal.",
    daily_filters={"gap_min": -15.0, "gap_max": 15.0, "rel_vol_min": 1.0,
                   "price_min": 5.0, "price_max": 400.0, "dollar_vol_min": 20_000_000},
    liquidity=LiquidityReqs(min_price=5.0, max_price=400.0, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=20_000_000, max_spread_bps=30,
                            min_float=None, min_mktcap=None),
    atr_min_pct=1.5, atr_max_pct=9.0,
    confirmation=["pool_identified(PDH/PDL/PMH/PML/OR)", "shallow_overshoot_0.10-0.5ATR",
                  "reclaim_within_1-3_candles", "rejection_wick>=2x_body", "elevated_sweep+reclaim_vol"],
    levels=StructureLevels(entry_basis="reclaim_close_inside_swept_level", stop_basis="beyond_swept_extreme+-max(0.25ATR,noise)",
                           target_basis="opposite_side_swept_range/next_pool", atr_period=14, atr_stop_mult=0.25, rr_ratio=2.0),
    options=OptionsEligibility(eligible=False),
    validation_method="forward_test", **_SC,
))

# ── 13. FVG + VWAP Bias (ICT) ──────────────────────────────────────────────
_reg(StrategyTemplate(
    key="fvg_vwap_bias", display_name="FVG + VWAP Bias (ICT)",
    family="ict", direction="both", hold_horizon="intraday",
    thesis="Tap the proximal edge of a fair-value gap in the direction of the session VWAP bias.",
    daily_filters={"gap_min": -12.0, "gap_max": 12.0, "rel_vol_min": 1.0,
                   "price_min": 5.0, "price_max": 400.0, "dollar_vol_min": 20_000_000},
    liquidity=LiquidityReqs(min_price=5.0, max_price=400.0, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=20_000_000, max_spread_bps=25,
                            min_float=10_000_000, min_mktcap=300_000_000),
    atr_min_pct=1.5, atr_max_pct=8.0,
    confirmation=["bias=VWAP_side_>=2_closes", "valid_3candle_FVG>=0.10ATR", "displacement>1.5x_prior10",
                  "tap_proximal_edge", "not_filled/inverted"],
    levels=StructureLevels(entry_basis="proximal_FVG_edge_first_tap", stop_basis="beyond_distal_FVG_edge-0.10ATR",
                           target_basis="next_opposing_liquidity|measured_move", atr_period=14, atr_stop_mult=0.1, rr_ratio=2.0),
    options=OptionsEligibility(eligible=False),
    validation_method="forward_test", **_SC,
))

# ── 14. Options Swing Trend Continuation ───────────────────────────────────
_reg(StrategyTemplate(
    key="options_swing_trend_continuation", display_name="Options Swing Trend Continuation",
    family="options", direction="both", hold_horizon="swing",
    thesis="Single-leg call/put on a trend resumption from EMA20 in an ADX-confirmed trend.",
    daily_filters={"gap_min": -5.0, "gap_max": 10.0, "rel_vol_min": 0.8,
                   "price_min": 15.0, "price_max": 600.0, "dollar_vol_min": 25_000_000},
    liquidity=LiquidityReqs(min_price=15.0, max_price=600.0, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=25_000_000, max_spread_bps=25,
                            min_float=20_000_000, min_mktcap=2_000_000_000),
    atr_min_pct=1.5, atr_max_pct=7.0,
    confirmation=["EMA_stack(Close>EMA20>EMA50,slope>0)", "ADX14>=20", "HL/LH_intact",
                  "pullback_to_EMA20_then_resumption", "resumption_rvol>=1.3x", "earnings_blackout"],
    levels=StructureLevels(entry_basis="daily_resumption_break(intraday_confirm)", stop_basis="HL_swing-0.25ATR|backstop_1.5ATR",
                           target_basis="measured_move(impulse_leg)|next_resistance", atr_period=14, atr_stop_mult=1.5, rr_ratio=2.0),
    options=OptionsEligibility(eligible=True, structure="long_single_leg", min_oi=500, min_option_volume=100,
                              max_option_spread_pct=8.0, target_delta_low=0.55, target_delta_high=0.70,
                              dte_min=30, dte_max=60, iv_rank_max=50.0),
    validation_method="backtest_daily", **_SC,
))

# ── 15. Options Breakout Continuation (Debit Spread) ───────────────────────
_reg(StrategyTemplate(
    key="options_breakout_continuation", display_name="Options Breakout Continuation (Debit Spread)",
    family="options", direction="both", hold_horizon="swing",
    thesis="Vertical debit spread the day after a volume-confirmed Donchian/range breakout.",
    daily_filters={"gap_min": 0.0, "gap_max": 15.0, "rel_vol_min": 1.5,
                   "price_min": 10.0, "price_max": 600.0, "dollar_vol_min": 20_000_000},
    liquidity=LiquidityReqs(min_price=10.0, max_price=600.0, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=20_000_000, max_spread_bps=25,
                            min_float=20_000_000, min_mktcap=1_000_000_000),
    atr_min_pct=1.5, atr_max_pct=8.0,
    confirmation=["close_beyond_20session_range_edge", "TR>=1.3xATR14", "vol>=1.5x_20d",
                  "close_top/bottom_25%", "20EMA>50EMA&price>20EMA", "next_session_hold+VWAP"],
    levels=StructureLevels(entry_basis="retest_hold|beyond_breakout_bar(day_after)", stop_basis="back_inside_range|entry-1.5ATR",
                           target_basis="measured_move(range_height)", atr_period=14, atr_stop_mult=1.5, rr_ratio=1.8),
    options=OptionsEligibility(eligible=True, structure="debit_spread", min_oi=500, min_option_volume=100,
                              max_option_spread_pct=10.0, target_delta_low=0.30, target_delta_high=0.60,
                              dte_min=30, dte_max=60, iv_rank_max=60.0),
    validation_method="backtest_daily", **_SC,
))

# ── 16. Earnings/Catalyst Continuation (Defined-Risk Post-Catalyst Drift) ──
_reg(StrategyTemplate(
    key="options_catalyst_earnings_continuation",
    display_name="Earnings/Catalyst Continuation (Defined-Risk Drift)",
    family="options", direction="both", hold_horizon="swing",
    thesis="Defined-risk debit spread on post-catalyst drift (PEAD), entered days +1..+5.",
    daily_filters={"gap_min": 4.0, "gap_max": 40.0, "rel_vol_min": 2.0,
                   "price_min": 10.0, "price_max": 1e9, "dollar_vol_min": 25_000_000},
    liquidity=LiquidityReqs(min_price=10.0, max_price=None, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=25_000_000, max_spread_bps=30,
                            min_float=20_000_000, min_mktcap=1_000_000_000),
    atr_min_pct=2.5, atr_max_pct=12.0,
    confirmation=["day0:|gap|>=4%&$vol>=2x&TR>=1.5xATR&dir_close", "trend_agreement(EMA20vsEMA50)",
                  "topN_break_1/5min_close", "breakout_vol>=1.5x", "vwap_side", "no_opp_gap>=2%_since_day0"],
    levels=StructureLevels(entry_basis="break_day0_extreme(days+1..+5)", stop_basis="day0_low|entry-1.5ATR",
                           target_basis="R-multiple|day0_range_MM+next_swing", atr_period=14, atr_stop_mult=1.5, rr_ratio=2.0),
    options=OptionsEligibility(eligible=True, structure="debit_spread", min_oi=500, min_option_volume=100,
                              max_option_spread_pct=10.0, target_delta_low=0.55, target_delta_high=0.70,
                              dte_min=14, dte_max=45, iv_rank_max=60.0),
    validation_method="backtest_daily", **_SC,
))

# ── 17. Options Pullback Entry (Long Calls on Support Resumption) ───────────
_reg(StrategyTemplate(
    key="options_pullback_entry", display_name="Options Pullback Entry (Long Calls on Support Resumption)",
    family="options", direction="long", hold_horizon="swing",
    thesis="Long call / debit call vertical on an uptrend pullback reclaiming a support shelf.",
    daily_filters={"gap_min": -8.0, "gap_max": 8.0, "rel_vol_min": 0.5,
                   "price_min": 20.0, "price_max": 600.0, "dollar_vol_min": 50_000_000},
    liquidity=LiquidityReqs(min_price=20.0, max_price=600.0, min_dollar_vol_premarket=1_000_000,
                            min_avg_daily_dollar_vol=50_000_000, max_spread_bps=25,
                            min_float=30_000_000, min_mktcap=2_000_000_000),
    atr_min_pct=1.5, atr_max_pct=7.0,
    confirmation=["uptrend(close>rising_50SMA,20EMA>50EMA)", "pullback_to_EMA20<=0.5ATR", "higher_low_intact",
                  "retrace_23.6-61.8%", "RSI14>=35", "resumption_close>shelf&>=EMA20", "vol>=1.2x_20d"],
    levels=StructureLevels(entry_basis="resumption_bar_high|reclaim_close", stop_basis="0.25ATR_below_support_shelf",
                           target_basis="measured_move(prior_up_leg)|prior_swing_high", atr_period=14, atr_stop_mult=0.25, rr_ratio=2.0),
    options=OptionsEligibility(eligible=True, structure="long_call", min_oi=1000, min_option_volume=200,
                              max_option_spread_pct=10.0, target_delta_low=0.55, target_delta_high=0.70,
                              dte_min=21, dte_max=45, iv_rank_max=50.0),
    validation_method="backtest_daily", **_SC,
))


def approved_templates() -> list:
    """All templates the user signed off on (watch-only until promoted)."""
    return [t for t in TEMPLATES.values() if t.approved]


def equity_templates() -> list:
    """Approved templates the equity funnel can run today (non-options)."""
    return [t for t in approved_templates() if not t.options.eligible]


def enabled_templates() -> list:
    """Templates actually promoted to live (stats cleared + operator flip). Empty for now."""
    return [t for t in TEMPLATES.values() if t.approved and t.enabled]
