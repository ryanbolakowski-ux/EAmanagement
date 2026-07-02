import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Activity, ArrowRight, BarChart2, Bot, CheckCircle2, ChevronRight,
  Crosshair, FileText, FlaskConical, Lock, ShieldCheck,
  TrendingUp, Zap,
} from 'lucide-react'
import ThetaLogo from '../../components/ThetaLogo'
import { StatCard, LiveNumber, TickerTape } from '../../components/v2'

// ═════════════════════════════════════════════════════════════════════════
// Published platform facts — every figure below already appears on the V1
// landing (pages/Landing.tsx) or pricing (pages/Pricing.tsx) pages.
// HARD RULE for this page: no performance claims (win rates, P&L, returns).
// The decorative SVGs further down are deliberately unlabeled for the same
// reason. Anything we WANT to show but cannot source yet is parked here as
// a TODO-VERIFY-CLAIM constant and NOT rendered.
// ═════════════════════════════════════════════════════════════════════════
const TICKERS_SCANNED = 336               // intraday options-scanner universe (V1 hero + stats bar)
const SCAN_CADENCE_MIN = 5                // minutes between sweeps (V1 stats bar)
const COVERAGE_WINDOW = '4 AM – 8 PM'     // ET scanning window (V1 stats bar)
const NEWS_BLACKOUT = 'FOMC + CPI'        // headline auto-skipped events (V1 stats bar)
const PREMARKET_UNIVERSE = '3,000+'       // pre-market scanner universe (V1 pricing, Tier 3+)

// TODO-VERIFY-CLAIM: signals delivered to date, active subscribers, uptime %.
// None of these have an audited backend source yet, so no card renders them.

// Fabricated testimonials are a compliance risk. This stays false until real,
// verifiable customer quotes (with written permission on file) exist — the
// section below renders clearly-marked placeholders even then.
const SHOW_TESTIMONIALS: boolean = false

// ── Pricing — mirrors pages/Pricing.tsx TIERS verbatim (names, prices, copy,
//    highlight). If Pricing.tsx changes, this must change with it; the full
//    comparison table still lives at /pricing. ─────────────────────────────
const PRICING_TIERS = [
  {
    id: 'free_trial',
    name: 'Free Trial',
    tierLabel: 'Tier 1',
    price: '$0',
    originalPrice: '$49',
    per: 'per month',
    promoNote: '30 days · no card required',
    desc: 'See what the bot picks every morning at 8:30 ET. Full futures signals + options scanner preview, paper trading only.',
    highlight: false,
    tag: undefined as string | undefined,
    features: [
      'Strategy builder + backtesting',
      'Paper trading',
      'Futures signals (Apex/TPT/Topstep)',
      'Options scanner — preview only',
      '500-ticker universe · 1 yr history',
    ],
  },
  {
    id: 'tier_2',
    name: 'Futures Signals',
    tierLabel: 'Tier 2',
    price: '$49',
    originalPrice: undefined as string | undefined,
    per: 'per month',
    promoNote: undefined as string | undefined,
    desc: 'For prop-firm traders (Apex, TPT, Topstep). ICT-based signals on ES/NQ/RTY/YM as email + push — you place trades manually inside your prop rules.',
    highlight: false,
    tag: undefined as string | undefined,
    features: [
      'Strategy builder + backtesting',
      'ICT signals on ES/NQ/RTY/YM',
      'Email + push delivery',
      '2+ years historical data',
      'Email support',
    ],
  },
  {
    id: 'tier_3',
    name: 'Options Scanner',
    tierLabel: 'Tier 3',
    price: '$99',
    originalPrice: undefined as string | undefined,
    per: 'per month',
    promoNote: undefined as string | undefined,
    desc: 'The full 3,000+ ticker pre-market scanner. Daily 8:30 ET email with 1 top pick + 4 runners-up. You place the trade in your broker.',
    highlight: false,
    tag: undefined as string | undefined,
    features: [
      'Everything in Tier 2',
      '3,000+ ticker pre-market scanner',
      'Daily 1 pick + 4 runners-up email',
      'Low-Float, Breakout, Gap, Oracle',
    ],
  },
  {
    id: 'tier_4',
    name: 'Options Live',
    tierLabel: 'Tier 4',
    price: '$199',
    originalPrice: undefined as string | undefined,
    per: 'per month',
    promoNote: undefined as string | undefined,
    desc: 'Same scanner — but Confirm now places real orders through your connected Tradier account. Live greeks, real bid/ask.',
    highlight: true,
    tag: 'Most Popular' as string | undefined,
    features: [
      'Everything in Tier 3',
      'Tradier broker integration',
      'Confirm places real orders',
      'Priority email support',
    ],
  },
  {
    id: 'tier_5',
    name: 'Fully Automated',
    tierLabel: 'Tier 5',
    price: '$399',
    originalPrice: undefined as string | undefined,
    per: 'per month',
    promoNote: undefined as string | undefined,
    desc: 'Zero clicks. The bot scans, picks, sizes, places, manages, and exits — automatically. Multi-strategy concurrent execution including the Wheel.',
    highlight: false,
    tag: undefined as string | undefined,
    features: [
      'Everything in Tier 4',
      'Auto-execute — no manual confirm',
      'Multi-strategy concurrent + Wheel',
      '5+ years historical data',
      'Priority + chat support',
    ],
  },
]

