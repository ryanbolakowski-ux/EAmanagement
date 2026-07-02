"""Anthropic-backed strategy generation (async).

Client usage mirrors app/api/routes/support.py: the SDK is imported LAZILY
inside the call path (never at module import — keeps the module importable
and cheap when the feature flag is off), the key comes from ANTHROPIC_API_KEY
in the environment, and the model is env-selectable via AI_BUILDER_MODEL
(default claude-sonnet-5).

Structured output is FORCED via a tool call (tool_choice type=tool), so the
model cannot reply with prose. The tool input is validated against the
GeneratedStrategy schema; on validation failure the errors are fed back as
an is_error tool_result and the model gets exactly ONE retry.
"""
from __future__ import annotations

import os

from loguru import logger

from app.engines.ai_builder.prompts import (
    SYSTEM_PROMPT,
    TOOL_NAME,
    build_tool_definition,
    retry_feedback,
)
from app.engines.ai_builder.schema import GeneratedStrategy
from app.engines.ai_builder.validator import validate_generated

#: One retry after the first validation failure (2 API calls max per request).
_MAX_ATTEMPTS = 2


class GenerationError(Exception):
    """Raised when generation cannot produce a valid strategy. Carries an
    http-ish status_code so the route can map it without string matching."""

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


def _make_client():
    """Build the AsyncAnthropic client (lazy import, same as support.py).

    Split out so tests can monkeypatch it and the SDK import cost is only
    paid on the request path.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        # Same posture as support.chat: configuration problem, not user error.
        raise GenerationError(
            "AI builder is being configured (missing API key). Try again later.",
            status_code=503,
        )
    from anthropic import AsyncAnthropic
    return AsyncAnthropic(api_key=api_key)


async def generate_strategy(prose: str) -> GeneratedStrategy:
    """Compile plain-English ``prose`` into a validated GeneratedStrategy.

    Raises GenerationError (with .status_code) on empty prose, missing
    configuration, upstream API failure, or persistent invalid output.
    """
    prose = (prose or "").strip()
    if len(prose) < 10:
        raise GenerationError(
            "Describe your strategy in at least a sentence.", status_code=400
        )

    client = _make_client()
    model = os.environ.get("AI_BUILDER_MODEL", "claude-sonnet-5")
    tool = build_tool_definition()
    messages: list[dict] = [{"role": "user", "content": prose}]
    last_errors: list[str] = ["model returned no strategy"]

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=[tool],
                # Force the structured tool call — no prose replies possible.
                tool_choice={"type": "tool", "name": TOOL_NAME},
            )
        except GenerationError:
            raise
        except Exception as exc:  # SDK/network errors — isolate, don't leak
            logger.error(f"[ai_builder] anthropic call failed (attempt {attempt}): "
                         f"{type(exc).__name__}: {exc}")
            raise GenerationError(
                "The AI builder is unavailable right now. Please try again.",
                status_code=502,
            ) from exc

        tool_block = next(
            (b for b in response.content
             if getattr(b, "type", None) == "tool_use"
             and getattr(b, "name", None) == TOOL_NAME),
            None,
        )
        if tool_block is None:
            # Should be impossible with forced tool_choice, so this is an
            # upstream/model anomaly, NOT a problem with the user's prose —
            # surface it as 502 (like other upstream failures) instead of
            # falling through to the 422 "could not compile" path.
            logger.error(f"[ai_builder] attempt {attempt}: no {TOOL_NAME} "
                         f"tool_use block in response (forced tool_choice ignored?)")
            raise GenerationError(
                "The AI builder returned an unexpected response. Please try again.",
                status_code=502,
            )

        gen, errors = validate_generated(tool_block.input)
        if gen is not None:
            logger.info(
                f"[ai_builder] compiled ok on attempt {attempt}: "
                f"setups={gen.ict_setup or 'generic-v1'} "
                f"confidence={gen.confidence:.2f} "
                f"unsupported={len(gen.unsupported_concepts)}"
            )
            return gen

        last_errors = errors
        logger.warning(f"[ai_builder] attempt {attempt} failed validation: {errors}")
        if attempt < _MAX_ATTEMPTS:
            # Feed the validator errors back and let the model correct itself.
            messages = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "is_error": True,
                    "content": retry_feedback(errors),
                }]},
            ]

    raise GenerationError(
        "Could not compile that description into a valid strategy: "
        + "; ".join(last_errors[:5]),
        status_code=422,
    )
