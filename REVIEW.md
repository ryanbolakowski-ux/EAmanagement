# THETA ALGOS V2 — REVIEW & TEST-DRIVE GUIDE
**Branch:** `v2-redesign` · **Built:** 2026-07-02 · **Status: ready for your approve/deny**

Production is untouched. Everything here coexists with V1 — approving is a deliberate deploy step; denying costs nothing.

## Verification status (independently re-run by orchestrator, not just agent claims)
- **Backend: 84/84 new tests green** in one clean run (ephemeral container, prod image)
- **Frontend: tsc --noEmit → 0 errors** (all V2 pages/components + wiring)
- Every track passed adversarial review; 2 tracks needed a fix round (issues fixed, re-verified)
- Full audit trail: `docs/v2/` (forensics → audits → adversarial verification appendix → specs)

## What's in the box
| Track | What it does | Off switch |
|---|---|---|
| **Scanner V2** (`backend/app/engines/scanner/v2/`) | Rebuilt ranking from measured data (V1 score was anti-predictive): rel-vol dominant, gap penalty curve, $1M premarket liquidity+catalyst gate, 10:00 ET hard close. Runs SHADOW-only, persists `v2:*` picks for forward-test | `SCANNER_V2_SHADOW_ENABLED=0` |
| **Backend hardening** | Task supervisor (crashed loops auto-restart + alert), bounded caches, executor-wrapped network calls, **walk-forward optimizer** (`oos_fraction`, default 0 = V1 parity), KYC passcode gate | `TASK_SUPERVISOR_ENABLED=0`; oos_fraction=0 |
| **Frontend V2** (`/v2`, `/app/v2`) | Institutional design system + component kit + full LandingV2 + DashboardV2, lazy-loaded; V1 pages byte-identical | V2 routes are additive |
| **Strategy V2 harness** | V1-vs-V2 comparison under honest execution (2-tick slippage, OOS split); guarded seeder for `<name> V2` draft rows | Scripts only run by hand |
| **AI Builder V2** | Real prose→strategy compiler (Claude → schema-validated rule_tree + honest `unsupported_concepts`); returns DRAFTS only | `ENABLE_AI_BUILDER_V2` off by default → 404 |

## TEST IT YOURSELF (copy-paste, all safe: read-only or ephemeral containers)

**0) Run the whole V2 test suite (what I ran):**
```
ssh edge 'IMG=$(docker inspect edge_backend --format "{{.Image}}"); docker run --rm -v /root/worktrees/v2-redesign/backend:/app -w /app "$IMG" sh -c "pip install -q pytest && python -m pytest tests/test_scanner_v2_scoring.py tests/test_scanner_v2_gates.py tests/test_ttl_cache.py tests/test_task_supervisor.py tests/test_optimizer_walkforward.py tests/test_strategy_v2_harness.py tests/test_ai_builder_v2.py -q | tail -3"'
```

**1) Scanner V2 — score a candidate and see WHY (the explainability V1 never had):**
```
ssh edge 'IMG=$(docker inspect edge_backend --format "{{.Image}}"); docker run --rm -v /root/worktrees/v2-redesign/backend:/app -w /app "$IMG" python -c "
from app.engines.scanner.v2.scoring import score_v2
cast = {\"ticker\":\"CAST\",\"price\":6.72,\"gap_pct\":25.0,\"rel_vol\":3.4,\"premarket_dollar_vol\":180000,\"day_pct\":25.0}
irdm = {\"ticker\":\"IRDM\",\"price\":54.59,\"gap_pct\":8.0,\"rel_vol\":41.0,\"premarket_dollar_vol\":2100000,\"day_pct\":8.0}
for c in (cast, irdm):
    b = score_v2(c, {\"qqq_day_pct\":0.4,\"qqq_above_prev_close\":True})
    print(c[\"ticker\"], round(b.total,1)); print(\"  \", b.why()); print()
"'
```
(CAST — the −20% pick — scores LOW under V2; IRDM scores high. That's the fix, visible.)

**2) Scanner V2 — fire gates (the no-more-6AM-pumps rule):**
```
ssh edge 'IMG=$(docker inspect edge_backend --format "{{.Image}}"); docker run --rm -v /root/worktrees/v2-redesign/backend:/app -w /app "$IMG" python -c "
from app.engines.scanner.v2.gates import decide_fire
thin = {\"premarket_dollar_vol\": 180000, \"catalyst_reason\": None, \"confirmed\": False}
liquid = {\"premarket_dollar_vol\": 2_500_000, \"catalyst_reason\": \"8-K filing\", \"confirmed\": True}
for label, mins in ((\"06:00\",360),(\"09:32\",572),(\"09:40\",580),(\"10:05\",605)):
    print(label, \"thin:\", decide_fire(mins, thin).allowed, \"| liquid:\", decide_fire(mins, liquid).allowed)
"'
```

