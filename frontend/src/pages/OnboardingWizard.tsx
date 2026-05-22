import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { CheckCircle2, ArrowRight, ArrowLeft, Sparkles, Target, Briefcase, Clock, DollarSign } from 'lucide-react'

type AnswerSet = {
  trades: 'futures' | 'stocks' | 'options' | 'all' | null
  experience: 'beginner' | 'intermediate' | 'advanced' | null
  account_size: '<10k' | '10k-50k' | '50k-250k' | '250k+' | null
  goal: 'signals_only' | 'auto_execute' | 'learn' | 'prop_firm' | null
  session: 'ny_am' | 'all' | 'london' | 'asia' | null
  risk_per_trade: '0.5' | '1' | '2' | '5' | null
}

function recommendTier(a: AnswerSet): { tier: string; price: number; reason: string } {
  if (a.goal === 'auto_execute' && a.trades !== 'futures') {
    return { tier: 'Tier 5', price: 399, reason: 'Auto-execute on options/stocks needs Tier 5 (no manual confirm)' }
  }
  if (a.goal === 'auto_execute') {
    return { tier: 'Tier 4', price: 199, reason: 'Auto-confirm options live trading via Tradier' }
  }
  if (a.goal === 'prop_firm' || a.trades === 'futures') {
    return { tier: 'Tier 2', price: 49, reason: 'Futures signals via email — execute manually inside your prop-firm account' }
  }
  if (a.trades === 'options' || a.trades === 'stocks') {
    return { tier: 'Tier 3', price: 99, reason: 'Options scanner + morning email batch + manual execution' }
  }
  if (a.goal === 'learn') {
    return { tier: 'Free Trial', price: 0, reason: '30-day trial covers paper trading + backtesting' }
  }
  return { tier: 'Tier 2', price: 49, reason: 'Futures signals — most popular starter' }
}

