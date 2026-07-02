# AI Strategy Builder V2 — real backend compiler (2026-07-01)

## A) Current-state truth (what "AI builder" actually was before this)

There was NO backend generation path. Verified against the tree:

- `frontend/src/pages/AIStrategyBuilder.tsx` (535 LOC) does **client-side
  regex/keyword matching** (`quickScan()`): it pulls timeframe tokens and a
  concept word-list out of the prose as the user types. Its comment "Same
  logic on the server for richer detection" was **false** — no such server
  code existed (confirmed: no generate route in `app/api/routes/`, no
  ai/compiler module in `app/engines/`; the only Anthropic usage was the
  support-chat SSE endpoint in `routes/support.py`).
- On save (`saveMutation`, AIStrategyBuilder.tsx:190-244) it compiled **at
  most 2 rule_tree knobs**: `use_vwap_filter` (if the word VWAP appeared) and
  `take_profit_mode: 'range'` (if the target step matched
  `/other side|opposite side|other end|range/`), plus an explicit break-even
  radio (`breakeven_mode` / `breakeven_at_r`).
- Everything else was **hardcoded**: `risk_reward_ratio: 2.5`,
  `stop_loss_type: 'structure'`, `max_contracts: 10`, `session_filters: []`,
  `fvg_min_size_ticks: 4` — regardless of what the user wrote.
- The user's prose was stored as an **inert `description`**. "Stop below the
  order block", "only trade shorts", "2 trades max" — all silently ignored.
  The engine then ran its generic FVG/sweep/bias model under the user's
  strategy name. This is the honesty gap V2 closes.

## B) V2 architecture (`backend/app/engines/ai_builder/`)

```
prose ─► POST /api/v1/strategies/generate-v2        (auth: paid tier; flag: ENABLE_AI_BUILDER_V2, default OFF -> 404)
           │
           ▼
       generator.py   AsyncAnthropic (env ANTHROPIC_API_KEY, model env
           │          AI_BUILDER_MODEL, default claude-sonnet-5), structured
           │          output FORCED via tool_choice={type:"tool"} on the
           │          `emit_strategy` tool; ONE retry feeding validator
           │          errors back as an is_error tool_result.
           ▼
       schema.py      GeneratedStrategy — ONLY engine-real knobs (see below)
           │          + honesty fields: explanation, confidence 0-1,
           │          unsupported_concepts[], warnings[].
           ▼
       validator.py   schema/range/enum validation, risk-sanity defaults
           │          (each fix appends a warning — nothing silent),
           │          compile_to_rule_tree(), build_strategy_payload().
           ▼
       DRAFT response {draft: true, generated, rule_tree, strategy_payload}
                      — the endpoint NEVER creates the strategy row.
```

### The honest knob vocabulary

Derived by reading the actual consumers (`models/strategy.py` columns ->
`StrategyConfig` construction in `routes/backtests.py:431-453` /
`paper_trading/runner.py` / `account_signals/runner.py`, plus every
`rule_tree.get(...)` in the engine):

| Knob | Engine consumer |
|---|---|
| instruments (ES/NQ/RTY/YM/CL/GC — all 6 allowed at once) | TICK_SIZES, indicators.py |
| primary/execution TFs (1m/5m/15m/30m only — the TFs mapped in data_handler.TIMEFRAME_ALIASES; anything else, e.g. 3m, crashes pandas resample) + higher TFs (1H/4H/1D) | ICTStrategy.on_bar TF cascade |
| risk_reward_ratio (0.5–10) | compute_take_profit |
| stop_loss_type structure/ticks (+ticks 1–200) | backtest_runner stops |
| take_profit_mode auto/range | RANGE-TP-V1 (ict_strategy.py:896) |
| breakeven_mode off/r/structure + at_r (0–1) | backtest_runner.py:334-341 |
| session_filters NY/NY_AM/NY_PM/LONDON/LONDON_CLOSE/ASIA | is_in_session |
| use_vwap_filter / use_rsi_filter | ict_strategy.py:960-976 |
| fvg_min/max_size_ticks | detect_fvgs |
| max_trades_per_day (1–20) / max_daily_loss / max_contracts | check_risk_controls + entry_guard; rule_tree copy read by fvg_inversion_tap |
| ict_setup (fvg_inversion_tap / silver_bullet / judas_swing / london_into_ny / po3) | ict/registry.py V2 dispatch |
| engine_version | DERIVED (v2 iff ict_setup set) — never trusted from the model |

**Deliberately absent** (engine has no such knob; requests land in
`unsupported_concepts`): direction/long-only filters (direction always comes
from the engine's own bias model), order-block entries, SMT divergence,
gamma/options-flow, news filters, trailing stops, partial take-profits,
custom indicators beyond VWAP/RSI. The system prompt states this and the
schema enforces it — there is nowhere for an unsupported concept to hide.

### Honesty rules (enforced, not aspirational)

1. Every unexpressible request must appear in `unsupported_concepts` — the
   prompt forbids silent drops and the review UI shows the list verbatim.
2. Every server-side fixup appends to `warnings` (e.g. tick-stop with no
   distance -> structure stop; no daily cap -> safety default 5/day).
3. `confidence` is the model's own faithfulness estimate (0–1), lowered per
   unsupported/approximated concept.
4. `engine_version` is computed server-side from `ict_setup`, so the model
   cannot claim a dedicated setup that isn't registered.

### Ops

- Flag: `ENABLE_AI_BUILDER_V2` (default off -> route 404s), read per-request.
- Model: `AI_BUILDER_MODEL` (default `claude-sonnet-5`); key: `ANTHROPIC_API_KEY`
  — same env pattern as support-chat. Missing key -> 503, upstream failure
  -> 502 (including a response missing the forced tool call — an upstream
  anomaly, never blamed on the prose), un-compilable prose after the retry
  -> 422 with the validator errors.
- Cost: 2 calls max per request (1 + 1 retry), ~2k output tokens cap.
- Tests: `backend/tests/test_ai_builder_v2.py` — anthropic fully mocked,
  no DB, runs in the ephemeral container.

## C) Frontend follow-up plan (not in this change)

`AIStrategyBuilder.tsx` should become a thin client of the new endpoint:

1. Keep `quickScan()` ONLY as a live typing preview, relabeled as such —
   delete the "same logic on the server" comment (it now would undersell,
   not oversell).
2. On "Review": POST `/api/v1/strategies/generate-v2` with the combined
   step prose; render the draft honestly —
   * the knob table from `generated` (instruments, sessions, RR, BE, stops,
     filters, setup template),
   * `explanation` as the summary paragraph,
   * `confidence` as a visible meter,
   * `unsupported_concepts` as a red "the engine CANNOT do these" list,
   * `warnings` as an amber "the compiler changed these" list.
   This replaces the current PE-HONESTY-V2 banner, which could only name
   2 knobs because only 2 existed.
3. On "Save": POST the returned `strategy_payload` (user-edited) through the
   existing `strategiesApi.create` — the draft status means it lands
   unpublished; Activate stays an explicit step.
4. Flag-off behavior: on 404 from generate-v2, fall back to today's
   client-side compile so the page keeps working while the flag is dark.
5. Retire the hardcoded RR 2.5 / max_contracts 10 defaults once the endpoint
   is the primary path.
