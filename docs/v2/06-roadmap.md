# V2 Prioritized Roadmap — impact × effort (2026-07-02)

Everything below exists on branch v2-redesign (built) or is specced. Promotion of ANY item = Ryan approval + its validation gate passing. P0 = do first after approval.

## P0 — Revenue-critical, built, validation-gated
| Item | Why | Validation gate before it goes live |
|---|---|---|
| Scanner V2 (shadow) | V1 score is measurably ANTI-predictive; V2 reweights to measured signal (rel-vol), caps gap, hard fire window | ≥30 resolved shadow picks AND v2 expectancy > V1 over same days |
| Real-time stock feed (vendor pending) | Root cause of dark days + late entries; unblocks Oracle-style 9:35 picks | First week: confirmation rate at 9:35–10:00 >0 daily |
| Walk-forward optimizer | V1 optimizer curve-fits by construction | oos_fraction=0 parity proven by tests; then re-optimize 2 strategies and forward-check 2 weeks |

## P1 — Trust & stability (built)
| Item | Why |
|---|---|
| Task supervisor | Crashed watchers currently stay dead silently until restart |
| Bounded caches | _preview_chain_cache is user-growable memory; rest leak stale entries |
| KYC manual-override passcode gate | Verified: admin JWT alone could flip KYC status |
| Executor-wrapped watcher network calls | Event-loop blocking = the systemic cadence disease |
| Slippage re-baseline (harness) | Backtest 1-tick assumption inflates PF 5–15% vs live |

## P2 — Product & conversion (built)
| Item | Why |
|---|---|
| LandingV2 | Homepage must answer what/trust/why-subscribe in one viewport; current 596-LOC page doesn't |
| DashboardV2 | Density + live-feel + strategy health in one place |
| AI Builder V2 backend | "Build in Plain English" currently discards most user intent silently — trust liability |
| Component kit (toasts/error boundaries/modals/skeletons) | Silent failures are the #1 UX defect class found |

## P3 — Follow-ups (specced, not built)
- WebSocket/SSE live data channel (kills the 30s polling herd)
- Route-level code splitting for ALL V1 pages (~60% bundle cut) — V2 pages already lazy
- Delete LiveTrading.tsx legacy page after V2 parity check
- DOMPurify as approved dependency (replace interim sanitizer)
- TOTP backup codes; SECRET_KEY rotation strategy; git-history purge + TwelveData key rotation (STILL PENDING, Ryan)
- Watcher pooling (one watcher per instrument shared across users) — the 100-user architecture
- Options data feed → unlock 4 options templates + accurate options-paper P&L
- Scanner rebrand rollout (recommend: Bellwether) after Ryan picks

## Explicitly NOT recommended
- Forcing a pick every day (quality bar exists for a reason — no-pick days are correct)
- Promoting any shadow template below the 30-sample gate
- Real CME feed before subscriber revenue justifies ~$740/mo (QQQ-structure emails cover futures until then)
