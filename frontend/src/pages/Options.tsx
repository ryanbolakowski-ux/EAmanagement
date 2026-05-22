import { useState } from 'react'
import { Link } from 'react-router-dom'
import { LineChart, TrendingUp, Zap, Layers, Calendar, RotateCw, Mail, ShieldCheck, Clock } from 'lucide-react'

type Mode = {
  slug: string
  name: string
  icon: any
  tagline: string
  detail: string
  vehicle: string
  rrTarget: string
}

const MODES: Mode[] = [
  {
    slug: 'trend_pullback', name: 'Trend Following — Pullbacks', icon: TrendingUp,
    tagline: 'Buy options on pullbacks inside a strong existing trend.',
    detail: 'Filter for stocks in a clear uptrend (50-day EMA above 200-day) or downtrend. When price pulls back to the 50-day EMA or recent swing support, the bot fires a long-call (uptrend) or long-put (downtrend) signal. RSI(14) must agree with bias. The pullback is the entry — the trend is the edge.',
    vehicle: 'Long calls (uptrend) / long puts (downtrend), 30-50 delta, 30-60 DTE',
    rrTarget: 'TP +75% premium · Stop -30% OR underlying breaks pullback low',
  },
  {
    slug: 'breakout', name: 'Breakout Trading', icon: Zap,
    tagline: 'Enter on confirmed breakouts above resistance with volume.',
    detail: 'Bot watches for price breaks above 20-day high (longs) or below 20-day low (shorts). Volume on the breakout day must be at least 2× the 20-day average — that\'s the institutional confirmation. Hard stop at 1% below the breakout level.',
    vehicle: 'Long calls/puts, 30-50 delta, 30-60 DTE',
    rrTarget: 'TP at the measured-move target (range height projected) · Stop 1% below breakout',
  },
  {
    slug: 'vertical_spread', name: 'Vertical Spreads (Bull Call / Bear Put)', icon: Layers,
    tagline: 'Cap cost and limit theta drag with defined-risk spreads.',
    detail: 'When IV is elevated, buying outright premium hurts. Vertical spreads (buy one option, sell another N strikes higher/lower) cut net cost dramatically. Bot fires "Buy ATM call + Sell strike +5 call" for bullish, mirror for bearish. Reduces leverage but also reduces theta and IV exposure — the right tool when premium is rich.',
    vehicle: 'Bull call spread / bear put spread, ATM long leg, +5 strikes short leg, 30-60 DTE',
    rrTarget: 'TP +50% of max profit · Stop at -50% of debit paid',
  },
  {
    slug: 'earnings_catalyst', name: 'Earnings / Catalyst Plays', icon: Calendar,
    tagline: 'Buy a straddle ahead of high-IV catalysts. Small size only.',
    detail: 'Earnings, FDA approvals, Fed days produce binary moves. Bot identifies stocks with a catalyst within 5 days and high historical earnings-move volatility. Buys an ATM straddle (call + put same strike) 1–3 days before. Profits whichever way price breaks. Position size kept tiny because IV crush after the event can wreck both legs.',
    vehicle: 'ATM straddle (long call + long put same strike), 14-30 DTE',
    rrTarget: 'TP +100% on the straddle · Stop -50%, time-stop the day after the event',
  },
  {
    slug: 'wheel', name: 'The Wheel Strategy', icon: RotateCw,
    tagline: 'Sell cash-secured puts on stocks you want to own; if assigned, sell calls.',
    detail: 'Premium-collection strategy on blue-chip names you\'d be happy holding. Bot sells cash-secured puts at 30-delta strikes — you collect premium, and if assigned, you own the shares at a discount. Once assigned, bot rotates into selling covered calls at 30-delta until shares get called away. Repeat. Lower-risk, slower-growth.',
    vehicle: 'Short cash-secured puts (30 delta) → assigned shares → short covered calls (30 delta), 30-45 DTE rolls',
    rrTarget: 'TP +50% of premium collected (close early to redeploy) · Hold to expiration if needed',
  },
]