// ── Strategy preview cards — names + honest descriptive copy lifted from the
//    V1 feature grid. Deliberately NO win rates / P&L / trade counts. ───────
const STRATEGIES = [
  {
    icon: Zap,
    market: 'Stocks · Options',
    name: 'Pre-Market Gap Scanner',
    desc: 'From 4:00 ET, hunts stocks gapping up 5%+ on 100K+ shares of pre-market volume. The top movers, ranked by volume, land in your inbox at 8:30 ET — an hour before the open.',
  },
  {
    icon: TrendingUp,
    market: 'Stocks · Options',
    name: 'Oracle 5-Min Opening Candle',
    desc: 'Tracks the first 5-minute candle on every gapper. At 9:35 ET it emits the setup: VWAP-based bias, entry at the opening-candle high or low, stop at the other end, target at 2× risk.',
  },
  {
    icon: Activity,
    market: 'Stocks',
    name: 'Low-Float Squeeze',
    desc: 'Filters for stocks under $20 with float under 10M shares, a 5%+ pre-market or 10%+ intraday move, and a positive text catalyst on the wire. Fires only when every box checks.',
  },
  {
    icon: BarChart2,
    market: 'Stocks',
    name: '52-Week Breakout + RSI',
    desc: 'Catches stocks within 2% of their 52-week high on 300%+ volume versus the 20-day average, with RSI confirming. Entry triggers when a 1-minute candle closes above resistance.',
  },
  {
    icon: Crosshair,
    market: 'Futures',
    name: 'ICT Sweep + FVG',
    desc: 'Liquidity-sweep detection, Fair Value Gaps, inverse-FVG confirmation on the lower timeframe, session filters, and structure-based stops — chained across timeframes on ES, NQ, RTY and YM.',
  },
  {
    icon: Bot,
    market: 'Options',
    name: 'Strike Picker + Wheel',
    desc: 'When a directional signal fires, the engine picks the contract: 30–60 DTE, delta 0.30–0.50, factoring IV and stop distance. Trend Pullback, Breakout, Vertical Spread, Earnings Catalyst and Wheel modes are pre-built.',
  },
]

// ── How it works — V1's four steps condensed to three, same claims. ────────
const STEPS = [
  {
    title: 'Pick your strategies',
    desc: 'Turn on the built-in scanners and futures setups, or assemble your own in the strategy builder — liquidity sweeps, FVGs, session filters, risk/reward rules. No code required.',
  },
  {
    title: 'Validate before you risk',
    desc: 'Backtest against years of ES and NQ history with realistic slippage and commissions, then paper trade the same rules on live market data until the edge is proven to you.',
  },
  {
    title: 'Go live, your way',
    desc: 'One-click confirm from your inbox, fully automated execution through a connected broker, or manual signal alerts that stay inside prop-firm rules. Kill switch any time.',
  },
]

