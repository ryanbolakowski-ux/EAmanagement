import { Link } from 'react-router-dom'
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
    <div className="relative w-full max-w-3xl mx-auto rounded-2xl overflow-hidden shadow-2xl border border-slate-200" style={{background:'#fff'}}>
      {/* Title bar */}
      <div className="bg-slate-50 border-b border-slate-200 px-5 py-3 flex items-center gap-3">
        <div className="flex gap-1.5">
          <span className="w-3 h-3 rounded-full bg-red-400"/>
          <span className="w-3 h-3 rounded-full bg-amber-400"/>
          <span className="w-3 h-3 rounded-full bg-green-400"/>
        </div>
        <div className="flex-1 bg-white border border-slate-200 rounded-md px-3 py-1 text-xs text-slate-400 text-center">
          app.edgeassetmanagement.com
        </div>
      </div>

      <div className="flex h-80">
        {/* Sidebar */}
        <div className="w-44 bg-white border-r border-slate-100 p-3 flex flex-col gap-1">
          <div className="flex items-center gap-2 p-2 mb-2">
            <BarChart2 size={16} className="text-blue-600"/>
            <span className="text-xs font-bold text-slate-800">Edge AM</span>
          </div>
          {[
            { label: 'Dashboard', active: false },
            { label: 'Strategies', active: false },
            { label: 'Backtests', active: true },
            { label: 'Optimization', active: false },
            { label: 'Paper Trading', active: false },
            { label: 'Live Trading', active: false },
          ].map(({ label, active }) => (
            <div key={label} className={`px-3 py-1.5 rounded-lg text-xs font-medium ${active ? 'bg-blue-600 text-white' : 'text-slate-500'}`}>
              {label}
            </div>
          ))}
        </div>

        {/* Main content */}
        <div className="flex-1 p-4 bg-slate-50 overflow-hidden">
          <div className="text-sm font-semibold text-slate-800 mb-3">ES · ICT Sweep + FVG · Jan 2022 – Dec 2023</div>

          {/* Metric strip */}
          <div className="grid grid-cols-4 gap-2 mb-3">
            {[
              { l: 'Win Rate', v: '64.2%', c: 'text-green-600' },
              { l: 'Profit Factor', v: '2.41', c: 'text-blue-600' },
              { l: 'Net Profit', v: '+$48,220', c: 'text-green-600' },
              { l: 'Max Drawdown', v: '8.3%', c: 'text-red-500' },
            ].map(({ l, v, c }) => (
              <div key={l} className="bg-white rounded-lg border border-slate-200 p-2">
                <div className="text-[9px] text-slate-400">{l}</div>
                <div className={`text-sm font-bold mt-0.5 ${c}`}>{v}</div>
              </div>
            ))}
          </div>

          {/* Chart */}
          <div className="bg-white rounded-lg border border-slate-200 p-3 h-28">
            <div className="text-[9px] text-slate-400 mb-1">Equity Curve</div>
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
    <div className="bg-white rounded-2xl border border-slate-200 p-6 hover:shadow-md hover:border-slate-300 transition-all duration-200">
      <div className={`w-11 h-11 rounded-xl flex items-center justify-center mb-4 ${color}`}>
        <Icon size={20}/>
      </div>
      <h3 className="text-base font-semibold text-slate-900 mb-2">{title}</h3>
      <p className="text-sm text-slate-500 leading-relaxed">{desc}</p>
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
        <h4 className="font-semibold text-slate-900 mb-1">{title}</h4>
        <p className="text-sm text-slate-500 leading-relaxed">{desc}</p>
      </div>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────
