# V2 Design Specification — homepage, dashboard, branding, motion (2026-07-02)

## Brand direction: "institutional terminal"
Positioning: Bloomberg-grade seriousness, Linear-grade execution, zero TradingView cosplay. The product sells DISCIPLINE (gates, no-trade days, measured stats) — the design language must embody restraint.
- Surfaces: near-black graphite scale (#0b0e14 base), hairline borders, elevation by border+shadow not color blocks. Light mode = full token mapping, dark-first.
- Accent: ONE electric blue. Semantic green/red reserved exclusively for P&L/direction — never decorative.
- Type: existing system/Inter stack; ALL numerics tabular-nums; data-dense tables at 13px with generous row height.
- Iconography: lucide (existing), 16/20px, never filled.
- Voice: honest and specific ("39% of picks hit target; winners avg +10.3%") beats hype ("massive gains daily"). No fabricated testimonials/stats — placeholders are flagged and hidden by default.

## Homepage V2 (built: pages/v2/LandingV2.tsx at /v2)
Narrative order: (1) what it is + CTA in one viewport, (2) proof strip (real numbers only), (3) how it works in 3 steps, (4) product showcase (scanner/futures/AI), (5) strategy previews, (6) pricing (existing tiers verbatim), (7) trust/disclosures, (8) final CTA.
Conversion logic: single primary CTA per viewport; secondary is always "see proof". Scroll reveals ≤320ms; zero motion for prefers-reduced-motion.

## Dashboard V2 (built: pages/v2/DashboardV2.tsx at /app/v2)
Top strip: equity / day P&L / open positions / win-rate StatCards (LiveNumber). Then: market bias + regime card · today's pick card · strategy health rows · positions table · activity feed. Rules: every panel has skeleton→empty→error states; ErrorBoundary isolation per panel (one dead panel never blanks the page); all live numbers tabular + semantically colored; staleTime < refetchInterval.

## Motion spec
Tokens: 120ms (hover/press), 200ms (reveal/fade), 320ms (page/section), cubic-bezier(0.2,0,0,1). Allowed: opacity/transform only (compositor-safe). Counters: rAF tween ≤600ms, once per mount. Forbidden: parallax, looping attention-seekers outside the landing hero, animating layout properties, anything not disabled by prefers-reduced-motion.

## Component kit (built: components/v2/)
ErrorBoundary · ToastProvider/useToast · BaseModal (focus trap/esc/scroll-lock) · Skeleton · EmptyState · StatCard · LiveNumber · Sparkline (dep-free SVG) · SectionHeader · sanitizeHtml (interim until DOMPurify approved).
Adoption rule for V1 pages (post-approval): any page touched for another reason migrates its modals/loading/empty/error states to the kit in the same PR — no big-bang rewrite.