// ── Trust strip — security posture + legal stance from the V1 landing. ─────
const TRUST_ITEMS = [
  {
    icon: Lock,
    title: 'Encrypted credentials',
    desc: 'Broker API keys are encrypted at rest with Fernet (AES-128) before they touch the database — never stored in plaintext. JWT-authenticated sessions with refresh rotation.',
  },
  {
    icon: ShieldCheck,
    title: 'Risk controls by default',
    desc: 'Hard daily-loss cap and a one-click kill switch that cancels open orders immediately. Automatic news blackout ±30 min around FOMC, CPI, PPI, NFP, GDP and Retail Sales.',
  },
  {
    icon: FileText,
    title: 'Not financial advice',
    desc: 'Theta Algos LLC is a software platform — not a registered investment adviser, broker-dealer, or futures commission merchant. Nothing on this site is investment advice.',
  },
]

// Verbatim from the V1 landing footer — keep in sync with pages/Landing.tsx
// and the /disclosures page if legal copy changes.
const RISK_DISCLOSURE =
  'Futures and options trading involve substantial risk of loss and are not appropriate for every investor. ' +
  'Options can expire worthless — you can lose 100% of the premium paid on a trade. Forex (coming soon) carries similar risk. ' +
  'Past performance, backtest results, and paper-trading metrics do not guarantee future returns. ' +
  'Theta Algos LLC is a software platform — not a registered investment adviser, broker-dealer, or futures commission merchant. ' +
  'Nothing on this site is investment advice. Prop-firm rules change frequently and most prohibit automated trading; ' +
  'account closures from rule violations are the user\'s sole responsibility. Always trade with capital you can afford to lose.'

// ── Scroll reveal — same `.reveal` / `.is-visible` contract as the V1
//    landing (pages/Landing.tsx); v2.css retimes the transition with the V2
//    motion tokens inside .v2-root. ────────────────────────────────────────
function useScrollReveal() {
  useEffect(() => {
    const els = Array.from(document.querySelectorAll<HTMLElement>('.v2-root .reveal'))
    if (typeof window === 'undefined' || !('IntersectionObserver' in window)) {
      // No IntersectionObserver: show everything rather than hiding content
      els.forEach((el) => el.classList.add('is-visible'))
      return
    }
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) {
            e.target.classList.add('is-visible')
            io.unobserve(e.target)
          }
        })
      },
      { threshold: 0.12 },
    )
    els.forEach((el) => io.observe(el))
    return () => io.disconnect()
  }, [])
}

// Count-up wrapper: mounts at 0 and tweens to the real figure via LiveNumber
// (which already snaps instantly under prefers-reduced-motion).
function CountUp({
  target,
  format,
  duration = 900,
}: {
  target: number
  format?: (v: number) => string
  duration?: number
}) {
  const [value, setValue] = useState(0)
  useEffect(() => {
    const id = window.setTimeout(() => setValue(target), 150)
    return () => window.clearTimeout(id)
  }, [target])
  return <LiveNumber value={value} format={format} duration={duration} />
}

// ═════════════════════════════════════════════════════════════════════════
// Decorative product visuals — hand-drawn SVGs on v2 tokens (theme-aware),
// deliberately unlabeled: no tickers, no prices, no P&L. aria-hidden.
// ═════════════════════════════════════════════════════════════════════════

/** Scanner: ranked result rows with up/down status dots; a CSS sweep line
 *  (see .v2-lp-scan-sweep in v2.css) passes over them like a radar. */
function ScannerVisual() {
  const rows = [
    { w: 58, up: true, hot: false },
    { w: 42, up: false, hot: false },
    { w: 84, up: true, hot: true },
    { w: 36, up: true, hot: false },
    { w: 50, up: false, hot: false },
  ]
  return (
    <>
      <svg viewBox="0 0 220 132" className="v2-lp-visual" aria-hidden="true" focusable="false">
        {rows.map((r, i) => {
          const y = 8 + i * 25
          return (
            <g key={i}>
              <rect
                x="8" y={y} width="204" height="19" rx="5"
                fill={r.hot ? 'var(--v2-accent-bg)' : 'var(--v2-surface-1)'}
                stroke={r.hot ? 'var(--v2-accent-border)' : 'var(--v2-border)'}
              />
              <circle cx="21" cy={y + 9.5} r="3.5" fill={r.up ? 'var(--v2-up)' : 'var(--v2-down)'} />
              <rect x="32" y={y + 6.5} width="30" height="6" rx="3" fill="var(--v2-surface-3)" />
              <rect
                x={204 - r.w} y={y + 6.5} width={r.w} height="6" rx="3"
                fill={r.up ? 'var(--v2-up)' : 'var(--v2-down)'}
                opacity={r.hot ? 0.9 : 0.4}
              />
            </g>
          )
        })}
      </svg>
      <div className="v2-lp-scan-sweep" aria-hidden="true" />
    </>
  )
}

