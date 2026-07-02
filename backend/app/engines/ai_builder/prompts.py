"""Prompts + tool definition for the AI Strategy Builder V2.

The system prompt teaches the model the EXACT supported knob vocabulary
(embedded straight from schema.py so prompt and validator can never drift)
and the honesty rule: anything the engine cannot express goes in
``unsupported_concepts`` — never silently dropped, never approximated
without a warning.
"""
from __future__ import annotations

from app.engines.ai_builder.schema import (
    HIGHER_TIMEFRAMES,
    INTRADAY_TIMEFRAMES,
    KNOWN_INSTRUMENTS,
    KNOWN_SESSIONS,
    KNOWN_SETUPS,
)

#: Name of the forced tool; the tool_use input IS the GeneratedStrategy JSON.
TOOL_NAME = "emit_strategy"


def build_tool_definition() -> dict:
    """Anthropic tool whose input_schema mirrors GeneratedStrategy.

    Hand-written (not model_json_schema()) so field descriptions teach the
    model, but every enum/range is imported from schema.py constants.
    """
    return {
        "name": TOOL_NAME,
        "description": (
            "Emit the compiled trading strategy. Every field must use ONLY the "
            "supported vocabulary. Concepts the engine cannot express go in "
            "unsupported_concepts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short, clean strategy name (no profanity).",
                },
                "instruments": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(KNOWN_INSTRUMENTS)},
                    "description": "Futures symbols to trade.",
                },
                "primary_timeframe": {
                    "type": "string",
                    "enum": list(INTRADAY_TIMEFRAMES),
                    "description": "Setup-detection timeframe (intraday, < 1H).",
                },
                "execution_timeframe": {
                    "type": "string",
                    "enum": list(INTRADAY_TIMEFRAMES),
                    "description": "Entry-refinement timeframe, <= primary_timeframe.",
                },
                "higher_timeframes": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(HIGHER_TIMEFRAMES)},
                    "description": "Bias timeframes (max 2).",
                },
                "risk_reward_ratio": {
                    "type": "number",
                    "description": "Take-profit distance as a multiple of risk. "
                                   "0.5-10. '2:1 target' means 2.0.",
                },
                "stop_loss_type": {
                    "type": "string",
                    "enum": ["structure", "ticks"],
                    "description": "'structure' = behind the swing that formed the "
                                   "setup (default); 'ticks' = fixed distance.",
                },
                "stop_loss_ticks": {
                    "type": ["integer", "null"],
                    "description": "Required when stop_loss_type='ticks' (1-200).",
                },
                "take_profit_mode": {
                    "type": "string",
                    "enum": ["auto", "range"],
                    "description": "'auto' = swing/HTF-FVG/RR hierarchy; 'range' = "
                                   "opposite extreme of the swept dealing range "
                                   "('target the other side of the range').",
                },
                "breakeven_mode": {
                    "type": "string",
                    "enum": ["off", "r", "structure"],
                    "description": "'r' = stop to entry after breakeven_at_r x risk; "
                                   "'structure' = stop to entry on a prior-swing "
                                   "break; 'off' = never.",
                },
                "breakeven_at_r": {
                    "type": "number",
                    "description": "R-multiple trigger for breakeven_mode='r' (0-1, "
                                   "e.g. 0.5 for 'break even at half R'). 0 otherwise.",
                },
                "max_contracts": {"type": "integer", "description": "1-10."},
                "session_filters": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(KNOWN_SESSIONS)},
                    "description": "Only trade inside these ET session windows. "
                                   "Empty = all hours (futures also pass a "
                                   "market-activity gate).",
                },
                "use_vwap_filter": {
                    "type": "boolean",
                    "description": "Veto entries not aligned with session VWAP "
                                   "(longs above / shorts below).",
                },
                "use_rsi_filter": {
                    "type": "boolean",
                    "description": "Veto overheated longs (RSI>70) / oversold shorts (RSI<30).",
                },
                "fvg_min_size_ticks": {
                    "type": "integer",
                    "description": "Minimum fair-value-gap size in ticks (default 4).",
                },
                "fvg_max_size_ticks": {
                    "type": ["integer", "null"],
                    "description": "Optional FVG size cap in ticks.",
                },
                "max_trades_per_day": {
                    "type": ["integer", "null"],
                    "description": "Daily trade cap (1-20), null = no explicit cap.",
                },
                "max_daily_loss": {
                    "type": ["number", "null"],
                    "description": "Dollar daily-loss kill switch, null = none.",
                },
                "ict_setup": {
                    "type": ["string", "null"],
                    "enum": list(KNOWN_SETUPS) + [None],
                    "description": "Dedicated setup template if the description "
                                   "clearly matches one; null = generic model.",
                },
                "explanation": {
                    "type": "string",
                    "description": "2-4 plain-English sentences: what was compiled, "
                                   "how the engine will trade it, and what was NOT "
                                   "expressible.",
                },
                "confidence": {
                    "type": "number",
                    "description": "0-1: how faithfully the knobs capture the "
                                   "user's described strategy. Lower it for every "
                                   "unsupported concept or guess.",
                },
                "unsupported_concepts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Every requested idea the engine CANNOT express, "
                                   "verbatim-ish (e.g. 'gamma exposure filter').",
                },
                "warnings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Caveats about approximations you made.",
                },
            },
            "required": [
                "name", "instruments", "risk_reward_ratio", "explanation",
                "confidence", "unsupported_concepts", "warnings",
            ],
        },
    }


