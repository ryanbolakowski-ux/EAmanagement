"""Pydantic schema for the AI Strategy Builder V2.

HONESTY CONTRACT: ``GeneratedStrategy`` contains ONLY knobs the engine
actually consumes. The list was derived by reading the real consumers:

  * ``app/models/strategy.py`` columns that flow into ``StrategyConfig``
    (see api/routes/backtests.py:431-453, engines/paper_trading/runner.py,
    engines/account_signals/runner.py) — instruments, timeframes, RR,
    stop basis, break-even mode/at_r, sessions, FVG size band, daily caps,
    max contracts.
  * ``rule_tree`` keys the engine reads: ``engine_version`` + ``ict_setup``
    (backtest_engine/ict_strategy.py:123-128 / ict/registry.py),
    ``use_vwap_filter`` / ``use_rsi_filter`` (ict_strategy.py:960-976),
    ``take_profit_mode`` (RANGE-TP-V1, ict_strategy.py:896-898), and
    ``max_trades_per_day`` (ict/setups/fvg_inversion_tap.py:111).

Deliberately ABSENT because the engine has no such knob (requests for
these must land in ``unsupported_concepts``): direction/long-only filters
(direction always comes from the engine's own bias model), order blocks as
a standalone entry trigger, SMT divergence, gamma/options-flow inputs,
news filters, trailing stops, partial take-profits, DOM/volume-profile
conditions.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# ── The honest vocabulary (single source of truth for prompts + validation) ──

#: Futures symbols with real tick sizes in engines/strategy_engine/indicators.py
KNOWN_INSTRUMENTS = ("ES", "NQ", "RTY", "YM", "CL", "GC")

#: Session keys implemented in indicators.is_in_session (ET windows).
KNOWN_SESSIONS = ("NY", "NY_AM", "NY_PM", "LONDON", "LONDON_CLOSE", "ASIA")

#: Canonical dedicated-setup ids registered in app.engines.ict.registry.
#: Selecting one flips the strategy onto the V2 dispatch path; anything else
#: runs the generic V1 ICT cascade model.
KNOWN_SETUPS = (
    "fvg_inversion_tap",   # 1m FVG inversion tap (SS3.8)
    "silver_bullet",       # ICT Silver Bullet 10-11am kill zone
    "judas_swing",         # Judas Swing
    "london_into_ny",      # London Sweep into NY
    "po3",                 # Power of 3 (AMD)
)

#: Intraday timeframes valid for primary/execution (< 60 min — the engine
#: detects setups intraday; >= 1H belongs in higher_timeframes for bias).
#: RESTRICTED to timeframes mapped in backtest_engine/data_handler.py
#: TIMEFRAME_ALIASES: unknown values pass through raw to pandas resample,
#: which raises ValueError (e.g. "Invalid frequency: 3m") and crashes every
#: backtest/optimization run — so 2m/3m/10m must never validate here.
INTRADAY_TIMEFRAMES = ("1m", "5m", "15m", "30m")

#: Bias timeframes valid for higher_timeframes.
HIGHER_TIMEFRAMES = ("1H", "4H", "1D")

_TF_MINUTES = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1H": 60, "4H": 240, "1D": 1440,
}


def timeframe_minutes(tf: str) -> int:
    """Rank helper (1m=1 ... 1D=1440); unknown values rank as 0."""
    return _TF_MINUTES.get(tf, 0)


def _norm_setup(value: str) -> str:
    """Mirror ict.registry._normalize: lower, spaces/hyphens/slashes -> _."""
    out = str(value or "").strip().lower()
    for ch in (" ", "-", "/"):
        out = out.replace(ch, "_")
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


#: Long registry aliases -> canonical short id (e.g. seed names).
_SETUP_ALIASES = {
    "ict_silver_bullet": "silver_bullet",
    "london_sweep_into_ny": "london_into_ny",
    "power_of_3_(po3)": "po3",
    "power_of_3": "po3",
}


class GeneratedStrategy(BaseModel):
    """A strategy expressed ONLY in engine-supported knobs, plus honesty
    fields (explanation / confidence / unsupported_concepts / warnings)."""

    # ── identity ──────────────────────────────────────────────────────────
    name: str = Field(default="AI Generated Strategy", min_length=1, max_length=200)

    # ── instruments / timeframes ──────────────────────────────────────────
    # max_length matches len(KNOWN_INSTRUMENTS): every supported symbol may
    # be requested at once (the before-validator dedupes, so this can't trip).
    instruments: list[str] = Field(
        default_factory=lambda: ["ES"],
        min_length=1,
        max_length=len(KNOWN_INSTRUMENTS),
    )
    primary_timeframe: str = "15m"
    execution_timeframe: str = "1m"
    higher_timeframes: list[str] = Field(default_factory=lambda: ["1H"], max_length=2)

    # ── risk / exits ──────────────────────────────────────────────────────
    risk_reward_ratio: float = Field(default=2.0, ge=0.5, le=10.0)
    stop_loss_type: str = "structure"          # "structure" | "ticks"
    stop_loss_ticks: Optional[int] = Field(default=None, ge=1, le=200)
    take_profit_mode: str = "auto"             # "auto" | "range" (RANGE-TP-V1)
    breakeven_mode: str = "off"                # "off" | "r" | "structure"
    breakeven_at_r: float = Field(default=0.0, ge=0.0, le=1.0)
    max_contracts: int = Field(default=1, ge=1, le=10)

    # ── filters / risk controls ───────────────────────────────────────────
    session_filters: list[str] = Field(default_factory=list, max_length=6)
    use_vwap_filter: bool = False
    use_rsi_filter: bool = False
    fvg_min_size_ticks: int = Field(default=4, ge=1, le=50)
    fvg_max_size_ticks: Optional[int] = Field(default=None, ge=1, le=200)
    max_trades_per_day: Optional[int] = Field(default=None, ge=1, le=20)
    max_daily_loss: Optional[float] = Field(default=None, gt=0, le=100_000)

    # ── engine dispatch ───────────────────────────────────────────────────
    ict_setup: Optional[str] = None            # one of KNOWN_SETUPS -> V2 path
    engine_version: str = "v1"                 # derived; forced "v2" when ict_setup set

    # ── honesty fields ────────────────────────────────────────────────────
    explanation: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    unsupported_concepts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    # ── normalizers / validators ──────────────────────────────────────────

    @field_validator("instruments", mode="before")
    @classmethod
    def _norm_instruments(cls, v):
        if not isinstance(v, list):
            return v
        out = []
        for item in v:
            sym = str(item).strip().upper()
            if sym not in KNOWN_INSTRUMENTS:
                raise ValueError(
                    f"unknown instrument {item!r}; supported: {', '.join(KNOWN_INSTRUMENTS)}"
                )
            if sym not in out:
                out.append(sym)
        return out

    @field_validator("session_filters", mode="before")
    @classmethod
    def _norm_sessions(cls, v):
        if not isinstance(v, list):
            return v
        out = []
        for item in v:
            key = str(item).strip().upper().replace(" ", "_")
            if key not in KNOWN_SESSIONS:
                raise ValueError(
                    f"unknown session {item!r}; supported: {', '.join(KNOWN_SESSIONS)}"
                )
            if key not in out:
                out.append(key)
        return out

    @field_validator("primary_timeframe", "execution_timeframe", mode="before")
    @classmethod
    def _norm_intraday_tf(cls, v):
        tf = str(v).strip().lower().replace("min", "m")
        if tf not in INTRADAY_TIMEFRAMES:
            raise ValueError(
                f"timeframe {v!r} is not a supported intraday timeframe; "
                f"use one of: {', '.join(INTRADAY_TIMEFRAMES)} "
                f"(>= 1H timeframes go in higher_timeframes)"
            )
        return tf

    @field_validator("higher_timeframes", mode="before")
    @classmethod
    def _norm_higher_tfs(cls, v):
        if not isinstance(v, list):
            return v
        out = []
        for item in v:
            tf = str(item).strip().upper().replace("HR", "H")
            if tf in ("60M",):
                tf = "1H"
            if tf not in HIGHER_TIMEFRAMES:
                raise ValueError(
                    f"higher timeframe {item!r} not supported; "
                    f"use one of: {', '.join(HIGHER_TIMEFRAMES)}"
                )
            if tf not in out:
                out.append(tf)
        return out

    @field_validator("stop_loss_type")
    @classmethod
    def _check_stop_type(cls, v):
        if v not in ("structure", "ticks"):
            raise ValueError("stop_loss_type must be 'structure' or 'ticks'")
        return v

    @field_validator("take_profit_mode")
    @classmethod
    def _check_tp_mode(cls, v):
        if v not in ("auto", "range"):
            raise ValueError("take_profit_mode must be 'auto' or 'range'")
        return v

    @field_validator("breakeven_mode")
    @classmethod
    def _check_be_mode(cls, v):
        if v not in ("off", "r", "structure"):
            raise ValueError("breakeven_mode must be 'off', 'r' or 'structure'")
        return v

    @field_validator("ict_setup", mode="before")
    @classmethod
    def _norm_setup_id(cls, v):
        if v is None or str(v).strip() == "":
            return None
        key = _norm_setup(v)
        key = _SETUP_ALIASES.get(key, key)
        if key not in KNOWN_SETUPS:
            raise ValueError(
                f"unknown ict_setup {v!r}; registered setups: {', '.join(KNOWN_SETUPS)} "
                f"(leave null for the generic V1 model)"
            )
        return key

    @model_validator(mode="after")
    def _cross_field(self):
        # engine_version is DERIVED, not trusted: a dedicated setup id means
        # the V2 dispatch path, everything else is the generic V1 model.
        self.engine_version = "v2" if self.ict_setup else "v1"
        if (
            self.fvg_max_size_ticks is not None
            and self.fvg_max_size_ticks < self.fvg_min_size_ticks
        ):
            raise ValueError("fvg_max_size_ticks must be >= fvg_min_size_ticks")
        return self