/** Futures: a sell-off that sweeps the prior low, leaves an FVG on the way
 *  back up, and the entry/stop/target rails around the retrace. */
function FuturesVisual() {
  // [x, bodyTop, bodyBottom, wickTop, wickBottom, up]
  const candles: Array<[number, number, number, number, number, boolean]> = [
    [16, 30, 48, 26, 52, false],
    [34, 44, 60, 40, 64, false],
    [52, 56, 72, 52, 78, false],
    [70, 68, 80, 64, 104, true],   // sweep candle — long lower wick under prior low
    [88, 50, 70, 46, 74, true],
    [106, 34, 52, 30, 56, true],   // displacement leg that leaves the FVG
    [124, 44, 56, 40, 60, false],  // retrace back into the gap
    [142, 28, 46, 24, 50, true],
    [160, 16, 30, 12, 34, true],
    [178, 8, 20, 4, 24, true],
  ]
  return (
    <svg viewBox="0 0 220 132" className="v2-lp-visual" aria-hidden="true" focusable="false">
      {/* Prior low that gets swept */}
      <line x1="8" y1="98" x2="86" y2="98" stroke="var(--v2-text-3)" strokeWidth="1" strokeDasharray="3 3" />
      <text x="8" y="94" fontSize="7" fill="var(--v2-text-3)">prior low — swept</text>
      {/* Fair Value Gap left by the displacement leg */}
      <rect x="82" y="58" width="52" height="12" rx="2" fill="var(--v2-accent-bg)" stroke="var(--v2-accent-border)" strokeDasharray="3 2" />
      <text x="86" y="67" fontSize="7" fill="var(--v2-accent)">FVG</text>
      {/* Candles */}
      {candles.map(([x, bt, bb, wt, wb, up], i) => (
        <g key={i}>
          <line x1={x} y1={wt} x2={x} y2={wb} stroke={up ? 'var(--v2-up)' : 'var(--v2-down)'} strokeWidth="1.2" />
          <rect x={x - 4} y={bt} width="8" height={Math.max(2, bb - bt)} rx="1.5" fill={up ? 'var(--v2-up)' : 'var(--v2-down)'} opacity="0.85" />
        </g>
      ))}
      {/* Bracket rails: entry / stop / target */}
      <line x1="130" y1="58" x2="214" y2="58" stroke="var(--v2-accent)" strokeWidth="1" strokeDasharray="4 3" />
      <text x="192" y="55" fontSize="7" fill="var(--v2-accent)">entry</text>
      <line x1="130" y1="78" x2="214" y2="78" stroke="var(--v2-down)" strokeWidth="1" strokeDasharray="4 3" />
      <text x="194" y="88" fontSize="7" fill="var(--v2-down)">stop</text>
      <line x1="130" y1="10" x2="214" y2="10" stroke="var(--v2-up)" strokeWidth="1" strokeDasharray="4 3" />
      <text x="188" y="21" fontSize="7" fill="var(--v2-up)">target</text>
    </svg>
  )
}

/** Backtesting + AI: an equity-style curve above a parameter-sweep heatmap
 *  with the winning combination outlined. All unlabeled. */
