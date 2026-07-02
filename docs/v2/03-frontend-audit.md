# Frontend Audit — Theta Algos V2 program (2026-07-01)

Read-only audit of frontend/src. 13,364 LOC pages, ~4,482 LOC components, 26 reusable components.

## Scores
| Category | Score | Note |
|---|---|---|
| Pages & routes | 7/10 | Well-organized; incomplete LiveTrading V1→V2 migration |
| Styling system | 7/10 | Tailwind + CSS vars, good dark mode; 47 hardcoded hex colors |
| Component quality | 6/10 | Good Layout/TradeMetrics reuse; modal/chart duplication |
| Loading/empty/error states | 5/10 | Inconsistent; several pages fail silently |
| Animations | 8/10 | CSS-based, respects prefers-reduced-motion; no motion system |
| Data layer | 7/10 | React-Query + Zustand; polling-only, no WebSocket |
| Responsiveness | 6/10 | Manual DevicePicker, not viewport-based; no tablet breakpoints |
| Performance | 4/10 | No code-splitting; ~600KB gz bundle, all 40+ pages static-imported |
| Accessibility | 4/10 | 16 aria attrs total; no focus traps; no aria-live for live P&L |
| Security | 5/10 | dangerouslySetInnerHTML on backend HTML (see verification note) |

## Page inventory (key routes)
Landing 596 LOC · Dashboard 332 · StrategyBuilder 1,260 · AIStrategyBuilder 535 · HowToTrade 283 · Backtests 568 · Optimization 297 · PaperTrading 660 · **LiveTradingV2 1,612** · **LiveTrading (legacy, still routed at /app/live/classic) 1,061** · AccountSignals 770 · Admin 772 · Profile 683 · + auth/legal/options/kyc pages. 50+ routes, all statically imported (zero lazy loading).

## Top issues (ranked, user-visible impact)
1. 🔴 Duplicate Live Trading implementations (V1 1,061 + V2 1,612 LOC) both shipped & routed — maintenance hazard, bundle bloat.
2. 🔴 No lazy route loading — all pages in one ~600KB gz bundle; unauthenticated visitors download Admin + trading engine code. Fix: React.lazy per route (~60% bundle cut).
3. 🔴 Silent API errors on many mutations (Kyc, Profile, Options, SharedStrategy) — user clicks save, nothing happens. No global toast/notification system, no ErrorBoundary anywhere (component crash = blank screen).
4. 🔴 dangerouslySetInnerHTML without sanitization (Profile.tsx legal-doc render, AcknowledgmentModal) — backend HTML rendered raw. Add DOMPurify. [VERIFIED — see note]
5. 🟠 Polling-only live data: 10+ queries × 30s intervals (thundering herd every 30s); live P&L lags fills. WebSocket/SSE is the V2 move.
6. 🟠 No shared modal wrapper — 6 modals each reimplement scroll-lock/escape/focus; stacking broken.
7. 🟠 Two chart implementations (custom SVG Candlestick 288 LOC + Recharts CandlestickChart 254 LOC) — no canonical chart layer.
8. 🟠 47 hardcoded hex colors bypass the CSS-var theme — dark-mode flips fail in spots; rebrand requires code edits.
9. 🟡 staleTime 30s == refetchInterval 30s (wasted refetches); no invalidation strategy; no revalidate-on-focus.
10. 🟡 No skeleton/empty-state components — animate-pulse divs & "No data" strings copy-pasted (8+ / 16 pages).
11. 🟡 Auth init not awaited → login-page flash race on refresh.
12. 🟡 Tier map duplicated client-side (authStore TIER_ORDER) — silently breaks if backend tiers change.
13. 🟡 Mutations lack in-flight button states → double-submits.
14. 🟡 Mobile = manual device picker, not responsive viewport; tablets get desktop layout.
15. 🟡 25 .bak.* files polluting src (App.tsx ×5, LiveTradingV2 ×14…) — add *.bak.* to .gitignore, clean.

## Styling system detail
CSS vars in :root (--bg/--surface/--border/--text-1..3/--blue/--green/--red/--amber); Tailwind 3.4.4; class-based dark mode with !important safety nets; zustand themeStore persisted to localStorage. Mixed approaches: ~70% Tailwind utilities, 20% .card/.btn-*/.badge-* utility classes, 10% inline styles. Slate family used ad hoc (20+ shades) outside tokens.

## Animation inventory
Custom keyframes (edge-fade-up/-in, edge-scale-in, edge-bg-grid 24s, edge-blue-pulse, edge-line-grow) + .reveal scroll-trigger on Landing. Tailwind animate-spin ×14 / pulse ×8 / bounce ×3; transition-* 40+. No framer-motion. Recharts renders static. prefers-reduced-motion respected globally.

## Data layer detail
Axios + interceptors (401→logout, 451→blocked, 403 kyc/2fa→redirects; all other errors silently rejected). 41 refetchInterval instances (30s live accounts/positions/signals; 3–5s optimization polls; 5m bias). React-Query defaults retry:1, staleTime:30s. Zustand: authStore (tier gating via TIER_ORDER), themeStore.

## Performance detail
recharts ~300KB gz (biggest dep), lucide-react, react-query, zod. No React.lazy anywhere. Estimated FMP 2–3s / TTI 3–4s on 3G. LiveTradingV2 renders sparklines for 50+ positions. Layout gates re-run every location change (no memo).

## Accessibility detail
16 aria/role attrs total across codebase; 18 clickable divs; no focus trap in modals; no aria-live regions for live-updating P&L; focus-visible outline defined globally (good); contrast risks: slate-500-on-slate-100 dark mode, amber badge combos.

## Verification note (added by orchestrator)
The XSS claim (#4) was independently verified against source: see 03a note appended after verification pass.