SYSTEM_PROMPT = f"""You are the strategy compiler for Theta Algos, an algorithmic futures-trading platform. You translate a trader's plain-English strategy description into the EXACT configuration knobs the platform's engine supports, by calling the `{TOOL_NAME}` tool.

THE ENGINE (what your output actually drives)
The platform runs a multi-timeframe ICT model: it determines bias from a higher timeframe, requires displacement + a recent liquidity sweep + premium/discount positioning, then enters at the consequent encroachment of a fair value gap, stop at structure, target from a swing/HTF-FVG/RR hierarchy. YOU DO NOT WRITE CODE OR RULES — you only set the knobs below. The entry logic itself (FVG/sweep/bias gates) is fixed; your knobs filter, scope and manage it.

SUPPORTED KNOB VOCABULARY (exhaustive — nothing else exists)
- instruments: {', '.join(KNOWN_INSTRUMENTS)} (futures only; "Nasdaq"->NQ, "S&P"->ES, "Russell"->RTY, "Dow"->YM, "crude/oil"->CL, "gold"->GC)
- primary_timeframe / execution_timeframe: {', '.join(INTRADAY_TIMEFRAMES)} (execution <= primary)
- higher_timeframes (bias): {', '.join(HIGHER_TIMEFRAMES)}, max 2
- risk_reward_ratio: 0.5-10 ("2:1" / "2R target" -> 2.0)
- stop_loss_type: structure (default) | ticks (+ stop_loss_ticks 1-200)
- take_profit_mode: auto | range ("other side of the range" -> range)
- breakeven_mode: off | r (+ breakeven_at_r 0-1) | structure
- session_filters: {', '.join(KNOWN_SESSIONS)} (ET windows; "London session"->LONDON, "New York morning"/"NY open"->NY_AM, "afternoon"->NY_PM, "Asia"->ASIA)
- use_vwap_filter ("VWAP-aligned only"), use_rsi_filter ("skip overbought/oversold")
- fvg_min_size_ticks / fvg_max_size_ticks (gap size band)
- max_trades_per_day (1-20), max_daily_loss ($), max_contracts (1-10)
- ict_setup: dedicated templates -> {', '.join(KNOWN_SETUPS)}

ICT CONCEPT MAPPINGS (map to what is REAL in this engine)
- "FVG inversion" / "inverse FVG" / "IFVG reclaim" -> ict_setup: fvg_inversion_tap
- "Silver Bullet" / "10-11am kill zone" -> ict_setup: silver_bullet (also session NY_AM)
- "Judas swing" / "fake move at the open" -> ict_setup: judas_swing
- "London sweep into New York" -> ict_setup: london_into_ny
- "Power of 3" / "PO3" / "AMD" / "accumulation-manipulation-distribution" -> ict_setup: po3
- Plain "FVG", "liquidity sweep", "displacement", "premium/discount", "kill zone" descriptions with no template match -> generic model (ict_setup: null): those gates are ALREADY built in; express the user's scoping via sessions/timeframes/filters.
- "break even at structure" / "move stop to entry when a swing breaks" -> breakeven_mode: structure
- "target the opposite side of the range" -> take_profit_mode: range

THE HONESTY RULE (non-negotiable)
If the trader asks for ANYTHING not expressible with the knobs above — examples: longs-only/shorts-only (direction comes from the engine's own bias model), order-block entries, SMT divergence, gamma exposure, options flow, news filters, trailing stops, partial profits, DOM/volume conditions, specific indicators other than VWAP/RSI — you MUST list it in unsupported_concepts, in the trader's own words. NEVER silently drop a request. NEVER pretend a knob covers something it does not. If you approximate (e.g. "morning" -> NY_AM), say so in warnings. Set confidence honestly: 0.9+ only when every stated idea mapped cleanly; subtract for each unsupported or approximated concept.

Call `{TOOL_NAME}` exactly once with your best compilation."""


def retry_feedback(errors: list[str]) -> str:
    """Tool-result body fed back after a validation failure (one retry)."""
    bullet = "\n".join(f"- {e}" for e in errors)
    return (
        "Your strategy failed validation against the engine schema:\n"
        f"{bullet}\n"
        f"Call `{TOOL_NAME}` again with corrected values. If a value failed "
        "because the concept is not supported, remove it and record the "
        "concept in unsupported_concepts instead."
    )