export default function Options() {
  const [submitted, setSubmitted] = useState(false)
  return (
    <div className="space-y-6 max-w-5xl mx-auto px-4 sm:px-6 py-6">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="flex-1 min-w-0">
          <h1 className="text-2xl font-extrabold text-slate-900 dark:text-slate-100 flex items-center gap-2">
            <LineChart size={22}/> Swing Options Trading
            <span className="ml-2 text-[10px] font-bold uppercase tracking-widest bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 px-2 py-0.5 rounded">Live</span>
          </h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Five distinct swing strategies, plus universal risk rules. Each mode is its own option in the Strategy Builder — pick one that fits market conditions and let the bot screen the universe for you.
          </p>
        </div>
        <Link to="/app/options/sessions"
          className="inline-flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-bold border border-blue-200 dark:border-blue-700/60 text-blue-700 dark:text-blue-300 bg-blue-50 dark:bg-blue-900/20 hover:bg-blue-100 dark:hover:bg-blue-900/30 flex-shrink-0">
          View Sessions →
        </Link>
      </div>

      {/* Risk rules — apply across all modes */}
      <section className="rounded-xl border border-blue-200 dark:border-blue-800/50 bg-blue-50/60 dark:bg-blue-900/20 p-5">
        <div className="flex items-center gap-2 mb-3">
          <ShieldCheck size={16} className="text-blue-600"/>
          <h2 className="text-sm font-bold uppercase tracking-widest text-slate-700 dark:text-slate-200">Universal risk rules — applied to every mode</h2>
        </div>
        <ul className="text-sm text-slate-700 dark:text-slate-200 space-y-1.5 leading-relaxed list-disc list-inside">
          <li><strong>Position sizing:</strong> max <strong>1–2%</strong> of total capital per trade — configurable, default 1.5%</li>
          <li><strong>Stop losses:</strong> hard stops outside key support/resistance — typically <strong>1% below a breakout level</strong> on the underlying</li>
          <li><strong>Expirations:</strong> minimum <strong>30 DTE</strong> to minimize theta decay; ITM strikes available as a "safer" toggle for users who want lower leverage</li>
          <li><strong>Earnings filter:</strong> auto-skip trades within <strong>7 days of earnings</strong> on every mode <em>except</em> Earnings/Catalyst Plays</li>
          <li><strong>Sandbox mode:</strong> every new broker account starts in Sandbox so the bot's sizing/timing can be verified before real money is at risk</li>
        </ul>
      </section>

      {/* The 5 modes */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-bold uppercase tracking-widest text-slate-700 dark:text-slate-200">The five modes</h2>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {MODES.map(m => (
            <div key={m.slug} className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4">
              <div className="flex items-start gap-3 mb-2">
                <div className="w-10 h-10 rounded-xl bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 flex items-center justify-center flex-shrink-0">
                  <m.icon size={18}/>
                </div>
                <div className="min-w-0">
                  <h3 className="font-bold text-slate-900 dark:text-slate-100">{m.name}</h3>
                  <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5 leading-relaxed">{m.tagline}</p>
                </div>
              </div>
              <p className="text-xs text-slate-700 dark:text-slate-300 leading-relaxed mb-3">{m.detail}</p>
              <div className="text-[11px] space-y-1 border-t border-slate-100 dark:border-slate-800 pt-2">
                <div><span className="font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider">Vehicle:</span> <span className="text-slate-700 dark:text-slate-300">{m.vehicle}</span></div>
                <div><span className="font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider">Targets:</span> <span className="text-slate-700 dark:text-slate-300">{m.rrTarget}</span></div>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* What's needed */}
      <section className="rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 p-5">
        <h2 className="text-sm font-bold uppercase tracking-widest text-slate-700 dark:text-slate-200 mb-3 flex items-center gap-2">
          <Clock size={14}/> What's needed to ship the engine
        </h2>
        <ul className="text-sm text-slate-700 dark:text-slate-300 space-y-2 leading-relaxed">
          <li>• <strong>Tradier developer token</strong> ($0) — provides options chains, equity quotes, and execution. Single token serves the whole platform.</li>
          <li>• <strong>Universe loader</strong> — pulls all ~5,000 optionable US stocks, filters down to liquid names with daily-bar trend, RSI, volume, and earnings calendar (Yahoo, free).</li>
          <li>• <strong>Strategy router</strong> — dispatches the right entry/exit logic based on which of the 5 modes the user selected.</li>
          <li>• <strong>Black-Scholes simulator</strong> — used for backtests since real historical chains are $500+/mo. Approximate but useful.</li>
          <li>• Build time: <strong>~2 weeks</strong> end-to-end after Tradier token is in hand. Trend Following + Breakout + Wheel ship first; Vertical Spread + Earnings Catalyst follow a week later.</li>
        </ul>
      </section>

      {/* Notify */}
      <section className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-5">
        <h2 className="text-sm font-bold uppercase tracking-widest text-slate-700 dark:text-slate-200 mb-2">Get notified when it ships</h2>
        {!submitted ? (
          <button onClick={() => setSubmitted(true)} className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold px-4 py-2 rounded-lg">
            <Mail size={14}/> Notify me at my account email
          </button>
        ) : (
          <div className="text-xs text-green-700 bg-green-50 border border-green-200 px-3 py-2 rounded-lg inline-block dark:bg-green-900/20 dark:text-green-300 dark:border-green-900">
            ✓ You'll get an email when each mode goes live.
          </div>
        )}
      </section>

      <div className="text-xs text-slate-400 dark:text-slate-500 leading-relaxed pt-4 border-t border-slate-200 dark:border-slate-800">
        Each mode will appear as a separate template in the <Link to="/app/strategies" className="text-blue-600 hover:underline">Strategy Builder</Link> the moment the options engine ships. You'll be able to deploy multiple modes in parallel, each on its own ticker universe.
      </div>
    </div>
  )
}
