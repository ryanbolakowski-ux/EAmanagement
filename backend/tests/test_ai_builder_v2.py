"""AI Strategy Builder V2 — unit tests (NO live API calls, NO database).

The anthropic client is FULLY mocked by monkeypatching
``app.engines.ai_builder.generator._make_client``; the route tests run the
real FastAPI router in-process with the auth dependency overridden.

Covers:
  1. prose-to-knobs golden case (mocked model output -> exact knob asserts)
  2. unsupported-concept honesty (never silently dropped)
  3. validator rejects out-of-range / unknown-enum values
  4. retry-on-invalid path (validator errors fed back, second call succeeds)
  5. feature flag: ENABLE_AI_BUILDER_V2 off (default) -> 404; on -> draft
     payload returned WITHOUT creating a strategy row

Run: pytest tests/test_ai_builder_v2.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.engines.ai_builder import generator as gen_mod
from app.engines.ai_builder.generator import GenerationError, generate_strategy
from app.engines.ai_builder.prompts import TOOL_NAME, build_tool_definition
from app.engines.ai_builder.schema import INTRADAY_TIMEFRAMES, KNOWN_INSTRUMENTS
from app.engines.ai_builder.validator import (
    DEFAULT_MAX_TRADES_PER_DAY,
    compile_to_rule_tree,
    validate_generated,
)

GOLDEN_PROSE = (
    "trade NQ london session, 2:1 target, break even at 0.5R, only VWAP-aligned"
)

#: What a correct model response for GOLDEN_PROSE looks like (the mocked
#: tool_use input). The test asserts the plumbing carries every knob through
#: validation + sanity + compile EXACTLY.
GOLDEN_PAYLOAD = {
    "name": "NQ London VWAP Model",
    "instruments": ["NQ"],
    "primary_timeframe": "15m",
    "execution_timeframe": "1m",
    "higher_timeframes": ["1H"],
    "risk_reward_ratio": 2.0,
    "stop_loss_type": "structure",
    "take_profit_mode": "auto",
    "breakeven_mode": "r",
    "breakeven_at_r": 0.5,
    "session_filters": ["LONDON"],
    "use_vwap_filter": True,
    "use_rsi_filter": False,
    "explanation": "NQ during the London window, engine FVG model, 2R target, "
                   "break-even at 0.5R, VWAP-aligned entries only.",
    "confidence": 0.92,
    "unsupported_concepts": [],
    "warnings": [],
}


# ── fully mocked anthropic client ────────────────────────────────────────────

class _FakeToolUse:
    type = "tool_use"
    name = TOOL_NAME

    def __init__(self, payload, block_id="toolu_fake_01"):
        self.input = payload
        self.id = block_id


#: Sentinel payload: the fake response carries NO tool_use block (models the
#: near-impossible case of forced tool_choice being ignored upstream).
NO_TOOL_USE = object()


class _FakeResponse:
    def __init__(self, payload):
        if payload is NO_TOOL_USE:
            self.content = [SimpleNamespace(type="text", text="(no tool call)")]
        else:
            self.content = [_FakeToolUse(payload)]


class _FakeMessages:
    """Returns one canned payload per .create() call, records every call."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._payloads:
            raise AssertionError("mock exhausted: more API calls than expected")
        return _FakeResponse(self._payloads.pop(0))


class _FakeClient:
    def __init__(self, payloads):
        self.messages = _FakeMessages(payloads)


def _mock_client(monkeypatch, payloads) -> _FakeClient:
    client = _FakeClient(payloads)
    monkeypatch.setattr(gen_mod, "_make_client", lambda: client)
    return client


# ── 1. golden case: prose -> exact knobs ─────────────────────────────────────

