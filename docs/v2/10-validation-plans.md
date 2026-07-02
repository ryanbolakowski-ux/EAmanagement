# Expected Benefits, Risks & Validation — per major change (2026-07-02)

Format: benefit → risk → validation gate (what must be TRUE before it touches production behavior).

## Scanner V2
Benefit: replaces measurably anti-predictive scoring; expected fewer CAST-type picks, higher expectancy/pick. Risk: new weights are hypotheses from n=41; could underperform V1 short-term. Gate: runs shadow-only alongside V1 ≥30 resolved picks; promote only if expectancy_v2 > expectancy_v1 on the SAME days AND max single-pick loss ≤ V1. Rollback: flip SCANNER_V2_SHADOW_ENABLED=0 — zero prod coupling.

## Walk-forward optimizer
Benefit: params that generalize; kills curve-fit churn. Risk: OOS ranking on short windows is noisy. Gate: oos_fraction=0 byte-parity with V1 (tested); pilot on 2 strategies, 2-week forward check vs V1 params before defaulting oos_fraction>0.

## Task supervisor
Benefit: watchers/schedulers self-heal; silent-death class eliminated. Risk: restart loop on a persistent crasher → alert spam (dedup + max-restarts cap mitigate). Gate: 1 week in prod with restart counts in logs reviewed; flag TASK_SUPERVISOR_ENABLED=0 reverts to V1 behavior instantly.

## Bounded caches
Benefit: memory ceiling; kills the user-growable _preview_chain_cache vector. Risk: maxsize too small → hit-rate drop → more upstream calls. Gate: log evictions for a week; sizes tuned so steady-state never evicts hot keys.

## KYC passcode gate
Benefit: closes verified admin-JWT-only override. Risk: none beyond one extra passcode prompt for Ryan. Gate: unit test + one manual admin flow check.

## AI Builder V2
Benefit: user prose actually compiles to real strategy knobs + honest unsupported-concepts list; trust + differentiation. Risk: LLM cost/latency; hallucinated knobs (schema-validated, so fail-closed). Gate: flag off by default; golden-prompt test suite green; Ryan runs ~10 real prompts and judges outputs before flag-on; per-user rate limit before public.

## LandingV2 / DashboardV2
Benefit: conversion-grade first impression; dashboard that feels alive with honest states everywhere. Risk: V2 pages call existing endpoints only — regression risk isolated to new routes. Gate: tsc green, Ryan click-through on preview, then A/B or hard-switch / at Ryan's call. V1 pages untouched either way.

## Strategy V2 variants (harness + seeds)
Benefit: measures V1 strategies under honest execution (2-tick slippage, OOS); V2 rows coexist as drafts. Risk: none until a V2 row is activated manually. Gate: harness comparison table reviewed per strategy; only strategies whose V2 beats V1 OOS get activated, one at a time.