function BacktestVisual() {
  const cells = [
    0.12, 0.3, 0.22, 0.5, 0.35, 0.18, 0.42, 0.65,
    0.3, 0.55, 0.92, 0.6, 0.25, 0.48, 0.38, 0.2,
  ]
  const best = 10 // index of the outlined "winner" cell
  return (
    <svg viewBox="0 0 220 132" className="v2-lp-visual" aria-hidden="true" focusable="false">
      {/* Equity-style curve (unlabeled, no axis values) */}
      <path
        d="M 10 62 L 32 56 L 54 58 L 76 46 L 98 40 L 120 44 L 142 30 L 164 24 L 186 18 L 210 12 L 210 70 L 10 70 Z"
        fill="var(--v2-accent)" opacity="0.08" stroke="none"
      />
      <path
        d="M 10 62 L 32 56 L 54 58 L 76 46 L 98 40 L 120 44 L 142 30 L 164 24 L 186 18 L 210 12"
        fill="none" stroke="var(--v2-accent)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"
      />
      <line x1="10" y1="70" x2="210" y2="70" stroke="var(--v2-border)" strokeWidth="1" />
      {/* Parameter-sweep heatmap, best combo outlined */}
      {cells.map((v, i) => {
        const x = 10 + (i % 8) * 25.5
        const y = 80 + Math.floor(i / 8) * 24
        return (
          <g key={i}>
            <rect x={x} y={y} width="21" height="20" rx="4" fill="var(--v2-accent)" opacity={0.06 + v * 0.5} />
            {i === best && (
              <rect x={x - 1.5} y={y - 1.5} width="24" height="23" rx="5" fill="none" stroke="var(--v2-up)" strokeWidth="1.5" />
            )}
          </g>
        )
      })}
    </svg>
  )
}