def test_golden_nq_london_vwap_knobs(monkeypatch):
    client = _mock_client(monkeypatch, [GOLDEN_PAYLOAD])
    gen = asyncio.run(generate_strategy(GOLDEN_PROSE))

    # exact knob assertions
    assert gen.instruments == ["NQ"]
    assert gen.session_filters == ["LONDON"]
    assert gen.risk_reward_ratio == 2.0
    assert gen.breakeven_mode == "r"
    assert gen.breakeven_at_r == 0.5
    assert gen.use_vwap_filter is True
    assert gen.stop_loss_type == "structure"
    assert gen.engine_version == "v1"  # no dedicated setup -> generic model
    # risk-sanity default: uncapped strategy gets a reviewable daily cap
    assert gen.max_trades_per_day == DEFAULT_MAX_TRADES_PER_DAY
    assert any("safety default" in w for w in gen.warnings)

    # compiled rule_tree carries ONLY keys the engine actually reads
    rt = compile_to_rule_tree(gen)
    assert rt == {
        "engine_version": "v1",
        "generated_by": "ai_builder_v2",
        "use_vwap_filter": True,
        "max_trades_per_day": DEFAULT_MAX_TRADES_PER_DAY,
    }

    # single API call, forced tool choice, prose passed through
    assert len(client.messages.calls) == 1
    call = client.messages.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    assert call["messages"][0]["content"] == GOLDEN_PROSE


def test_setup_alias_normalizes_and_flips_engine_v2(monkeypatch):
    payload = dict(GOLDEN_PAYLOAD, ict_setup="ICT Silver Bullet",
                   session_filters=["NY_AM"])
    _mock_client(monkeypatch, [payload])
    gen = asyncio.run(generate_strategy("silver bullet on NQ in the ny am kill zone"))
    assert gen.ict_setup == "silver_bullet"        # registry-style normalization
    assert gen.engine_version == "v2"              # derived, not model-trusted
    rt = compile_to_rule_tree(gen)
    assert rt["ict_setup"] == "silver_bullet"
    assert rt["engine_version"] == "v2"


# ── 2. unsupported-concept honesty ───────────────────────────────────────────

def test_unsupported_concept_is_reported_not_dropped(monkeypatch):
    payload = dict(
        GOLDEN_PAYLOAD,
        confidence=0.4,
        unsupported_concepts=["gamma exposure filter"],
        warnings=["gamma exposure is not something the engine can evaluate"],
    )
    _mock_client(monkeypatch, [payload])
    gen = asyncio.run(generate_strategy(
        "trade NQ london session with 2:1 target but only when gamma exposure is negative"
    ))
    assert "gamma exposure filter" in gen.unsupported_concepts
    assert gen.confidence == 0.4
    # honesty fields survive into the API-facing dump too
    dumped = gen.model_dump()
    assert dumped["unsupported_concepts"] == ["gamma exposure filter"]


# ── 3. validator rejects out-of-range / unknown enums ────────────────────────

def test_validator_rejects_out_of_range_rr():
    gen, errors = validate_generated(dict(GOLDEN_PAYLOAD, risk_reward_ratio=42))
    assert gen is None
    assert any("risk_reward_ratio" in e for e in errors)


def test_validator_rejects_breakeven_beyond_1r():
    gen, errors = validate_generated(dict(GOLDEN_PAYLOAD, breakeven_at_r=2.0))
    assert gen is None
    assert any("breakeven_at_r" in e for e in errors)


def test_validator_rejects_unknown_session_and_instrument():
    gen, errors = validate_generated(dict(GOLDEN_PAYLOAD, session_filters=["TOKYO"]))
    assert gen is None and any("session" in e.lower() for e in errors)

    gen, errors = validate_generated(dict(GOLDEN_PAYLOAD, instruments=["BTC"]))
    assert gen is None and any("instrument" in e.lower() for e in errors)


def test_validator_rejects_unknown_setup_and_bad_timeframe():
    gen, errors = validate_generated(dict(GOLDEN_PAYLOAD, ict_setup="order_block_magic"))
    assert gen is None and any("ict_setup" in e for e in errors)

    gen, errors = validate_generated(dict(GOLDEN_PAYLOAD, primary_timeframe="4H"))
    assert gen is None and any("primary_timeframe" in e for e in errors)


