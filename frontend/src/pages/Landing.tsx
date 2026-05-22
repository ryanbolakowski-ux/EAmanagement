import { useEffect } from 'react'
import { Link } from 'react-router-dom'
import ThetaLogo from '../components/ThetaLogo'
import {
  BarChart2, TrendingUp, Zap, ShieldCheck, FlaskConical,
  Sliders, PlayCircle, ChevronRight, CheckCircle2, ArrowRight,
  Activity, Lock, Globe2, Users
} from 'lucide-react'

// ── Mini chart SVG (decorative) ───────────────────────────────────────────────
function MiniChart() {
  const pts = [10,38,28,52,18,60,30,72,20,80,42,68,55,88,48,95]
  const path = pts.reduce((acc, v, i) => acc + (i % 2 === 0 ? (i === 0 ? `M ${v}` : ` L ${v}`) : ` ${v}`), '')
  return (
    <svg viewBox="0 0 100 100" className="w-full h-full" preserveAspectRatio="none">
      <defs>
        <linearGradient id="gfill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#2563eb" stopOpacity="0.15"/>
          <stop offset="100%" stopColor="#2563eb" stopOpacity="0"/>
        </linearGradient>
      </defs>
      <path d={path + ' L 95 100 L 10 100 Z'} fill="url(#gfill)" />
      <path d={path} fill="none" stroke="#2563eb" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  )
}

// ── App screenshot mockup ─────────────────────────────────────────────────────
function AppMockup() {
  const bars = [62, 78, 45, 88, 55, 92, 70, 83, 60, 95, 72, 86, 64, 90]
  return (
    <div className="relative w-full max-w-3xl mx-auto rounded-2xl overflow-hidden shadow-2xl border border-slate-200 dark:border-slate-700" style={{background:'#fff'}}>
      {/* Title bar */}
      <div className="bg-slate-50 border-b border-slate-200 px-5 py-3 flex items-center gap-3 dark:bg-slate-900 dark:border-slate-700">
        <div className="flex gap-1.5">
          <span className="w-3 h-3 rounded-full bg-red-400"/>
          <span className="w-3 h-3 rounded-full bg-amber-400"/>
          <span className="w-3 h-3 rounded-full bg-green-400"/>
        </div>
        <div className="flex-1 bg-white border border-slate-200 rounded-md px-3 py-1 text-xs text-slate-400 text-center dark:bg-slate-900 dark:text-slate-500 dark:border-slate-700">
          app.thetaalgos.com
        </div>
      </div>

      <div className="flex h-80">
        {/* Sidebar */}
        <div className="w-44 bg-white border-r border-slate-100 p-3 flex flex-col gap-1 dark:bg-slate-900 dark:border-slate-800">
          <div className="flex items-center gap-2 p-2 mb-2">
            <BarChart2 size={16} className="text-blue-600"/>
            <span className="text-xs font-bold text-slate-800 dark:text-slate-100">Theta Algos</span>
          </div>
          {[
            { label: 'Dashboard', active: false },
            { label: 'Strategies', active: false },
            { label: 'Backtests', active: true },
            { label: 'Optimization', active: false },
            { label: 'Paper Trading', active: false },
            { label: 'Live Trading', active: false },
          ].map(({ label, active }) => (
            <div key={label} className={`px-3 py-1.5 rounded-lg text-xs font-medium ${active ? 'bg-blue-600 text-white' : 'text-slate-500 dark:text-slate-400'}`}>
              {label}
            </div>
          ))}
        </div>

        {/* Main content */}
        <div className="flex-1 p-4 bg-slate-50 overflow-hidden dark:bg-slate-900">
          <div className="text-sm font-semibold text-slate-800 mb-3 dark:text-slate-100">ES · ICT Sweep + FVG · Jan 2022 – Dec 2023</div>

          {/* Metric strip */}
          <div className="grid grid-cols-4 gap-2 mb-3">
            {[
              { l: 'Win Rate', v: '64.2%', c: 'text-green-600' },
              { l: 'Profit Factor', v: '2.41', c: 'text-blue-600' },
              { l: 'Net Profit', v: '+$48,220', c: 'text-green-600' },
              { l: 'Max Drawdown', v: '8.3%', c: 'text-red-500' },
            ].map(({ l, v, c }) => (
              <div key={l} className="bg-white rounded-lg border border-slate-200 p-2 dark:bg-slate-900 dark:border-slate-700">
                <div className="text-[9px] text-slate-400 dark:text-slate-500">{l}</div>
                <div className={`text-sm font-bold mt-0.5 ${c}`}>{v}</div>
              </div>
            ))}
          </div>

          {/* Chart */}
          <div className="bg-white rounded-lg border border-slate-200 p-3 h-28 dark:bg-slate-900 dark:border-slate-700">
            <div className="text-[9px] text-slate-400 mb-1 dark:text-slate-500">Equity Curve</div>
            <div className="h-16 relative">
              <svg viewBox="0 0 200 50" className="w-full h-full" preserveAspectRatio="none">
                <defs>
                  <linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#2563eb" stopOpacity="0.12"/>
                    <stop offset="100%" stopColor="#2563eb" stopOpacity="0"/>
                  </linearGradient>
                </defs>
                <path d="M 0 45 L 15 42 L 30 38 L 45 35 L 55 38 L 70 28 L 85 22 L 100 18 L 115 14 L 130 16 L 145 8 L 160 5 L 175 3 L 200 2 L 200 50 L 0 50 Z" fill="url(#eq)"/>
                <path d="M 0 45 L 15 42 L 30 38 L 45 35 L 55 38 L 70 28 L 85 22 L 100 18 L 115 14 L 130 16 L 145 8 L 160 5 L 175 3 L 200 2" fill="none" stroke="#2563eb" strokeWidth="1.5"/>
              </svg>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Feature card ─────────────────────────────────────────────────────────────
function FeatureCard({ icon: Icon, title, desc, color }: { icon: any; title: string; desc: string; color: string }) {
  return (
    <div className="bg-white rounded-2xl border border-slate-200 p-6 hover:shadow-md hover:border-slate-300 transition-all duration-200 dark:bg-slate-900 dark:border-slate-700">
      <div className={`w-11 h-11 rounded-xl flex items-center justify-center mb-4 ${color}`}>
        <Icon size={20}/>
      </div>
      <h3 className="text-base font-semibold text-slate-900 mb-2 dark:text-slate-100">{title}</h3>
      <p className="text-sm text-slate-500 leading-relaxed dark:text-slate-400">{desc}</p>
    </div>
  )
}

// ── Step card ────────────────────────────────────────────────────────────────
function StepCard({ n, title, desc }: { n: string; title: string; desc: string }) {
  return (
    <div className="flex gap-5">
      <div className="flex-shrink-0 w-10 h-10 rounded-full bg-blue-600 text-white font-bold text-sm flex items-center justify-center shadow-md">
        {n}
      </div>
      <div>
        <h4 className="font-semibold text-slate-900 mb-1 dark:text-slate-100">{title}</h4>
        <p className="text-sm text-slate-500 leading-relaxed dark:text-slate-400">{desc}</p>
      </div>
    </div>
  )
}

// IntersectionObserver-driven scroll reveal: every element with `.reveal`
// inside <main> gets `.is-visible` added when it enters the viewport.
function useScrollReveal() {
  useEffect(() => {
    if (typeof window === 'undefined' || !('IntersectionObserver' in window)) return
    const els = Array.from(document.querySelectorAll<HTMLElement>('.reveal'))
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) {
          e.target.classList.add('is-visible')
          io.unobserve(e.target)
        }
      })
    }, { threshold: 0.12 })
    els.forEach((el) => io.observe(el))
    return () => io.disconnect()
  }, [])
}

