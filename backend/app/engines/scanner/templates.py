"""Strategy-template data model for the multi-strategy scanner (SCANNER-V1).

`StrategyTemplate` is the in-code form of an approved spec from
strategy_template_signoff.md. Every template ships `watch_only_default=True` and
`approved=False`/`enabled=False` until a human signs it off and its real stats
clear the quality gate — nothing here fires a live pick on its own.

`TemplateHit` unifies the existing STTHit / MomentumHit / ScannerHit shapes so a
single emit path can render any scanner's output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class LiquidityReqs:
    min_price: float = 5.0
    max_price: Optional[float] = None
    min_dollar_vol_premarket: float = 1_000_000.0
    min_avg_daily_dollar_vol: float = 20_000_000.0
    max_spread_bps: Optional[float] = 30.0
    min_float: Optional[float] = None
    max_float: Optional[float] = None
    min_mktcap: Optional[float] = None
    max_mktcap: Optional[float] = None


@dataclass(frozen=True)
class StructureLevels:
    entry_basis: str = "breakout"          # how entry is set
    stop_basis: str = "swing_low"          # structural stop reference
    target_basis: str = "measured_move"    # structural target reference
    atr_period: int = 14
    atr_stop_mult: float = 1.0
    rr_ratio: float = 2.0


@dataclass(frozen=True)
class OptionsEligibility:
    eligible: bool = False
    structure: Optional[str] = None        # long_call | long_put | debit_spread | ...
    min_oi: int = 0
    min_option_volume: int = 0
    max_option_spread_pct: Optional[float] = None
    target_delta_low: Optional[float] = None
    target_delta_high: Optional[float] = None
    dte_min: Optional[int] = None
    dte_max: Optional[int] = None
    iv_rank_max: Optional[float] = None


@dataclass(frozen=True)
class StrategyTemplate:
    key: str
    display_name: str
    family: str                  # momentum | breakout | mean_reversion | ict | options
    direction: str               # long | short | both
    hold_horizon: str            # intraday | swing
    thesis: str = ""
    daily_filters: dict = field(default_factory=dict)
    liquidity: LiquidityReqs = field(default_factory=LiquidityReqs)
    atr_min_pct: float = 2.0
    atr_max_pct: float = 12.0
    confirmation: list = field(default_factory=list)
    levels: StructureLevels = field(default_factory=StructureLevels)
    options: OptionsEligibility = field(default_factory=OptionsEligibility)
    # validation / gating
    watch_only_default: bool = True
    validation_method: str = "forward_test"   # forward_test | backtest_daily
    min_score_consider: float = 15.0
    min_score_confirm: float = 20.0
    # lifecycle — a template only fires live when BOTH are true AND stats clear the gate
    approved: bool = False       # human sign-off (strategy_template_signoff.md)
    enabled: bool = False        # operator promotion after stats clear
    version: int = 1


@dataclass
class TemplateHit:
    """Unified scanner output (supersedes STTHit/MomentumHit/ScannerHit)."""
    ticker: str
    direction: str = "long"
    price: float = 0.0
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    score: float = 0.0
    strategy_key: str = ""
    instrument_type: str = "stock"        # stock | options | watch_only
    watch_only: bool = False
    reason: str = ""
    stop_reason: str = ""
    target_reason: str = ""
    rr: float = 0.0
    projected_move_pct: float = 0.0
    why_selected: list = field(default_factory=list)
    filters_passed: list = field(default_factory=list)
    invalidation: str = ""
    metadata: dict = field(default_factory=dict)