def test_pandas_unrunnable_timeframes_never_validate():
    # 2m/3m/10m have no TIMEFRAME_ALIASES mapping (data_handler.py) and pass
    # raw to pandas resample, which raises "Invalid frequency" — a validated
    # draft with those TFs would crash every backtest. They must be rejected
    # here AND absent from the vocabulary the tool schema advertises.
    assert INTRADAY_TIMEFRAMES == ("1m", "5m", "15m", "30m")
    for tf in ("2m", "3m", "10m"):
        gen, errors = validate_generated(dict(GOLDEN_PAYLOAD, primary_timeframe=tf))
        assert gen is None and any("primary_timeframe" in e for e in errors)
        gen, errors = validate_generated(dict(GOLDEN_PAYLOAD, execution_timeframe=tf))
        assert gen is None and any("execution_timeframe" in e for e in errors)

    # prompts.py imports the constant, so the tool schema follows suit
    props = build_tool_definition()["input_schema"]["properties"]
    assert props["primary_timeframe"]["enum"] == ["1m", "5m", "15m", "30m"]
    assert props["execution_timeframe"]["enum"] == ["1m", "5m", "15m", "30m"]


def test_all_six_supported_instruments_accepted_together():
    # the instruments cap must fit every supported symbol at once — prose
    # like "trade ES, NQ, RTY, YM, CL and GC" is fully engine-supported.
    gen, errors = validate_generated(dict(GOLDEN_PAYLOAD,
                                          instruments=list(KNOWN_INSTRUMENTS)))
    assert errors == []
    assert gen.instruments == list(KNOWN_INSTRUMENTS)


def test_risk_sanity_fixes_are_warned_never_silent():
    # tick stop without a distance falls back to structure, WITH a warning
    gen, errors = validate_generated(dict(
        GOLDEN_PAYLOAD, stop_loss_type="ticks", stop_loss_ticks=None,
    ))
    assert errors == []
    assert gen.stop_loss_type == "structure"
    assert any("stop_loss_ticks" in w for w in gen.warnings)

    # breakeven 'r' with no trigger gets the 0.5R default, WITH a warning
    gen, _ = validate_generated(dict(GOLDEN_PAYLOAD, breakeven_at_r=0))
    assert gen.breakeven_at_r == 0.5
    assert any("0.5R" in w for w in gen.warnings)


# ── 4. retry-on-invalid path ─────────────────────────────────────────────────