// ── Main component ────────────────────────────────────────────────────────────
export default function Landing() {
  useScrollReveal()
  return (
    <div className="min-h-screen bg-slate-200 dark:bg-slate-950">

      {/* ── NAV ────────────────────────────────────────────────────────────── */}
      <nav className="sticky top-0 z-50 bg-slate-200/90 dark:bg-slate-950/90 backdrop-blur border-b border-slate-300 dark:border-slate-700">
        <div className="max-w-6xl mx-auto px-6 h-16 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <BarChart2 size={22} className="text-blue-600"/>
            <span className="font-bold text-slate-900 text-lg tracking-tight dark:text-slate-100">Theta Algos</span>
          </div>
          <div className="hidden md:flex items-center gap-7 text-sm font-medium text-slate-600 dark:text-slate-300">
            <a href="#features" className="hover:text-slate-900 dark:text-slate-100 transition-colors">Features</a>
            <a href="#how-it-works" className="hover:text-slate-900 dark:text-slate-100 transition-colors">How It Works</a>
            <Link to="/pricing" className="hover:text-slate-900 dark:text-slate-100 transition-colors">Pricing</Link>
          </div>
          <div className="flex items-center gap-3">
            <Link to="/login" className="text-sm font-medium text-slate-600 hover:text-slate-900 transition-colors px-3 py-2 dark:text-slate-300">
              Sign in
            </Link>
            <Link to="/register" className="text-sm font-semibold bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg transition-colors">
              Start Free Trial
            </Link>
          </div>
        </div>
      </nav>

      {/* ── HERO ───────────────────────────────────────────────────────────── */}
      <section className="relative overflow-hidden">
        {/* Subtly drifting background grid + gradient haze */}
        <div className="absolute inset-0 bg-[linear-gradient(to_right,#e2e8f0_1px,transparent_1px),linear-gradient(to_bottom,#e2e8f0_1px,transparent_1px)] dark:bg-[linear-gradient(to_right,#1e293b_1px,transparent_1px),linear-gradient(to_bottom,#1e293b_1px,transparent_1px)] bg-[size:40px_40px] opacity-50 anim-bg-grid"/>
        <div className="absolute inset-0 bg-gradient-to-b from-white via-white/85 to-white dark:from-slate-950 dark:via-slate-950/85 dark:to-slate-950"/>
        {/* Pulsing blue ambient orbs */}
        <div className="absolute -top-24 -right-24 w-96 h-96 rounded-full bg-blue-300/30 dark:bg-blue-500/20 blur-3xl anim-blue-pulse" style={{ animationDelay: '0s' }}/>
        <div className="absolute top-40 -left-20 w-80 h-80 rounded-full bg-indigo-200/30 dark:bg-indigo-500/15 blur-3xl anim-blue-pulse" style={{ animationDelay: '1.5s' }}/>

        <div className="relative max-w-6xl mx-auto px-6 pt-20 pb-16">
          {/* Badge */}
          <div className="inline-flex items-center gap-2 bg-blue-50 border border-blue-200 text-blue-700 text-xs font-semibold px-3 py-1.5 rounded-full mb-6 anim-fade-up" style={{ animationDelay: '0.05s' }}>
            <span className="w-1.5 h-1.5 rounded-full bg-blue-500 animate-pulse"/>
            Futures · Options scanner · Oracle 5-min trigger · Pre-market alerts
          </div>

          <div className="max-w-3xl">
            {/* Prominent brand mark above the headline */}
            <div className="flex items-center gap-4 mb-7 anim-fade-up" style={{ animationDelay: '0.05s' }}>
              <ThetaLogo size={72} />
              <div>
                <div className="text-[11px] font-bold uppercase tracking-[0.2em] text-violet-600 dark:text-violet-400">Trading Algorithm</div>
                <div className="text-3xl font-extrabold text-slate-900 dark:text-slate-100 tracking-tight leading-none mt-1">Theta Algos</div>
              </div>
            </div>
            <h1 className="text-5xl md:text-6xl font-extrabold text-slate-900 leading-tight tracking-tight mb-6 dark:text-slate-100">
              <span className="inline-block anim-fade-up" style={{ animationDelay: '0.15s' }}>Quantitative precision meets</span>{' '}
              <span className="text-violet-600 inline-block anim-fade-up" style={{ animationDelay: '0.5s' }}>algorithmic edge.</span>
            </h1>
            <p className="text-xl text-slate-500 leading-relaxed mb-8 max-w-2xl anim-fade-up dark:text-slate-400" style={{ animationDelay: '0.85s' }}>
              The platform scans <strong>336 tickers</strong> every 5 minutes between 4:00 AM and 8:00 PM ET, finds the biggest movers, picks the best option contract, and pushes a one-click confirm to your inbox. Pre-market gap scanner, 52-week-high breakouts, low-float squeezes, Oracle 5-minute opening-candle engine, ES/NQ futures, prop-firm signals, full backtest + paper + live — one unified platform.
            </p>

            <div className="flex flex-col sm:flex-row gap-3 mb-10 anim-fade-up" style={{ animationDelay: '1.05s' }}>
              <Link to="/register"
                className="inline-flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-700 text-white font-semibold px-6 py-3.5 rounded-xl text-base transition-colors shadow-lg shadow-blue-200 dark:shadow-blue-900/40">
                Start 30-Day Free Trial
                <ArrowRight size={18}/>
              </Link>
              <a href="#how-it-works"
                className="inline-flex items-center justify-center gap-2 bg-white hover:bg-slate-50 text-slate-700 font-semibold px-6 py-3.5 rounded-xl text-base transition-colors border border-slate-200 dark:bg-slate-900 dark:text-slate-200 dark:border-slate-700">
                See How It Works
              </a>
            </div>

            <div className="flex flex-wrap items-center gap-5 text-sm text-slate-500 anim-fade-up dark:text-slate-400" style={{ animationDelay: '1.25s' }}>
              {['No credit card required', '30-day free trial', 'Cancel anytime'].map((t) => (
                <span key={t} className="flex items-center gap-1.5">
                  <CheckCircle2 size={14} className="text-green-500"/>
                  {t}
                </span>
              ))}
            </div>
          </div>
        </div>

        {/* App mockup */}
        <div className="relative max-w-5xl mx-auto px-6 pb-20 anim-scale-in" style={{ animationDelay: '1.4s' }}>
          <AppMockup/>
          {/* Floating stat cards */}
          <div className="absolute -top-2 -right-4 md:right-0 bg-white rounded-xl border border-slate-200 shadow-lg px-4 py-3 text-sm hidden md:block anim-fade-up dark:bg-slate-900 dark:border-slate-700" style={{ animationDelay: '2.0s' }}>
            <div className="text-slate-400 text-xs mb-0.5 dark:text-slate-500">Last backtest</div>
            <div className="font-bold text-green-600">+$48,220 net profit</div>
          </div>
          <div className="absolute bottom-8 -left-2 md:left-0 bg-white rounded-xl border border-slate-200 shadow-lg px-4 py-3 text-sm hidden md:block anim-fade-up dark:bg-slate-900 dark:border-slate-700" style={{ animationDelay: '2.2s' }}>
            <div className="text-slate-400 text-xs mb-0.5 dark:text-slate-500">Win rate</div>
            <div className="font-bold text-blue-600">64.2% · 312 trades</div>
          </div>
        </div>
      </section>

      {/* ── STATS BAR ──────────────────────────────────────────────────────── */}
      {/* ── EMOTIONLESS-TRADING CENTERPIECE ────────────────────────────── */}
      <section className="relative overflow-hidden bg-gradient-to-br from-violet-50 via-white to-blue-50 dark:from-violet-950/40 dark:via-slate-950 dark:to-blue-950/40 border-y border-violet-200 dark:border-violet-900/60 reveal">
        <div className="absolute -top-32 -left-32 w-96 h-96 rounded-full bg-violet-400/15 blur-3xl"/>
        <div className="absolute -bottom-32 -right-32 w-96 h-96 rounded-full bg-blue-400/15 blur-3xl"/>
        <div className="relative max-w-5xl mx-auto px-6 py-24 text-center">
          <div className="inline-flex items-center gap-2 bg-white dark:bg-slate-900 border border-violet-200 dark:border-violet-800 text-violet-700 dark:text-violet-300 text-xs font-bold uppercase tracking-widest px-3 py-1.5 rounded-full mb-6">
            <span className="w-1.5 h-1.5 rounded-full bg-violet-500 animate-pulse"/>
            The trader's actual enemy
          </div>
          <h2 className="text-4xl md:text-6xl font-extrabold text-slate-900 dark:text-slate-100 leading-[1.05] tracking-tight mb-6">
            Most traders lose to one thing:
            <br/>
            <span className="text-violet-600 dark:text-violet-400">their own emotions.</span>
          </h2>
          <p className="text-lg md:text-xl text-slate-600 dark:text-slate-300 leading-relaxed max-w-3xl mx-auto mb-10">
            FOMO buying the top. Revenge trading after a loss. Hesitating on a perfect setup because the last trade went red. Holding losers, cutting winners. Closing positions at -1% on a 10-point edge because <em>"it just feels off."</em>
          </p>
          <p className="text-xl md:text-2xl font-extrabold text-slate-900 dark:text-slate-100 max-w-3xl mx-auto mb-10 leading-tight">
            Theta Algos has zero emotions. <span className="text-violet-600 dark:text-violet-400">It just runs the math.</span>
          </p>

          <div className="grid sm:grid-cols-3 gap-4 max-w-4xl mx-auto text-left">
            <div className="bg-white dark:bg-slate-900 rounded-2xl border border-slate-200 dark:border-slate-700 p-5">
              <div className="text-3xl mb-2">⏱️</div>
              <div className="font-extrabold text-slate-900 dark:text-slate-100 mb-1">No hesitation</div>
              <p className="text-sm text-slate-500 dark:text-slate-400 leading-relaxed">When the setup matches the rules, the bot enters. No second-guessing, no "let me wait one more bar."</p>
            </div>
            <div className="bg-white dark:bg-slate-900 rounded-2xl border border-slate-200 dark:border-slate-700 p-5">
              <div className="text-3xl mb-2">🛑</div>
              <div className="font-extrabold text-slate-900 dark:text-slate-100 mb-1">No revenge trades</div>
              <p className="text-sm text-slate-500 dark:text-slate-400 leading-relaxed">Hard daily-loss cap + kill switch. Once it trips, the bot stops trading for the day. No "I'll make it back."</p>
            </div>
            <div className="bg-white dark:bg-slate-900 rounded-2xl border border-slate-200 dark:border-slate-700 p-5">
              <div className="text-3xl mb-2">🎯</div>
              <div className="font-extrabold text-slate-900 dark:text-slate-100 mb-1">No drift from plan</div>
              <p className="text-sm text-slate-500 dark:text-slate-400 leading-relaxed">Stop and target are bracket orders the second the entry fills. The bot doesn't "give it room to work."</p>
            </div>
          </div>
        </div>
      </section>

      {/* ── STATS BAR ──────────────────────────────────────────────────────── */}
      <section className="border-y border-slate-200 bg-slate-50 dark:bg-slate-900 dark:border-slate-800">
        <div className="max-w-5xl mx-auto px-6 py-10 grid grid-cols-2 md:grid-cols-4 gap-8">
          {[
            { value: '336', label: 'Tickers Scanned', icon: Globe2 },
            { value: '4 AM-8 PM', label: 'Coverage Window', icon: Activity },
            { value: '5 min', label: 'Scan Cadence', icon: Zap },
            { value: 'FOMC + CPI', label: 'Auto-Skipped', icon: ShieldCheck },
          ].map(({ value, label, icon: Icon }) => (
            <div key={label} className="text-center">
              <Icon size={18} className="mx-auto text-blue-500 mb-2"/>
              <div className="text-2xl font-extrabold text-slate-900 dark:text-slate-100">{value}</div>
              <div className="text-sm text-slate-500 mt-0.5 dark:text-slate-400">{label}</div>
            </div>
          ))}
        </div>
      </section>

      {/* ── FEATURES ───────────────────────────────────────────────────────── */}
      <section id="features" className="max-w-6xl mx-auto px-6 py-24">
        <div className="text-center mb-14 reveal">
          <div className="text-sm font-semibold text-blue-600 uppercase tracking-widest mb-3">Platform Features</div>
          <h2 className="text-4xl font-extrabold text-slate-900 mb-4 dark:text-slate-100">Every edge a discretionary trader cannot replicate</h2>
          <p className="text-slate-500 text-lg max-w-xl mx-auto dark:text-slate-400">Pre-market scanner, intraday momentum hunter, options strike picker, futures execution, prop-firm signals — all running 16 hours a day without a single emotional decision.</p>
        </div>

        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-5">
          <FeatureCard
            icon={Zap}
            title="Pre-Market Gap Scanner"
            desc="Every weekday at 04:00 ET the bot starts scanning 336 tickers for stocks gapping up 5%+ on 100K+ pre-market volume. Top 15 ranked by volume land in your inbox by 08:30 ET — an hour before the market opens."
            color="bg-blue-50 text-blue-600"
          />
          <FeatureCard
            icon={TrendingUp}
            title="Oracle 5-Min Opening Candle"
            desc="At 09:30 ET the bot tracks the first 5-minute candle on every gapper. At 09:35 ET it emits the setup: VWAP-based bias (Green/Red), entry at opening-candle high (longs) or low (shorts), stop at the other end, target at 2× risk. Plus Fibonacci + half-dollar/whole-dollar Oracle Levels."
            color="bg-violet-50 text-violet-600"
          />
          <FeatureCard
            icon={Activity}
            title="Low-Float Squeeze Hunter"
            desc="Filters for stocks under $20 with float under 10M shares, 5%+ pre-market or 10%+ intraday move, AND a positive text catalyst on the wire (FDA approval, earnings beat, contract awarded). Fires only when every box checks."
            color="bg-amber-50 text-amber-600"
          />
          <FeatureCard
            icon={BarChart2}
            title="52-Week Breakout + RSI"
            desc="Catches stocks within 2% of their 52-week high with 300%+ intraday volume vs the 20-day average, RSI confirming the move. Entry triggers when a 1-minute candle closes above the resistance line."
            color="bg-green-50 text-green-600"
          />
          <FeatureCard
            icon={Sliders}
            title="Options Strike Picker"
            desc="When a directional signal fires, the bot auto-picks the right option contract: 30-60 DTE, delta 0.30-0.50, factoring IV and stop-distance. Trend Pullback, Breakout, Vertical Spread, Earnings Catalyst, and Wheel modes — all pre-built."
            color="bg-rose-50 text-rose-600"
          />
          <FeatureCard
            icon={ShieldCheck}
            title="News Blackout + Kill Switch"
            desc="Auto-pauses ±30 min around FOMC, CPI, PPI, NFP, GDP, Retail Sales — pulled from a live calendar that refreshes every 6 hours. Daily-loss cap kills the day automatically when hit. No emotional override."
            color="bg-sky-50 text-sky-600"
          />
          <FeatureCard
            icon={PlayCircle}
            title="Paper Trading + Backtest"
            desc="Every strategy can run paper-first against live market data with simulated fills. Backtest engines replay 2+ years of history with realistic slippage and commissions. Build a track record before going live."
            color="bg-cyan-50 text-cyan-600"
          />
          <FeatureCard
            icon={Lock}
            title="Confirm-Then-Execute Flow"
            desc="Pre-market signals arrive in your inbox at 08:30 ET with one-click Confirm and Skip buttons. Auto-executes at 08:45 if you don't click (configurable). Intraday signals fire automatically and email a receipt."
            color="bg-fuchsia-50 text-fuchsia-600"
          />
          <FeatureCard
            icon={Users}
            title="Prop-Firm Mode (Apex/TPT/Topstep)"
            desc="Most prop firms ban automated trading. The bot routes signals to email + push notifications instead, so you can place the trades manually inside their rules. Full ICT-based futures setups on ES/NQ/RTY/YM."
            color="bg-orange-50 text-orange-600"
          />
        </div>
      </section>

      {/* ── HOW IT WORKS ───────────────────────────────────────────────────── */}
      <section id="how-it-works" className="bg-slate-50 border-y border-slate-200 reveal dark:bg-slate-900 dark:border-slate-700">
        <div className="max-w-5xl mx-auto px-6 py-24">
          <div className="text-center mb-14">
            <div className="text-sm font-semibold text-blue-600 uppercase tracking-widest mb-3">Process</div>
            <h2 className="text-4xl font-extrabold text-slate-900 mb-4 dark:text-slate-100">From idea to live trade in 4 steps</h2>
          </div>

          <div className="grid md:grid-cols-2 gap-x-16 gap-y-10">
            <StepCard
              n="01"
              title="Build your strategy"
              desc="Use the strategy builder to define your entry rules — liquidity sweeps, FVGs, inverse FVGs, session filters, and risk/reward ratios. No coding required."
            />
            <StepCard
              n="02"
              title="Backtest & optimize"
              desc="Run your strategy across years of historical ES and NQ data. Then use the optimizer to automatically find the best-performing parameter combinations."
            />
            <StepCard
              n="03"
              title="Validate with paper trading"
              desc="Connect to live market data and simulate trades in real-time with no real money at risk. Build confidence in your edge before going live."
            />
            <StepCard
              n="04"
              title="Deploy to a brokerage"
              desc="Link a Webull or Tradovate account, set your risk controls, and let the engine execute automatically. Prop-firm accounts get manual signal alerts instead. Kill switch any time."
            />
          </div>
        </div>
      </section>

      {/* ── ICT STRATEGY HIGHLIGHT ─────────────────────────────────────────── */}
      <section className="max-w-6xl mx-auto px-6 py-24 reveal">
        <div className="grid md:grid-cols-2 gap-14 items-center">
          <div>
            <div className="text-sm font-semibold text-blue-600 uppercase tracking-widest mb-3">Strategy Engine</div>
            <h2 className="text-4xl font-extrabold text-slate-900 mb-5 leading-tight dark:text-slate-100">
              Built for ICT & Smart Money Concepts
            </h2>
            <p className="text-slate-500 text-base leading-relaxed mb-8 dark:text-slate-400">
              The strategy engine natively supports the concepts serious futures traders rely on. Every condition can be chained across timeframes with full user control.
            </p>
            <div className="space-y-3">
              {[
                'Liquidity sweep detection (previous H/L)',
                'Fair Value Gap (FVG) identification & tracking',
                'Inverse FVG (IFVG) confirmation on lower TF',
                'Multi-timeframe rule chaining',
                'NY / London / Asia session filters',
                'Structure-based or tick-based stop loss',
              ].map((item) => (
                <div key={item} className="flex items-center gap-3 text-sm text-slate-700 dark:text-slate-200">
                  <CheckCircle2 size={16} className="text-blue-500 flex-shrink-0"/>
                  {item}
                </div>
              ))}
            </div>
          </div>

          {/* Metrics cards column */}
          <div className="space-y-4">
            <div className="bg-white rounded-2xl border border-slate-200 p-5 shadow-sm dark:bg-slate-900 dark:border-slate-700">
              <div className="text-xs text-slate-400 mb-3 font-medium uppercase tracking-wider dark:text-slate-500">Sample Backtest — ES 15m · 2 Years</div>
              <div className="grid grid-cols-3 gap-4">
                {[
                  { l: 'Win Rate', v: '64.2%', c: '#16a34a' },
                  { l: 'Profit Factor', v: '2.41', c: '#2563eb' },
                  { l: 'Net Profit', v: '+$48K', c: '#16a34a' },
                  { l: 'Max Drawdown', v: '8.3%', c: '#dc2626' },
                  { l: 'Total Trades', v: '312', c: '#475569' },
                  { l: 'Avg R:R', v: '2.2', c: '#2563eb' },
                ].map(({ l, v, c }) => (
                  <div key={l} className="bg-slate-50 rounded-xl p-3 dark:bg-slate-900">
                    <div className="text-xs text-slate-400 mb-1 dark:text-slate-500">{l}</div>
                    <div className="text-lg font-bold" style={{ color: c }}>{v}</div>
                  </div>
                ))}
              </div>
              <div className="mt-4 h-20 rounded-lg overflow-hidden bg-slate-50 p-2 dark:bg-slate-900">
                <MiniChart/>
              </div>
            </div>

            <div className="bg-blue-600 rounded-2xl p-5 text-white">
              <div className="text-sm font-semibold mb-1">Optimization result</div>
              <div className="text-xs text-blue-200 mb-3">Best config from 48 parameter combinations</div>
              <div className="grid grid-cols-3 gap-2 text-center">
                {[['RR', '2.5:1'], ['SL', '10 ticks'], ['FVG Min', '4 ticks']].map(([l, v]) => (
                  <div key={l} className="bg-blue-500/40 rounded-lg p-2">
                    <div className="text-xs text-blue-200">{l}</div>
                    <div className="font-bold mt-0.5 text-sm">{v}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* ── SECURITY ───────────────────────────────────────────────────────── */}
      <section className="bg-slate-900 text-white reveal">
        <div className="max-w-5xl mx-auto px-6 py-20 text-center">
          <Lock size={28} className="mx-auto text-blue-400 mb-4"/>
          <h2 className="text-3xl font-bold mb-4">Security built in from day one</h2>
          <p className="text-slate-400 text-base mb-10 max-w-xl mx-auto dark:text-slate-500">
            Broker credentials are encrypted at rest with Fernet symmetric encryption. JWT-authenticated sessions. No credential ever leaves your account unencrypted.
          </p>
          <div className="grid md:grid-cols-3 gap-6 text-left">
            {[
              { title: 'Encrypted credentials', desc: 'Broker API keys stored with AES-128 Fernet encryption — never stored in plaintext.' },
              { title: 'JWT auth sessions', desc: 'Short-lived access tokens with refresh rotation. Auto-logout on expiry.' },
              { title: 'Kill switch', desc: 'One-click or automatic trading halt. Cancels open orders immediately.' },
            ].map(({ title, desc }) => (
              <div key={title} className="bg-slate-800 rounded-xl p-5">
                <div className="text-sm font-semibold text-white mb-2">{title}</div>
                <div className="text-sm text-slate-400 leading-relaxed dark:text-slate-500">{desc}</div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ── PRICING PREVIEW ────────────────────────────────────────────────── */}
      <section className="max-w-5xl mx-auto px-6 py-24 text-center reveal">
        <div className="text-sm font-semibold text-blue-600 uppercase tracking-widest mb-3">Pricing</div>
        <h2 className="text-4xl font-extrabold text-slate-900 mb-4 dark:text-slate-100">Plans for every stage</h2>
        <p className="text-slate-500 text-lg mb-10 max-w-xl mx-auto dark:text-slate-400">Start with backtesting and upgrade as you scale. All plans include a 30-day free trial.</p>

        <div className="grid md:grid-cols-3 gap-6 mb-8">
          {[
            { name: 'Futures Signals', tag: 'Tier 2 · $49/mo', desc: 'For prop-firm traders. ICT signals on ES/NQ/RTY/YM via email + push — you place trades manually inside Apex/TPT/Topstep rules.', features: ['ICT futures signal scanner', 'Paper trading + backtest', '5 prop accounts max', 'Email support'], cta: 'Get started', highlight: false },
            { name: 'Options Live', tag: 'Tier 4 · $199/mo', desc: '3,000+ ticker pre-market scanner with one-click execution through your Tradier broker. Live greeks, real bid/ask.', features: ['Everything in Tier 2 & 3', 'Tradier broker integration', 'Manual confirm → real fills', 'Priority support'], cta: 'Get started', highlight: true },
            { name: 'Fully Automated', tag: 'Tier 5 · $399/mo', desc: 'Zero clicks. The bot scans, picks, sizes, places, manages, and exits — automatically. Multi-strategy including the Wheel.', features: ['Everything in Tier 4', 'Auto-execute (no confirm)', 'Multi-strategy concurrent', 'Priority + chat support'], cta: 'Get started', highlight: false },
          ].map(({ name, tag, desc, features, cta, highlight }) => (
            <div key={name} className={`rounded-2xl border p-6 text-left relative ${highlight ? 'border-blue-500 bg-blue-600 text-white shadow-xl shadow-blue-200' : 'border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900'}`}>
              {highlight && (
                <div className="absolute -top-3 left-1/2 -translate-x-1/2 bg-amber-400 text-amber-900 text-xs font-bold px-3 py-1 rounded-full">
                  Most Popular
                </div>
              )}
              <div className={`text-xs font-semibold mb-1 ${highlight ? 'text-blue-200' : 'text-blue-600'}`}>{tag}</div>
              <div className={`text-xl font-bold mb-2 ${highlight ? 'text-white' : 'text-slate-900 dark:text-slate-100'}`}>{name}</div>
              <div className={`text-sm mb-5 leading-relaxed ${highlight ? 'text-blue-100' : 'text-slate-500 dark:text-slate-400'}`}>{desc}</div>
              <div className="space-y-2 mb-6">
                {features.map((f) => (
                  <div key={f} className={`flex items-center gap-2 text-sm ${highlight ? 'text-blue-50' : 'text-slate-600 dark:text-slate-300'}`}>
                    <CheckCircle2 size={14} className={highlight ? 'text-blue-200' : 'text-blue-500'}/>
                    {f}
                  </div>
                ))}
              </div>
              <Link to="/register" className={`block text-center text-sm font-semibold py-2.5 rounded-xl transition-colors ${highlight ? 'bg-white dark:bg-slate-900 text-blue-700 hover:bg-blue-50' : 'bg-blue-600 text-white hover:bg-blue-700'}`}>
                {cta}
              </Link>
            </div>
          ))}
        </div>

        <Link to="/pricing" className="inline-flex items-center gap-1.5 text-blue-600 hover:text-blue-700 font-semibold text-sm transition-colors">
          See full pricing comparison <ChevronRight size={16}/>
        </Link>
      </section>

      {/* ── CTA BANNER ─────────────────────────────────────────────────────── */}
      <section className="bg-blue-600 text-white reveal">
        <div className="max-w-4xl mx-auto px-6 py-20 text-center">
          <h2 className="text-4xl font-extrabold mb-4">Ready to trade with an edge?</h2>
          <p className="text-blue-100 text-lg mb-8 max-w-xl mx-auto">
            Start your 30-day free trial. No credit card required. Paper trading and limited backtesting included.
          </p>
          <Link to="/register"
            className="inline-flex items-center gap-2 bg-white text-blue-700 hover:bg-blue-50 font-bold px-8 py-4 rounded-xl text-base transition-colors shadow-lg dark:bg-slate-900">
            Start Free Trial
            <ArrowRight size={18}/>
          </Link>
        </div>
      </section>

      {/* ── FOOTER ─────────────────────────────────────────────────────────── */}
      <footer className="border-t border-slate-200 bg-white dark:bg-slate-900 dark:border-slate-700">
        <div className="max-w-6xl mx-auto px-6 py-10">
          <div className="flex flex-col md:flex-row items-center justify-between gap-4">
            <div className="flex items-center gap-2">
              <BarChart2 size={18} className="text-blue-600"/>
              <span className="font-bold text-slate-800 dark:text-slate-100">Theta Algos</span>
            </div>
            <div className="flex items-center gap-6 text-sm text-slate-500 dark:text-slate-400">
              <Link to="/pricing" className="hover:text-slate-800">Pricing</Link>
              <Link to="/login" className="hover:text-slate-800">Sign In</Link>
              <Link to="/register" className="hover:text-slate-800">Register</Link>
            </div>
            <p className="text-sm text-slate-400 dark:text-slate-500">© {new Date().getFullYear()} Theta Algos. All rights reserved.</p>
          </div>
          <div className="mt-6 p-4 bg-amber-50 border border-amber-200 rounded-xl text-xs text-amber-700">
            <strong>Risk Disclosure:</strong> Futures and options trading involve substantial risk of loss and are not appropriate for every investor. Options can expire worthless — you can lose 100% of the premium paid on a trade. Forex (coming soon) carries similar risk. Past performance, backtest results, and paper-trading metrics do not guarantee future returns. Theta Algos LLC is a software platform — not a registered investment adviser, broker-dealer, or futures commission merchant. Nothing on this site is investment advice. Prop-firm rules change frequently and most prohibit automated trading; account closures from rule violations are the user's sole responsibility. Always trade with capital you can afford to lose.
          </div>
        </div>
      </footer>

    </div>
  )
}
