# Performance Optimization Report (2026-07-02)

## Frontend (measured from code audit)
| Problem | Evidence | V2 status |
|---|---|---|
| ~600KB gz bundle, zero code-splitting | all 50+ routes statically imported in App.tsx | V2 routes lazy-loaded w/ Suspense (built); V1 conversion = P3 roadmap (est. −60% initial bundle) |
| Duplicate LiveTrading pages (2,673 LOC combined) | /app/live + /app/live/classic both shipped | flagged P3 delete after parity check |
| 30s staleTime == 30s poll (wasted refetches ×41 sites) | main.tsx defaults | V2 pages set staleTime < poll; V1 = follow-up |
| Thundering-herd polling, no push channel | 10+ queries × 30s | WebSocket/SSE specced (P3) |
| recharts ~300KB always loaded | imported by 3 pages statically | V2 uses dep-free SVG Sparkline; recharts isolates behind lazy routes when V1 splits |

## Backend (measured)
| Problem | Evidence | V2 status |
|---|---|---|
| Event-loop blocking sync HTTP | yfinance 2–10s under global lock; Polygon/Alpaca unwrapped | executor-wrapped in V2 (built); yfinance lock kept (rate-limit shield) |
| ~90-min scheduler cadence | measured 6/30; fast-loop hotfix live in prod | supervisor + wrapped calls address the disease, not just the symptom |
| Unbounded caches | 6 dicts, 2 realistically growable (verified) | TTLCache bounds all 6 (built) |
| Crashed tasks stay dead | no supervision anywhere | task_supervisor auto-restart w/ backoff + alert (built) |
| Optimizer grid = in-sample only | opt_worker.py verified split-free | walk-forward oos_fraction (built, parity default) |
| Pool 10+20 vs watcher fan-out | prior 70-socket leak incident (fixed) | monitor; watcher-pooling is the real fix at scale (P3) |

## Scale verdicts (unchanged from audit)
10 users: fine after V2. ~100 users: needs watcher pooling + WebSocket + read-path offload. 1,000: worker processes + replicas. All specced in roadmap P3, none blocking approval.