def test_retry_feeds_validator_errors_back_then_succeeds(monkeypatch):
    bad = dict(GOLDEN_PAYLOAD, risk_reward_ratio=42)  # fails range check
    client = _mock_client(monkeypatch, [bad, GOLDEN_PAYLOAD])

    gen = asyncio.run(generate_strategy(GOLDEN_PROSE))
    assert gen.risk_reward_ratio == 2.0            # second (corrected) payload won
    assert len(client.messages.calls) == 2         # exactly one retry

    # the retry request must carry the validator errors back to the model
    retry_messages = client.messages.calls[1]["messages"]
    assert len(retry_messages) == 3                # prose, assistant, tool_result
    tool_result = retry_messages[2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["is_error"] is True
    assert "risk_reward_ratio" in tool_result["content"]


def test_two_invalid_attempts_raise_generation_error(monkeypatch):
    bad = dict(GOLDEN_PAYLOAD, risk_reward_ratio=42)
    client = _mock_client(monkeypatch, [bad, bad])
    with pytest.raises(GenerationError) as exc:
        asyncio.run(generate_strategy(GOLDEN_PROSE))
    assert exc.value.status_code == 422
    assert "risk_reward_ratio" in str(exc.value)
    assert len(client.messages.calls) == 2         # never more than one retry


def test_no_tool_use_block_is_upstream_502_not_user_blame(monkeypatch):
    # forced tool_choice makes this near-impossible; if it happens anyway it
    # is an upstream anomaly -> 502, without burning the validation retry.
    client = _mock_client(monkeypatch, [NO_TOOL_USE])
    with pytest.raises(GenerationError) as exc:
        asyncio.run(generate_strategy(GOLDEN_PROSE))
    assert exc.value.status_code == 502
    assert len(client.messages.calls) == 1


def test_empty_prose_rejected_before_any_api_call(monkeypatch):
    client = _mock_client(monkeypatch, [])
    with pytest.raises(GenerationError) as exc:
        asyncio.run(generate_strategy("   "))
    assert exc.value.status_code == 400
    assert client.messages.calls == []


def test_missing_api_key_is_config_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(GenerationError) as exc:
        gen_mod._make_client()
    assert exc.value.status_code == 503


# ── 5. route: flag off -> 404; flag on -> draft payload, no row created ──────

def _make_test_app():
    """Real strategies router, in-process, auth overridden (no DB touched:
    the generate-v2 route has no db dependency and generation is mocked)."""
    from fastapi import FastAPI
    from app.api.routes import strategies as strategies_module
    from app.core import auth as auth_module
    from app.models.user import SubscriptionTier

    app = FastAPI()
    app.include_router(strategies_module.router, prefix="/api/v1/strategies")
    fake_user = SimpleNamespace(
        id="00000000-0000-0000-0000-0000000000a1",
        email="ai-builder@thetaalgos.test",
        username="ai_builder_test",
        subscription_tier=SubscriptionTier.TIER_5,
        totp_enabled=True,  # passes the 2FA gate inside require_tier
    )
    app.dependency_overrides[auth_module.get_current_user] = lambda: fake_user
    return app


def test_route_flag_off_returns_404(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.delenv("ENABLE_AI_BUILDER_V2", raising=False)  # default = off
    with TestClient(_make_test_app()) as client:
        r = client.post("/api/v1/strategies/generate-v2", json={"prose": GOLDEN_PROSE})
    assert r.status_code == 404


def test_route_flag_on_returns_draft_and_never_creates(monkeypatch):
    from fastapi.testclient import TestClient
    from app.engines.ai_builder.schema import GeneratedStrategy

    monkeypatch.setenv("ENABLE_AI_BUILDER_V2", "true")

    compiled = GeneratedStrategy.model_validate(GOLDEN_PAYLOAD)

    async def _fake_generate(prose: str):
        return compiled

    # the route lazy-imports generate_strategy from the module at request
    # time, so patching the module attribute is enough — no client needed.
    monkeypatch.setattr(gen_mod, "generate_strategy", _fake_generate)

    with TestClient(_make_test_app()) as client:
        r = client.post("/api/v1/strategies/generate-v2", json={"prose": GOLDEN_PROSE})

    assert r.status_code == 200
    body = r.json()
    # DRAFT contract: nothing persisted, no id anywhere, status is draft
    assert body["draft"] is True
    assert "id" not in body["strategy_payload"]
    assert body["strategy_payload"]["status"] == "draft"
    # the compile is visible + honest
    assert body["generated"]["instruments"] == ["NQ"]
    assert body["generated"]["confidence"] == pytest.approx(0.92)
    assert body["rule_tree"]["use_vwap_filter"] is True
    assert body["strategy_payload"]["rule_tree"] == body["rule_tree"]
    assert body["strategy_payload"]["description"] == GOLDEN_PROSE


def test_route_flag_on_rejects_offensive_override_name(monkeypatch):
    from fastapi.testclient import TestClient
    from app.engines.ai_builder.schema import GeneratedStrategy

    monkeypatch.setenv("ENABLE_AI_BUILDER_V2", "true")
    compiled = GeneratedStrategy.model_validate(GOLDEN_PAYLOAD)

    async def _fake_generate(prose: str):
        return compiled

    monkeypatch.setattr(gen_mod, "generate_strategy", _fake_generate)
    with TestClient(_make_test_app()) as client:
        r = client.post("/api/v1/strategies/generate-v2",
                        json={"prose": GOLDEN_PROSE, "name": "r4pe machine"})
    assert r.status_code == 400  # NAME-MODERATION-V1 applies to this route too