export default function OnboardingWizard({ onDone }: { onDone?: () => void }) {
  const navigate = useNavigate()
  const [step, setStep] = useState(0)
  const [a, setA] = useState<AnswerSet>({
    trades: null, experience: null, account_size: null,
    goal: null, session: null, risk_per_trade: null,
  })

  const recommendation = recommendTier(a)
  const last = step >= 6

  const steps = [
    {
      icon: Target,
      title: "What do you trade?",
      sub: "We'll tailor the recommendations.",
      options: [
        { id: 'futures', label: 'Futures', desc: 'ES, NQ, RTY, YM (CME)' },
        { id: 'stocks',  label: 'Stocks',  desc: 'AAPL, NVDA, TSLA, etc.' },
        { id: 'options', label: 'Options', desc: 'Calls, puts, spreads' },
        { id: 'all',     label: 'All of the above', desc: 'Mixed portfolio' },
      ],
      set: (v: string) => setA({...a, trades: v as any}),
      val: a.trades,
    },
    {
      icon: Sparkles,
      title: "How experienced are you?",
      sub: "Just so we know how much guidance to put in the UI.",
      options: [
        { id: 'beginner',     label: 'Beginner',     desc: 'Just learning — paper only at first' },
        { id: 'intermediate', label: 'Intermediate', desc: 'A few years, profitable but inconsistent' },
        { id: 'advanced',     label: 'Advanced',     desc: 'I want maximum control + auto-execute' },
      ],
      set: (v: string) => setA({...a, experience: v as any}),
      val: a.experience,
    },
    {
      icon: DollarSign,
      title: "Account size?",
      sub: "Sets your default position-sizing rules.",
      options: [
        { id: '<10k',     label: 'Under $10K', desc: 'Tight risk caps, micros recommended' },
        { id: '10k-50k',  label: '$10K - $50K',  desc: 'Mini futures + small stock positions' },
        { id: '50k-250k', label: '$50K - $250K', desc: 'Full position sizing' },
        { id: '250k+',    label: 'Over $250K',   desc: 'Multi-strategy concurrent' },
      ],
      set: (v: string) => setA({...a, account_size: v as any}),
      val: a.account_size,
    },
    {
      icon: Briefcase,
      title: "What's your goal?",
      sub: "Determines whether you need live broker integration.",
      options: [
        { id: 'signals_only', label: 'Just signals via email', desc: 'I place trades myself' },
        { id: 'auto_execute', label: 'Auto-execute on a real broker', desc: 'Tradier, IBKR, etc.' },
        { id: 'prop_firm',    label: 'Prop firm — Apex, Topstep, TPT', desc: 'Manual entry only (algo banned)' },
        { id: 'learn',        label: 'Learning + backtesting', desc: 'No real money yet' },
      ],
      set: (v: string) => setA({...a, goal: v as any}),
      val: a.goal,
    },
    {
      icon: Clock,
      title: "Which sessions do you want signals for?",
      sub: "Controls when the bot scans + emails you.",
      options: [
        { id: 'ny_am',  label: 'NY AM only', desc: '9:30 AM – 12:00 PM ET (most common)' },
        { id: 'all',    label: 'All sessions', desc: 'Asia + London + NY (24-hour signals)' },
        { id: 'london', label: 'London only', desc: '2:00 AM – 5:00 AM ET' },
        { id: 'asia',   label: 'Asia only', desc: '8:00 PM – 12:00 AM ET' },
      ],
      set: (v: string) => setA({...a, session: v as any}),
      val: a.session,
    },
    {
      icon: Target,
      title: "Risk per trade?",
      sub: "Percent of your account you're willing to lose per trade. Conservative = 0.5–1%.",
      options: [
        { id: '0.5', label: '0.5% per trade', desc: 'Very conservative — 200 losers before account is gone' },
        { id: '1',   label: '1% per trade',  desc: 'Conservative — industry standard' },
        { id: '2',   label: '2% per trade',  desc: 'Moderate — typical for experienced traders' },
        { id: '5',   label: '5% per trade',  desc: 'Aggressive — only if you have edge' },
      ],
      set: (v: string) => setA({...a, risk_per_trade: v as any}),
      val: a.risk_per_trade,
    },
  ]

  if (last) {
    return (
      <div className="max-w-2xl mx-auto px-6 py-12">
        <div className="rounded-3xl bg-gradient-to-br from-slate-900 via-slate-900 to-violet-950 text-white p-8 md:p-10 shadow-2xl">
          <CheckCircle2 size={48} className="text-emerald-400 mb-4"/>
          <div className="text-[10px] uppercase tracking-[0.2em] text-violet-300 font-bold mb-1">Your recommendation</div>
          <h1 className="text-3xl font-extrabold mb-2">{recommendation.tier}{recommendation.price > 0 && <span className="text-violet-300"> · ${recommendation.price}/mo</span>}</h1>
          <p className="text-slate-300 mb-6">{recommendation.reason}</p>

          <div className="grid grid-cols-2 gap-3 mb-6 text-sm">
            <div className="bg-white/10 rounded-xl p-3">
              <div className="text-[10px] uppercase tracking-wider text-violet-300 font-bold mb-1">Trades</div>
              <div className="font-bold">{a.trades?.toUpperCase()}</div>
            </div>
            <div className="bg-white/10 rounded-xl p-3">
              <div className="text-[10px] uppercase tracking-wider text-violet-300 font-bold mb-1">Sessions</div>
              <div className="font-bold">{a.session?.toUpperCase().replace('_', ' ')}</div>
            </div>
            <div className="bg-white/10 rounded-xl p-3">
              <div className="text-[10px] uppercase tracking-wider text-violet-300 font-bold mb-1">Risk per trade</div>
              <div className="font-bold">{a.risk_per_trade}%</div>
            </div>
            <div className="bg-white/10 rounded-xl p-3">
              <div className="text-[10px] uppercase tracking-wider text-violet-300 font-bold mb-1">Account size</div>
              <div className="font-bold">{a.account_size}</div>
            </div>
          </div>

          <div className="flex gap-3">
            <button onClick={() => setStep(0)} className="flex-1 bg-white/10 hover:bg-white/20 text-white py-3 rounded-xl text-sm font-bold">
              ← Edit answers
            </button>
            <button onClick={() => {
              localStorage.setItem('edge_onboarding_answers', JSON.stringify(a))
              if (onDone) onDone()
              else navigate(recommendation.price > 0 ? '/pricing' : '/app')
            }} className="flex-1 bg-violet-500 hover:bg-violet-400 text-white py-3 rounded-xl text-sm font-bold inline-flex items-center justify-center gap-2">
              {recommendation.price > 0 ? 'See plans' : 'Get started'} <ArrowRight size={16}/>
            </button>
          </div>
        </div>
        <p className="text-xs text-slate-400 text-center mt-4">You can change these any time from your Profile.</p>
      </div>
    )
  }

  const cur = steps[step]
  const Icon = cur.icon

  return (
    <div className="max-w-2xl mx-auto px-6 py-12">
      <div className="mb-6">
        <div className="flex items-center justify-between mb-3">
          <span className="text-[10px] uppercase tracking-[0.2em] text-violet-600 dark:text-violet-400 font-bold">Setup · step {step+1} of {steps.length}</span>
          <span className="text-[10px] text-slate-400 dark:text-slate-500">{Math.round(((step+1)/steps.length)*100)}%</span>
        </div>
        <div className="h-1 bg-slate-100 dark:bg-slate-800 rounded-full overflow-hidden">
          <div className="h-full bg-gradient-to-r from-violet-500 to-fuchsia-500 transition-all" style={{ width: `${((step+1)/steps.length)*100}%` }}/>
        </div>
      </div>

      <div className="rounded-2xl bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 p-6 md:p-8 shadow-lg">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-10 h-10 rounded-xl bg-violet-100 dark:bg-violet-900/40 text-violet-700 dark:text-violet-300 flex items-center justify-center">
            <Icon size={20}/>
          </div>
          <div>
            <h2 className="text-xl font-extrabold text-slate-900 dark:text-slate-100">{cur.title}</h2>
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5">{cur.sub}</p>
          </div>
        </div>

        <div className="space-y-2">
          {cur.options.map(o => (
            <button key={o.id} onClick={() => cur.set(o.id)}
              className={`w-full text-left rounded-xl border-2 px-4 py-3 transition-all ${
                cur.val === o.id
                  ? 'border-violet-500 bg-violet-50 dark:bg-violet-900/20'
                  : 'border-slate-200 dark:border-slate-700 hover:border-slate-300 dark:hover:border-slate-600'
              }`}>
              <div className="flex items-center gap-3">
                <div className={`w-5 h-5 rounded-full border-2 flex-shrink-0 ${cur.val === o.id ? 'border-violet-500 bg-violet-500' : 'border-slate-300 dark:border-slate-600'}`}>
                  {cur.val === o.id && <CheckCircle2 size={20} className="text-white -m-0.5"/>}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="font-bold text-sm text-slate-900 dark:text-slate-100">{o.label}</div>
                  <div className="text-xs text-slate-500 dark:text-slate-400">{o.desc}</div>
                </div>
              </div>
            </button>
          ))}
        </div>

        <div className="flex gap-3 mt-6">
          <button onClick={() => setStep(s => Math.max(0, s-1))} disabled={step === 0}
            className="px-4 py-2 rounded-lg text-sm font-bold text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 disabled:opacity-30 inline-flex items-center gap-1.5">
            <ArrowLeft size={14}/> Back
          </button>
          <div className="flex-1"/>
          <button onClick={() => setStep(s => s+1)} disabled={!cur.val}
            className="px-5 py-2 rounded-lg text-sm font-bold bg-violet-600 hover:bg-violet-700 disabled:opacity-30 text-white inline-flex items-center gap-1.5">
            {step === steps.length - 1 ? 'See my plan' : 'Next'} <ArrowRight size={14}/>
          </button>
        </div>
      </div>
    </div>
  )
}