**3) Strategy harness — V1 vs V2 execution assumptions (synthetic, no DB):**
```
ssh edge 'IMG=$(docker inspect edge_backend --format "{{.Image}}"); docker run --rm -v /root/worktrees/v2-redesign/backend:/app -w /app "$IMG" python -m scripts.strategy_v2_harness --synthetic'
```
Also see the committed sample output: `docs/v2/strategy-v2-comparison.csv` / `.md`.

**4) Seeder safety proof (refuses to touch the DB without double confirmation):**
```
ssh edge 'IMG=$(docker inspect edge_backend --format "{{.Image}}"); docker run --rm -v /root/worktrees/v2-redesign/backend:/app -w /app "$IMG" python -m scripts.seed_strategy_v2 --execute; echo "exit=$?"'
```

**5) AI Builder V2 — LIVE generation with your real key (~1 cent of API):**
```
ssh edge 'IMG=$(docker inspect edge_backend --format "{{.Image}}"); KEY=$(docker exec edge_backend printenv ANTHROPIC_API_KEY); docker run --rm -e ANTHROPIC_API_KEY="$KEY" -v /root/worktrees/v2-redesign/backend:/app -w /app "$IMG" python -c "
import asyncio, json
from app.engines.ai_builder.generator import generate_strategy
g = asyncio.run(generate_strategy(\"Trade NQ during the London session. Enter on a liquidity sweep into a fair value gap, 2:1 reward to risk, move to break even at 0.5R, max 3 trades a day. Also use gamma exposure to filter.\"))
print(json.dumps(g.model_dump(), indent=2, default=str))
"'
```
Watch for: correct knobs compiled AND `gamma exposure` honestly listed in `unsupported_concepts` — that honesty is the whole point of V2.

**6) Frontend V2 — visual review:**
- Branch is pushed → if Vercel watches the repo, a **preview deployment** for `v2-redesign` appears in your Vercel dashboard. Open `<preview-url>/v2` (homepage) and log in → `<preview-url>/app/v2` (dashboard).
- Local fallback: `cd frontend && npm run dev` on the branch → http://localhost:5173/v2 and /app/v2.
- UX checklist while you click: hero answers what/trust/why in one screen? every dashboard panel shows skeleton→data (or a designed empty state)? kill your network briefly — panels show error cards, not a blank page? mobile width? numbers aligned (tabular)?

## Honest caveats (read before approving)
1. **Scanner V2 weights are hypotheses from n=41** — that's why it ships shadow-only. The promotion gate is ≥30 resolved v2 picks beating V1 expectancy on the same days. Don't skip it.
2. **AI Builder returns drafts and is flag-off** — turning it on for customers should wait until you've judged ~10 real generations + a per-user rate limit exists.
3. Shadow's premarket/9:40 fire probes run at 09:45 scan-time on the 15-min-delayed feed — slightly optimistic; recompute live before promotion (noted in code).
4. V2 scores (0–100) are NOT comparable to V1 thresholds — never mix scales.
5. Minor non-blocking review notes were accepted as-is (logged in `docs/v2/` reports): e.g., harness summary-count cosmetics, no score floor on v2 shadow rows.
6. Pre-existing failure in `test_theta_scanner_no_pick_alert.py` (date-freeze artifact) exists on prod tree too — not from V2.

## APPROVE path (when you say go — I do it, one track at a time)
1. Backend: point the prod bind-mount at `v2-redesign` (or merge → prod branch) + restart after close → supervisor, caches, KYC gate, walk-forward (parity default) + scanner v2 SHADOW go live.
2. Frontend: merge to main → Vercel → `/v2` routes live (V1 untouched); switch `/` → LandingV2 only when you're happy.
3. Scanner V2 emits to users ONLY after the 30-sample shadow gate clears. AI Builder flag stays off until your call.

## DENY path
Do nothing. Prod never changed. The branch + full audit trail stay for reference.