export default function Landing() {
  return (
    <div className="min-h-screen bg-white">

      {/* ── NAV ────────────────────────────────────────────────────────────── */}
      <nav className="sticky top-0 z-50 bg-white/90 backdrop-blur border-b border-slate-200">
        <div className="max-w-6xl mx-auto px-6 h-16 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <BarChart2 size={22} className="text-blue-600"/>
            <span className="font-bold text-slate-900 text-lg tracking-tight">Edge Asset Management</span>
          </div>
          <div className="hidden md:flex items-center gap-7 text-sm font-medium text-slate-600">
            <a href="#features" className="hover:text-slate-900 transition-colors">Features</a>
            <a href="#how-it-works" className="hover:text-slate-900 transition-colors">How It Works</a>
            <Link to="/pricing" className="hover:text-slate-900 transition-colors">Pricing</Link>
          </div>
          <div className="flex items-center gap-3">
            <Link to="/login" className="text-sm font-medium text-slate-600 hover:text-slate-900 transition-colors px-3 py-2">
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
        {/* Background grid */}
        <div className="absolute inset-0 bg-[linear-gradient(to_right,#f1f5f9_1px,transparent_1px),linear-gradient(to_bottom,#f1f5f9_1px,transparent_1px)] bg-[size:40px_40px] opacity-60"/>
        <div className="absolute inset-0 bg-gradient-to-b from-white via-white/80 to-white"/>

        <div className="relative max-w-6xl mx-auto px-6 pt-20 pb-16">
          {/* Badge */}
          <div className="inline-flex items-center gap-2 bg-blue-50 border border-blue-200 text-blue-700 text-xs font-semibold px-3 py-1.5 rounded-full mb-6">
            <span className="w-1.5 h-1.5 rounded-full bg-blue-500 animate-pulse"/>
            Now supporting ES & NQ Futures
          </div>

          <div className="max-w-3xl">
            <h1 className="text-5xl md:text-6xl font-extrabold text-slate-900 leading-tight tracking-tight mb-6">
              Algorithmic futures trading,{' '}
              <span className="text-blue-600">built for serious traders.</span>
            </h1>
            <p className="text-xl text-slate-500 leading-relaxed mb-8 max-w-2xl">
              Build rule-based strategies, backtest across every timeframe, auto-optimize parameters, and deploy to live Tradovate accounts — all from one platform.
            </p>

            <div className="flex flex-col sm:flex-row gap-3 mb-10">
              <Link to="/register"
                className="inline-flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-700 text-white font-semibold px-6 py-3.5 rounded-xl text-base transition-colors shadow-lg shadow-blue-200">
                Start 30-Day Free Trial
                <ArrowRight size={18}/>
              </Link>
              <a href="#how-it-works"
                className="inline-flex items-center justify-center gap-2 bg-white hover:bg-slate-50 text-slate-700 font-semibold px-6 py-3.5 rounded-xl text-base transition-colors border border-slate-200">
                See How It Works
              </a>
            </div>

            <div className="flex flex-wrap items-center gap-5 text-sm text-slate-500">
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
        <div className="relative max-w-5xl mx-auto px-6 pb-20">
          <AppMockup/>
          {/* Floating stat cards */}
          <div className="absolute -top-2 -right-4 md:right-0 bg-white rounded-xl border border-slate-200 shadow-lg px-4 py-3 text-sm hidden md:block">
            <div className="text-slate-400 text-xs mb-0.5">Last backtest</div>
            <div className="font-bold text-green-600">+$48,220 net profit</div>
          </div>
          <div className="absolute bottom-8 -left-2 md:left-0 bg-white rounded-xl border border-slate-200 shadow-lg px-4 py-3 text-sm hidden md:block">
            <div className="text-slate-400 text-xs mb-0.5">Win rate</div>
            <div className="font-bold text-blue-600">64.2% · 312 trades</div>
          </div>
        </div>
      </section>

      {/* ── STATS BAR ──────────────────────────────────────────────────────── */}
      <section className="border-y border-slate-200 bg-slate-50">
        <div className="max-w-5xl mx-auto px-6 py-10 grid grid-cols-2 md:grid-cols-4 gap-8">
          {[
            { value: 'All TFs', label: 'Tick to Daily', icon: Activity },
            { value: '2+ Years', label: 'Historical Data', icon: BarChart2 },
            { value: 'Tradovate', label: 'Live Broker API', icon: Zap },
            { value: '100%', label: 'Modular & Scalable', icon: Globe2 },
          ].map(({ value, label, icon: Icon }) => (
            <div key={label} className="text-center">
              <Icon size={18} className="mx-auto text-blue-500 mb-2"/>
              <div className="text-2xl font-extrabold text-slate-900">{value}</div>
              <div className="text-sm text-slate-500 mt-0.5">{label}</div>
            </div>
          ))}
        </div>
      </section>

      {/* ── FEATURES ───────────────────────────────────────────────────────── */}
      <section id="features" className="max-w-6xl mx-auto px-6 py-24">
        <div className="text-center mb-14">
          <div className="text-sm font-semibold text-blue-600 uppercase tracking-widest mb-3">Platform Features</div>
          <h2 className="text-4xl font-extrabold text-slate-900 mb-4">Everything you need to trade algorithmically</h2>
          <p className="text-slate-500 text-lg max-w-xl mx-auto">From strategy conception to live execution — one unified platform built for futures traders.</p>
        </div>

        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-5">
          <FeatureCard
            icon={TrendingUp}
            title="Visual Strategy Builder"
            desc="Define multi-timeframe rule chains — liquidity sweeps, FVGs, IFVGs, session filters — with a structured no-code interface. No Python required."
            color="bg-blue-50 text-blue-600"
          />
          <FeatureCard
            icon={FlaskConical}
            title="High-Fidelity Backtesting"
            desc="Replay strategies bar-by-bar over 2+ years of data with realistic slippage, commission, and multi-timeframe execution logic across all timeframes."
            color="bg-violet-50 text-violet-600"
          />
          <FeatureCard
            icon={Sliders}
            title="Automatic Optimization"
            desc="Grid-search thousands of parameter combinations — RR ratios, stop sizes, FVG thresholds, timeframes — and get a ranked leaderboard of results."
            color="bg-amber-50 text-amber-600"
          />
          <FeatureCard
            icon={PlayCircle}
            title="Paper Trading Simulation"
            desc="Run your strategy against live market data with simulated fills. Build a track record before committing real capital. Included in the free trial."
            color="bg-green-50 text-green-600"
          />
          <FeatureCard
            icon={Zap}
            title="Live Execution via Tradovate"
            desc="Connect real Tradovate accounts, place bracket orders with automatic SL/TP, and manage up to 20 accounts simultaneously from a single dashboard."
            color="bg-rose-50 text-rose-600"
          />
          <FeatureCard
            icon={ShieldCheck}
            title="Built-In Risk Controls"
            desc="Daily loss limits, max trades per day, max contracts, and a one-click kill switch — both manual and automatic — protect your capital at every level."
            color="bg-sky-50 text-sky-600"
          />
        </div>
      </section>

      {/* ── HOW IT WORKS ───────────────────────────────────────────────────── */}
      <section id="how-it-works" className="bg-slate-50 border-y border-slate-200">
        <div className="max-w-5xl mx-auto px-6 py-24">
          <div className="text-center mb-14">
            <div className="text-sm font-semibold text-blue-600 uppercase tracking-widest mb-3">Process</div>
            <h2 className="text-4xl font-extrabold text-slate-900 mb-4">From idea to live trade in 4 steps</h2>
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
              title="Deploy to live accounts"
              desc="Link your Tradovate account, set your risk controls, and let the engine execute automatically. Monitor performance and trigger the kill switch anytime."
            />
          </div>
        </div>
      </section>

      {/* ── ICT STRATEGY HIGHLIGHT ─────────────────────────────────────────── */}
      <section className="max-w-6xl mx-auto px-6 py-24">
        <div className="grid md:grid-cols-2 gap-14 items-center">
          <div>
            <div className="text-sm font-semibold text-blue-600 uppercase tracking-widest mb-3">Strategy Engine</div>
            <h2 className="text-4xl font-extrabold text-slate-900 mb-5 leading-tight">
              Built for ICT & Smart Money Concepts
            </h2>
            <p className="text-slate-500 text-base leading-relaxed mb-8">
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
                <div key={item} className="flex items-center gap-3 text-sm text-slate-700">
                  <CheckCircle2 size={16} className="text-blue-500 flex-shrink-0"/>
                  {item}
                </div>
              ))}
            </div>
          </div>

          {/* Metrics cards column */}
          <div className="space-y-4">
            <div className="bg-white rounded-2xl border border-slate-200 p-5 shadow-sm">
              <div className="text-xs text-slate-400 mb-3 font-medium uppercase tracking-wider">Sample Backtest — ES 15m · 2 Years</div>
              <div className="grid grid-cols-3 gap-4">
                {[
                  { l: 'Win Rate', v: '64.2%', c: '#16a34a' },
                  { l: 'Profit Factor', v: '2.41', c: '#2563eb' },
                  { l: 'Net Profit', v: '+$48K', c: '#16a34a' },
                  { l: 'Max Drawdown', v: '8.3%', c: '#dc2626' },
                  { l: 'Total Trades', v: '312', c: '#475569' },
                  { l: 'Avg R:R', v: '2.2', c: '#2563eb' },
                ].map(({ l, v, c }) => (
                  <div key={l} className="bg-slate-50 rounded-xl p-3">
                    <div className="text-xs text-slate-400 mb-1">{l}</div>
                    <div className="text-lg font-bold" style={{ color: c }}>{v}</div>
                  </div>
                ))}
              </div>
              <div className="mt-4 h-20 rounded-lg overflow-hidden bg-slate-50 p-2">
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
      <section className="bg-slate-900 text-white">
        <div className="max-w-5xl mx-auto px-6 py-20 text-center">
          <Lock size={28} className="mx-auto text-blue-400 mb-4"/>
          <h2 className="text-3xl font-bold mb-4">Security built in from day one</h2>
          <p className="text-slate-400 text-base mb-10 max-w-xl mx-auto">
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
                <div className="text-sm text-slate-400 leading-relaxed">{desc}</div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ── PRICING PREVIEW ────────────────────────────────────────────────── */}
      <section className="max-w-5xl mx-auto px-6 py-24 text-center">
        <div className="text-sm font-semibold text-blue-600 uppercase tracking-widest mb-3">Pricing</div>
        <h2 className="text-4xl font-extrabold text-slate-900 mb-4">Plans for every stage</h2>
        <p className="text-slate-500 text-lg mb-10 max-w-xl mx-auto">Start with backtesting and upgrade as you scale. All plans include a 30-day free trial.</p>

        <div className="grid md:grid-cols-3 gap-6 mb-8">
          {[
            { name: 'Backtest', tag: 'Tier 1', desc: 'Build and backtest strategies with full historical data.', features: ['Strategy builder', 'Backtesting engine', 'Optimization engine'], cta: 'Get started', highlight: false },
            { name: 'Live Trader', tag: 'Tier 3', desc: 'Full access including paper and live trading with up to 5 accounts.', features: ['Everything in Tier 1', 'Paper trading', 'Live execution (2–5 accounts)', 'Real-time data feed'], cta: 'Get started', highlight: true },
            { name: 'Advanced', tag: 'Tier 4', desc: 'Scale to 20 simultaneous live accounts with full platform access.', features: ['Everything in Tier 3', 'Up to 20 live accounts', 'Priority support'], cta: 'Get started', highlight: false },
          ].map(({ name, tag, desc, features, cta, highlight }) => (
            <div key={name} className={`rounded-2xl border p-6 text-left relative ${highlight ? 'border-blue-500 bg-blue-600 text-white shadow-xl shadow-blue-200' : 'border-slate-200 bg-white'}`}>
              {highlight && (
                <div className="absolute -top-3 left-1/2 -translate-x-1/2 bg-amber-400 text-amber-900 text-xs font-bold px-3 py-1 rounded-full">
                  Most Popular
                </div>
              )}
              <div className={`text-xs font-semibold mb-1 ${highlight ? 'text-blue-200' : 'text-blue-600'}`}>{tag}</div>
              <div className={`text-xl font-bold mb-2 ${highlight ? 'text-white' : 'text-slate-900'}`}>{name}</div>
              <div className={`text-sm mb-5 leading-relaxed ${highlight ? 'text-blue-100' : 'text-slate-500'}`}>{desc}</div>
              <div className="space-y-2 mb-6">
                {features.map((f) => (
                  <div key={f} className={`flex items-center gap-2 text-sm ${highlight ? 'text-blue-50' : 'text-slate-600'}`}>
                    <CheckCircle2 size={14} className={highlight ? 'text-blue-200' : 'text-blue-500'}/>
                    {f}
                  </div>
                ))}
              </div>
              <Link to="/register" className={`block text-center text-sm font-semibold py-2.5 rounded-xl transition-colors ${highlight ? 'bg-white text-blue-700 hover:bg-blue-50' : 'bg-blue-600 text-white hover:bg-blue-700'}`}>
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
      <section className="bg-blue-600 text-white">
        <div className="max-w-4xl mx-auto px-6 py-20 text-center">
          <h2 className="text-4xl font-extrabold mb-4">Ready to trade with an edge?</h2>
          <p className="text-blue-100 text-lg mb-8 max-w-xl mx-auto">
            Start your 30-day free trial. No credit card required. Paper trading and limited backtesting included.
          </p>
          <Link to="/register"
            className="inline-flex items-center gap-2 bg-white text-blue-700 hover:bg-blue-50 font-bold px-8 py-4 rounded-xl text-base transition-colors shadow-lg">
            Start Free Trial
            <ArrowRight size={18}/>
          </Link>
        </div>
      </section>

      {/* ── FOOTER ─────────────────────────────────────────────────────────── */}
      <footer className="border-t border-slate-200 bg-white">
        <div className="max-w-6xl mx-auto px-6 py-10">
          <div className="flex flex-col md:flex-row items-center justify-between gap-4">
            <div className="flex items-center gap-2">
              <BarChart2 size={18} className="text-blue-600"/>
              <span className="font-bold text-slate-800">Edge Asset Management</span>
            </div>
            <div className="flex items-center gap-6 text-sm text-slate-500">
              <Link to="/pricing" className="hover:text-slate-800">Pricing</Link>
              <Link to="/login" className="hover:text-slate-800">Sign In</Link>
              <Link to="/register" className="hover:text-slate-800">Register</Link>
            </div>
            <p className="text-sm text-slate-400">© {new Date().getFullYear()} Edge Asset Management. All rights reserved.</p>
          </div>
          <div className="mt-6 p-4 bg-amber-50 border border-amber-200 rounded-xl text-xs text-amber-700">
            <strong>Risk Disclosure:</strong> Futures trading involves substantial risk of loss and is not appropriate for all investors. Past performance is not necessarily indicative of future results. Edge Asset Management is a software platform, not a registered investment adviser. Always trade with capital you can afford to lose.
          </div>
        </div>
      </footer>

    </div>
  )
}