// ═════════════════════════════════════════════════════════════════════════
// Page
// ═════════════════════════════════════════════════════════════════════════
export default function LandingV2() {
  useScrollReveal()

  return (
    <div className="v2-root v2-page v2-lp">

      {/* ── NAV ─────────────────────────────────────────────────────────── */}
      <nav className="v2-lp-nav">
        <div className="v2-lp-nav__inner">
          <Link to="/" className="v2-lp-nav__brand">
            <ThetaLogo size={30} />
            Theta Algos
          </Link>
          <div className="v2-lp-nav__links">
            <a href="#product">Product</a>
            <a href="#strategies">Strategies</a>
            <a href="#how-it-works">How it works</a>
            <a href="#pricing">Pricing</a>
            <Link to="/help">FAQ</Link>
          </div>
          <div className="v2-lp-nav__actions">
            <Link to="/login" className="v2-btn v2-btn--quiet v2-btn--sm">Sign in</Link>
            <Link to="/register" className="v2-btn v2-btn--primary v2-btn--sm">Start free trial</Link>
          </div>
        </div>
      </nav>

      {/* ── 1. HERO ─────────────────────────────────────────────────────── */}
      <header className="v2-lp-hero">
        {/* Wall-Street building crawl: two LED ticker bands frame the hero
            (top scrolls left, bottom scrolls right), keeping the two slow
            accent glows. CSS-only; v2.css §19 freezes the crawl under
            prefers-reduced-motion. Ryan 2026-07-02: replaced the drifting
            grid with the NYSE-style tape; now LIVE quotes via /api/v1/public/
            tape with a slower, larger crawl (speed = loop seconds). */}
        <TickerTape className="v2-lp-hero__tape--top" direction="left" speed={96} />
        <div className="v2-lp-hero__glow v2-lp-hero__glow--a" aria-hidden="true" />
        <div className="v2-lp-hero__glow v2-lp-hero__glow--b" aria-hidden="true" />
        <TickerTape className="v2-lp-hero__tape--bottom" direction="right" speed={132} />

        <div className="v2-lp-hero__inner">
          <div className="v2-lp-chips">
            <span className="v2-badge v2-badge--accent">Stocks · Options · Futures</span>
            {/* USA-only availability — Stripe Identity + broker network is US-tuned */}
            <Link to="/help" className="v2-lp-uschip" title="Why USA only? See the FAQ.">
              <span aria-hidden="true">🇺🇸</span> Available in the United States only
            </Link>
          </div>

          <h1>
            Algorithmic trading signals and execution,
            <span className="v2-lp-hero__accent"> without the emotions.</span>
          </h1>

          <p className="v2-lp-hero__sub">
            Theta Algos scans <strong>{TICKERS_SCANNED} tickers every {SCAN_CADENCE_MIN} minutes</strong> from
            4 AM to 8 PM ET, ranks the movers, picks the option contract, and sends a one-click
            confirm to your inbox. ICT-based ES/NQ futures setups, prop-firm-safe signal delivery,
            and a full backtest → paper → live pipeline — one platform, zero second-guessing.
          </p>

          <div className="v2-lp-hero__cta">
            <Link to="/register" className="v2-btn v2-btn--primary v2-btn--lg">
              Start 30-day free trial <ArrowRight size={16} />
            </Link>
            <a href="#live-stats" className="v2-btn v2-btn--ghost v2-btn--lg">
              See live performance
            </a>
          </div>

          <div className="v2-lp-hero__assurances">
            {['No credit card required', '30-day free trial', 'Cancel anytime', 'Kill switch built in'].map((t) => (
              <span key={t}>
                <CheckCircle2 size={13} className="v2-up" /> {t}
              </span>
            ))}
          </div>
        </div>
      </header>

      {/* ── 2. LIVE-STATS STRIP ─────────────────────────────────────────── */}
      {/* Operational coverage only — these are scanner-config facts already
          published on the V1 landing, not marketing math. Verified
          performance metrics slot in here once the backend exposes audited
          figures (see TODO-VERIFY-CLAIM at the top of this file). */}
      <section id="live-stats" className="v2-lp-stats v2-lp-anchor" aria-label="Platform coverage">
        <div className="v2-lp-stats__grid">
          <StatCard
            label="Tickers scanned"
            value={<CountUp target={TICKERS_SCANNED} format={(v) => Math.round(v).toString()} />}
            hint="every intraday scan cycle"
          />
          <StatCard
            label="Scan cadence"
            value={<CountUp target={SCAN_CADENCE_MIN} format={(v) => `${Math.round(v)} min`} />}
            hint="4 AM – 8 PM ET, every weekday"
          />
          <StatCard
            label="Coverage window"
            value={COVERAGE_WINDOW}
            hint="ET — pre-market through after-hours"
          />
          <StatCard
            label="News blackout"
            value={NEWS_BLACKOUT}
            hint="plus PPI, NFP, GDP, Retail Sales — auto-skipped"
          />
        </div>
      </section>

      {/* ── 3. PRODUCT SHOWCASE ─────────────────────────────────────────── */}
      <section id="product" className="v2-lp-section v2-lp-anchor">
        <div className="v2-lp-head reveal">
          <div className="v2-lp-eyebrow">Product</div>
          <h2 className="v2-lp-h2">Three engines. One terminal.</h2>
          <p className="v2-lp-lead">
            Scanning, futures execution, and research share the same account, the same
            risk controls, and the same kill switch.
          </p>
        </div>

        <div className="v2-lp-showcase reveal">
          <div className="v2-card v2-lp-panel">
            <div className="v2-lp-panel__visual"><ScannerVisual /></div>
            <div className="v2-lp-panel__body">
              <h3 className="v2-lp-panel__title">Daily Stock Scanner</h3>
              <p className="v2-lp-panel__desc">
                {TICKERS_SCANNED} tickers swept every {SCAN_CADENCE_MIN} minutes intraday — plus a{' '}
                {PREMARKET_UNIVERSE} ticker pre-market pass. Gaps, 52-week breakouts and low-float
                squeezes are ranked, filtered through the news blackout, and delivered as a
                one-click confirm email at 8:30 ET.
              </p>
            </div>
          </div>

          <div className="v2-card v2-lp-panel">
            <div className="v2-lp-panel__visual"><FuturesVisual /></div>
            <div className="v2-lp-panel__body">
              <h3 className="v2-lp-panel__title">Futures Strategies</h3>
              <p className="v2-lp-panel__desc">
                ICT-based setups on ES, NQ, RTY and YM: liquidity sweeps, Fair Value Gaps,
                session filters, structure-based stops. Auto-executed through Tradovate, or
                delivered as email + push signals you place manually inside prop-firm rules.
              </p>
            </div>
          </div>

          <div className="v2-card v2-lp-panel">
            <div className="v2-lp-panel__visual"><BacktestVisual /></div>
            <div className="v2-lp-panel__body">
              <h3 className="v2-lp-panel__title">Backtesting + AI</h3>
              <p className="v2-lp-panel__desc">
                Replay years of history with realistic slippage and commissions, sweep parameter
                combinations with the optimizer, or describe a strategy in plain English and let
                the AI builder assemble the rules — then paper trade it before a dollar is at risk.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* ── 4. STRATEGY PREVIEW ─────────────────────────────────────────── */}
      <section id="strategies" className="v2-lp-section v2-lp-anchor">
        <div className="v2-lp-head reveal">
          <div className="v2-lp-eyebrow">Strategy library</div>
          <h2 className="v2-lp-h2">The setups, exactly as they run</h2>
          <p className="v2-lp-lead">
            Every strategy below is described by its rules — not by cherry-picked results.
            Backtest any of them yourself before turning one on.
          </p>
        </div>

        <div className="v2-lp-strategies reveal">
          {STRATEGIES.map(({ icon: Icon, market, name, desc }) => (
            <div key={name} className="v2-card v2-lp-strategy">
              <div className="v2-lp-strategy__head">
                <span className="v2-lp-strategy__icon"><Icon size={16} /></span>
                <span className="v2-badge v2-badge--neutral">{market}</span>
              </div>
              <h3 className="v2-lp-strategy__name">{name}</h3>
              <p className="v2-lp-strategy__desc">{desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* ── 5. HOW IT WORKS ─────────────────────────────────────────────── */}
      <section id="how-it-works" className="v2-lp-section v2-lp-anchor">
        <div className="v2-lp-head reveal">
          <div className="v2-lp-eyebrow">Process</div>
          <h2 className="v2-lp-h2">From idea to live trade in three steps</h2>
        </div>

        <div className="v2-lp-steps reveal">
          {STEPS.map(({ title, desc }, i) => (
            <div key={title} className="v2-lp-step">
              <div className="v2-lp-step__num v2-num">{String(i + 1).padStart(2, '0')}</div>
              <div>
                <h3 className="v2-lp-step__title">{title}</h3>
                <p className="v2-lp-step__desc">{desc}</p>
              </div>
            </div>
          ))}
        </div>

        <div className="v2-lp-steps__foot reveal">
          <FlaskConical size={14} className="v2-lp-steps__foot-icon" />
          Paper trading and backtesting are included from day one — the platform is built so you
          validate first and go live second.
        </div>
      </section>

      {/* ── 6. PRICING ──────────────────────────────────────────────────── */}
      <section id="pricing" className="v2-lp-section v2-lp-anchor">
        <div className="v2-lp-head reveal">
          <div className="v2-lp-eyebrow">Pricing</div>
          <h2 className="v2-lp-h2">Simple, transparent pricing</h2>
          <p className="v2-lp-lead">
            Start for free, upgrade as you scale. All plans are month-to-month —
            cancel anytime and keep access through your billing period.
          </p>
        </div>

        <div className="v2-lp-pricing reveal">
          {PRICING_TIERS.map((tier) => (
            <div
              key={tier.id}
              className={`v2-card v2-lp-plan${tier.highlight ? ' v2-lp-plan--highlight' : ''}`}
            >
              {tier.tag && <span className="v2-badge v2-badge--accent v2-lp-plan__tag">{tier.tag}</span>}
              <div className="v2-lp-plan__name">{tier.name}</div>
              <div className="v2-lp-plan__tier">{tier.tierLabel}</div>
              <div className="v2-lp-plan__pricing">
                {tier.originalPrice && <span className="v2-lp-plan__strike v2-num">{tier.originalPrice}</span>}
                <span className="v2-lp-plan__price v2-num">{tier.price}</span>
                <span className="v2-lp-plan__per">{tier.per}</span>
              </div>
              {tier.promoNote && <div className="v2-lp-plan__promo">{tier.promoNote}</div>}
              <p className="v2-lp-plan__desc">{tier.desc}</p>
              <ul className="v2-lp-plan__features">
                {tier.features.map((f) => (
                  <li key={f}>
                    <CheckCircle2 size={13} className="v2-lp-plan__check" /> {f}
                  </li>
                ))}
              </ul>
              <Link
                to="/register"
                className={`v2-btn ${tier.highlight ? 'v2-btn--primary' : 'v2-btn--ghost'}`}
              >
                {tier.id === 'free_trial' ? 'Start free trial' : 'Get started'}
              </Link>
            </div>
          ))}
        </div>

        <div className="v2-lp-pricing__more reveal">
          <Link to="/pricing">
            See the full feature comparison <ChevronRight size={14} />
          </Link>
        </div>
      </section>

      {/* ── 7. TRUST STRIP ──────────────────────────────────────────────── */}
      <section className="v2-lp-section reveal" aria-label="Security and disclosures">
        <div className="v2-lp-head">
          <div className="v2-lp-eyebrow">Trust</div>
          <h2 className="v2-lp-h2">Built like infrastructure, disclosed like it should be</h2>
        </div>

        <div className="v2-lp-trust">
          {TRUST_ITEMS.map(({ icon: Icon, title, desc }) => (
            <div key={title} className="v2-card v2-lp-trust__item">
              <span className="v2-lp-strategy__icon"><Icon size={16} /></span>
              <h3 className="v2-lp-trust__title">{title}</h3>
              <p className="v2-lp-trust__desc">{desc}</p>
            </div>
          ))}
        </div>

        <div className="v2-lp-trust__links">
          <Link to="/disclosures">Read the full risk &amp; regulatory disclosures <ChevronRight size={13} /></Link>
        </div>
      </section>

      {/* ── 8. TESTIMONIALS — flag-gated placeholders only ──────────────── */}
      {/* Rendering fabricated quotes is a compliance risk; this whole section
          stays dark until SHOW_TESTIMONIALS flips AND real, permissioned
          quotes replace the placeholder copy below. */}
      {SHOW_TESTIMONIALS && (
        <section className="v2-lp-section reveal" aria-label="Testimonials">
          <div className="v2-lp-head">
            <div className="v2-lp-eyebrow">Subscribers</div>
            <h2 className="v2-lp-h2">What traders say</h2>
          </div>
          <div className="v2-lp-trust">
            {[1, 2, 3].map((i) => (
              <div key={i} className="v2-card v2-lp-quote">
                <span className="v2-badge v2-badge--warn">Placeholder — not a real quote</span>
                <p className="v2-lp-quote__text">
                  Verified customer quote pending. Do not ship this section enabled without a
                  real testimonial and written permission on file.
                </p>
                <div className="v2-lp-quote__who">— Awaiting verified subscriber</div>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* ── 9. FINAL CTA ────────────────────────────────────────────────── */}
      <section className="v2-lp-cta reveal">
        <h2 className="v2-lp-h2">Let the rules do the trading.</h2>
        <p className="v2-lp-lead v2-lp-cta__lead">
          Start your 30-day free trial — no credit card required. Paper trading and
          backtesting included from day one.
        </p>
        <Link to="/register" className="v2-btn v2-btn--primary v2-btn--lg">
          Start free trial <ArrowRight size={16} />
        </Link>
      </section>

      {/* ── FOOTER ──────────────────────────────────────────────────────── */}
      <footer className="v2-lp-footer">
        <div className="v2-lp-footer__inner">
          <div className="v2-lp-footer__row">
            <div className="v2-lp-nav__brand">
              <ThetaLogo size={24} />
              Theta Algos
            </div>
            <div className="v2-lp-footer__links">
              <Link to="/pricing">Pricing</Link>
              <Link to="/help">FAQ</Link>
              <Link to="/terms">Terms</Link>
              <Link to="/privacy">Privacy</Link>
              <Link to="/disclosures">Disclosures</Link>
              <Link to="/cookies">Cookies</Link>
              <Link to="/login">Sign in</Link>
              <Link to="/register">Register</Link>
            </div>
            <p className="v2-lp-footer__copy">© {new Date().getFullYear()} Theta Algos. All rights reserved.</p>
          </div>
          <div className="v2-lp-disclosure">
            <strong>Risk Disclosure:</strong> {RISK_DISCLOSURE}
          </div>
        </div>
      </footer>

    </div>
  )
}
