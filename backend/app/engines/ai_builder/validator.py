"""Validation, risk-sanity defaults, and rule_tree compilation.

Three responsibilities, all deterministic (no LLM here):

1. ``validate_generated`` — schema + range validation of the raw tool-call
   payload (RR 0.5-10, BE at_r 0-1, sessions/instruments/timeframes from the
   known enums — the checks live as pydantic validators in schema.py; this
   flattens the errors into strings the generator can feed back for a retry).
2. ``apply_risk_sanity`` — fixes internally-inconsistent but recoverable
   combinations with an explicit warning (never silently).
3. ``compile_to_rule_tree`` / ``build_strategy_payload`` — produce the
   ``rule_tree`` dict and a StrategyCreate-shaped draft payload compatible
   with the existing Strategy model / POST /strategies create flow.
"""
from __future__ import annotations

from pydantic import ValidationError

from app.engines.ai_builder.schema import GeneratedStrategy, timeframe_minutes

#: Safety default applied when the user set no daily cap at all. Surfaced as
#: a warning so the review UI shows it — the user can delete it before saving.
DEFAULT_MAX_TRADES_PER_DAY = 5


def validate_generated(payload: dict) -> tuple[GeneratedStrategy | None, list[str]]:
    """Validate a raw tool-call payload.

    Returns ``(strategy, [])`` on success (with risk-sanity defaults already
    applied) or ``(None, errors)`` where errors are human-readable strings
    suitable for feeding back to the model on the retry pass.
    """
    if not isinstance(payload, dict):
        return None, [f"expected a JSON object, got {type(payload).__name__}"]
    try:
        gen = GeneratedStrategy.model_validate(payload)
    except ValidationError as exc:
        errors = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
            errors.append(f"{loc}: {err.get('msg', 'invalid value')}")
        return None, errors
    return apply_risk_sanity(gen), []


def apply_risk_sanity(gen: GeneratedStrategy) -> GeneratedStrategy:
    """Fix recoverable inconsistencies in place, appending a warning for each.

    Nothing here is silent: every mutation adds a ``warnings`` entry so the
    draft-review UI can show exactly what the compiler changed.
    """
    # break-even consistency: mode 'r' needs a positive trigger; other modes
    # don't use at_r at all.
    if gen.breakeven_mode == "r" and gen.breakeven_at_r <= 0:
        gen.breakeven_at_r = 0.5
        gen.warnings.append(
            "breakeven_mode 'r' had no trigger; defaulted breakeven_at_r to 0.5R."
        )
    if gen.breakeven_mode != "r" and gen.breakeven_at_r != 0.0:
        gen.breakeven_at_r = 0.0
        gen.warnings.append(
            f"breakeven_at_r is only used with breakeven_mode 'r'; reset to 0 "
            f"(mode is '{gen.breakeven_mode}')."
        )

    # stop basis consistency: tick stops need a distance; structure stops
    # must not carry a stale tick distance.
    if gen.stop_loss_type == "ticks" and gen.stop_loss_ticks is None:
        gen.stop_loss_type = "structure"
        gen.warnings.append(
            "stop_loss_type 'ticks' had no stop_loss_ticks; fell back to the "
            "'structure' stop (behind the setup swing)."
        )
    if gen.stop_loss_type == "structure" and gen.stop_loss_ticks is not None:
        gen.stop_loss_ticks = None
        gen.warnings.append("stop_loss_ticks ignored: stop_loss_type is 'structure'.")

    # timeframe ordering: execution must be <= primary (the engine refines
    # entries on the execution TF inside setups found on the primary TF).
    if timeframe_minutes(gen.execution_timeframe) > timeframe_minutes(gen.primary_timeframe):
        gen.warnings.append(
            f"execution_timeframe {gen.execution_timeframe} was above "
            f"primary_timeframe {gen.primary_timeframe}; reset execution to 1m."
        )
        gen.execution_timeframe = "1m"

    # bias source: the engine falls back to the primary TF without HTFs, but
    # every seeded strategy carries one — keep parity with the manual builder.
    if not gen.higher_timeframes:
        gen.higher_timeframes = ["1H"]
        gen.warnings.append("no bias timeframe given; defaulted higher_timeframes to 1H.")

    # risk-sanity default: an uncapped strategy can overtrade a live account.
    # This is a REVIEWABLE default, not a hard rule — surfaced via warnings.
    if gen.max_trades_per_day is None:
        gen.max_trades_per_day = DEFAULT_MAX_TRADES_PER_DAY
        gen.warnings.append(
            f"no daily trade cap requested; applied a safety default of "
            f"{DEFAULT_MAX_TRADES_PER_DAY}/day (remove it in review if unwanted)."
        )

    return gen


def compile_to_rule_tree(gen: GeneratedStrategy) -> dict:
    """Compile to a rule_tree the existing engine actually reads.

    Key consumers: engine_version/ict_setup (ict_strategy.py V2 dispatch +
    ict/registry.py), use_vwap_filter/use_rsi_filter + take_profit_mode
    (StrategyConfig construction in backtests/paper/signals runners), and
    max_trades_per_day (read from rule_tree by the fvg_inversion_tap setup).
    Keys the engine defaults to falsy are only emitted when set, matching
    the minimal trees the frontend compiler produced.
    """
    rule_tree: dict = {
        "engine_version": gen.engine_version,
        # provenance — ignored by the engine, invaluable for debugging.
        "generated_by": "ai_builder_v2",
    }
    if gen.ict_setup:
        rule_tree["ict_setup"] = gen.ict_setup
    if gen.use_vwap_filter:
        rule_tree["use_vwap_filter"] = True
    if gen.use_rsi_filter:
        rule_tree["use_rsi_filter"] = True
    if gen.take_profit_mode != "auto":
        rule_tree["take_profit_mode"] = gen.take_profit_mode
    if gen.max_trades_per_day:
        # Mirrored here because V2 dedicated setups read the cap from the
        # rule_tree first (fvg_inversion_tap.py:111); the column remains the
        # engine-wide hard cap via check_risk_controls.
        rule_tree["max_trades_per_day"] = int(gen.max_trades_per_day)
    return rule_tree


def build_strategy_payload(gen: GeneratedStrategy, prose: str) -> dict:
    """StrategyCreate-shaped DRAFT payload for POST /api/v1/strategies.

    status is 'draft' on purpose: the route that returns this must never
    auto-create the row — the user reviews, optionally edits, then the
    frontend POSTs this payload through the normal create flow.
    """
    return {
        "name": gen.name,
        # the prose is kept as the description (as today), but it is no
        # longer the ONLY artifact — the knobs below are the real compile.
        "description": (prose or "").strip(),
        "status": "draft",
        "instruments": list(gen.instruments),
        "primary_timeframe": gen.primary_timeframe,
        "execution_timeframe": gen.execution_timeframe,
        "higher_timeframes": list(gen.higher_timeframes),
        "risk_reward_ratio": gen.risk_reward_ratio,
        "stop_loss_type": gen.stop_loss_type,
        "stop_loss_ticks": gen.stop_loss_ticks,
        "max_contracts": gen.max_contracts,
        "session_filters": list(gen.session_filters),
        "fvg_min_size_ticks": gen.fvg_min_size_ticks,
        "fvg_max_size_ticks": gen.fvg_max_size_ticks,
        "max_daily_loss": gen.max_daily_loss,
        "max_trades_per_day": gen.max_trades_per_day,
        "breakeven_mode": gen.breakeven_mode,
        "breakeven_at_r": gen.breakeven_at_r,
        "rule_tree": compile_to_rule_tree(gen),
    }
