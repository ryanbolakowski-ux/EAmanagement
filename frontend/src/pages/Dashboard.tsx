import { useQuery } from '@tanstack/react-query'
import { dashboardApi, liveTradingApi, type DailyBias } from '../api/endpoints'
import { useAuthStore } from '../stores/authStore'
import { Link } from 'react-router-dom'
import {
  TrendingUp, FlaskConical, PlayCircle, Zap, Sparkles,
  ArrowUpRight, ArrowDownRight, BarChart2, Activity, Minus, CheckCircle2,
  Briefcase, Target, Sliders, BookOpen,
} from 'lucide-react'
import {
  HeroHeader, MetricRow, MetricCard, SectionHeader, EmptyState,
  Sparkline, fmt, fmtUsd, pnlColor, pnlSign,
} from '../components/DashboardKit'

const BIAS_STYLE: Record<DailyBias['bias'], { label: string; tone: string; bar: string; icon: any }> = {
  strong_bullish: { label: 'Strong Bullish', tone: 'bg-emerald-600 text-white border-emerald-600',  bar: 'bg-emerald-500', icon: ArrowUpRight   },
  bullish:        { label: 'Bullish',        tone: 'bg-emerald-50 dark:bg-emerald-900/20 text-emerald-700 dark:text-emerald-300 border-emerald-200 dark:border-emerald-800',  bar: 'bg-emerald-400', icon: ArrowUpRight },
  neutral:        { label: 'Neutral',        tone: 'bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300 border-slate-200 dark:border-slate-700', bar: 'bg-slate-300', icon: Minus },
  bearish:        { label: 'Bearish',        tone: 'bg-rose-50 dark:bg-rose-900/20 text-rose-700 dark:text-rose-300 border-rose-200 dark:border-rose-900',  bar: 'bg-rose-400',  icon: ArrowDownRight },
  strong_bearish: { label: 'Strong Bearish', tone: 'bg-rose-600 text-white border-rose-600',  bar: 'bg-rose-500',  icon: ArrowDownRight },
}

const SESSION_LABEL: Record<string, { label: string; tone: string }> = {
  asian:     { label: 'Asian',     tone: 'bg-violet-50 text-violet-700 dark:bg-violet-900/30 dark:text-violet-300' },
  london:    { label: 'London',    tone: 'bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300' },
  ny:        { label: 'NY',        tone: 'bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300' },
  overnight: { label: 'Overnight', tone: 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300' },
  unknown:   { label: '—',         tone: 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400' },
}

function BiasCard({ b }: { b: DailyBias & {
  last_close?: number; narrative?: string;
  draw_target?: { label: string; level: number; side: string };
  pdh?: number; pdl?: number; pdc?: number;
} }) {
  const style = BIAS_STYLE[b.bias]
  const Icon = style.icon
  const width = Math.min(Math.abs(b.strength_pct) / 3 * 100, 100)
  const session = b.current_session ?? 'unknown'
  const sessionStyle = SESSION_LABEL[session] ?? SESSION_LABEL.unknown
  return (
    <Link to="/app/bias"
      className="group rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4 hover:border-violet-300 dark:hover:border-violet-700 hover:shadow-lg transition-all">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className="font-extrabold text-slate-900 text-base dark:text-slate-100">{b.instrument}</span>
          {b.last_close && (
            <span className="text-xs tabular-nums text-slate-500 dark:text-slate-400">${b.last_close.toLocaleString()}</span>
          )}
          <span className={`text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${sessionStyle.tone}`}>
            {sessionStyle.label}
          </span>
        </div>
        <span className={`inline-flex items-center gap-1 text-[11px] font-bold px-2 py-1 rounded-lg border ${style.tone}`}>
          <Icon size={11} strokeWidth={2.5}/>{style.label}
        </span>
      </div>
      <div className="h-1.5 bg-slate-100 dark:bg-slate-800 rounded-full overflow-hidden mb-2">
        <div className={`h-full ${style.bar} transition-all duration-500`} style={{ width: `${width}%` }}/>
      </div>
      {b.narrative && (
        <p className="text-[11px] text-slate-600 dark:text-slate-300 leading-snug line-clamp-2 mb-1.5">{b.narrative}</p>
      )}
      {b.draw_target && (
        <div className="text-[10px] text-violet-600 dark:text-violet-400 font-bold tabular-nums">
          → {b.draw_target.label} {typeof b.draw_target.level === 'number' ? `$${b.draw_target.level.toLocaleString()}` : ''}
        </div>
      )}
      {(b.pdh || b.pdl) && (
        <div className="flex justify-between text-[10px] text-slate-400 dark:text-slate-500 mt-1.5 pt-1.5 border-t border-slate-100 dark:border-slate-800 tabular-nums">
          {b.pdh && <span>PDH ${b.pdh.toLocaleString()}</span>}
          {b.pdl && <span>PDL ${b.pdl.toLocaleString()}</span>}
        </div>
      )}
    </Link>
  )
}

function QuickActionCard({
  to, icon: Icon, label, desc, accent,
}: { to: string; icon: any; label: string; desc: string; accent: 'violet' | 'emerald' | 'rose' | 'amber' | 'blue' }) {
  const accents = {
    violet:  'from-violet-100 to-violet-200 text-violet-700 dark:from-violet-900/40 dark:to-violet-800/40 dark:text-violet-300',
    emerald: 'from-emerald-100 to-emerald-200 text-emerald-700 dark:from-emerald-900/40 dark:to-emerald-800/40 dark:text-emerald-300',
    rose:    'from-rose-100 to-rose-200 text-rose-700 dark:from-rose-900/40 dark:to-rose-800/40 dark:text-rose-300',
    amber:   'from-amber-100 to-amber-200 text-amber-700 dark:from-amber-900/40 dark:to-amber-800/40 dark:text-amber-300',
    blue:    'from-blue-100 to-blue-200 text-blue-700 dark:from-blue-900/40 dark:to-blue-800/40 dark:text-blue-300',
  }
  return (
    <Link to={to} className="group rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-5 hover:border-violet-300 dark:hover:border-violet-700 hover:shadow-lg transition-all">
      <div className={`w-12 h-12 rounded-xl bg-gradient-to-br ${accents[accent]} flex items-center justify-center mb-3`}>
        <Icon size={20}/>
      </div>
      <div className="font-extrabold text-slate-900 dark:text-slate-100 mb-1">{label}</div>
      <div className="text-xs text-slate-500 dark:text-slate-400 leading-relaxed">{desc}</div>
      <div className="text-[11px] font-bold text-violet-600 dark:text-violet-400 mt-3 inline-flex items-center gap-1 group-hover:gap-2 transition-all">
        Open <ArrowUpRight size={11}/>
      </div>
    </Link>
  )
}

export default function Dashboard() {
  const { user } = useAuthStore()

  const { data, isLoading } = useQuery({
    queryKey: ['dashboard'],
    queryFn: () => dashboardApi.summary().then(r => r.data),
    refetchInterval: 60000,
    refetchOnMount: 'always',
    staleTime: 0,
  })

  const { data: biasData, isLoading: biasLoading, isError: biasError } = useQuery({
    queryKey: ['daily-bias'],
    queryFn: () => dashboardApi.bias().then(r => r.data),
    refetchInterval: 5 * 60 * 1000,
  })

  const { data: portfolio } = useQuery({
    queryKey: ['portfolio-summary-dash'],
    queryFn: () => liveTradingApi.portfolioSummary().then((r: any) => r.data),
    refetchInterval: 60000,
    retry: false,
  })

  const paperPnl = data?.paper_trading.net_pnl ?? 0
  const livePnl  = data?.live_trading.net_pnl ?? 0
  const combinedPnl = paperPnl + livePnl
  const totalTrades = (data?.paper_trading.total_trades ?? 0) + (data?.live_trading.total_trades ?? 0)
  const blendedWinRate = totalTrades > 0
    ? (((data?.paper_trading.win_rate ?? 0) * (data?.paper_trading.total_trades ?? 0)
        + (data?.live_trading.win_rate ?? 0) * (data?.live_trading.total_trades ?? 0)) / totalTrades) * 100
    : 0

  const sparkData = (portfolio?.equity_curve_14d || []).map((p: any) => p.pnl)
    .reduce((acc: number[], v: number) => { acc.push((acc[acc.length - 1] || 0) + v); return acc }, [] as number[])

  return (
    <div className="space-y-6 max-w-7xl mx-auto px-4 sm:px-6 py-6">

      {/* HERO — compact today-focused */}
      {(() => {
        const todayLive = (portfolio?.today_pnl ?? 0) + (portfolio?.today_unrealized_pnl ?? 0)
        const todayLiveClass = todayLive > 0 ? 'text-emerald-500' : todayLive < 0 ? 'text-rose-500' : 'text-slate-700 dark:text-slate-200'
        const greeting = `Good ${new Date().getHours() < 12 ? 'morning' : new Date().getHours() < 17 ? 'afternoon' : 'evening'}, ${user?.username || ''}`
        return (
          <div className="rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-5">
            <div className="flex items-start justify-between mb-4 gap-3">
              <div>
                <div className="text-xs text-slate-500 dark:text-slate-400">{greeting}</div>
                <div className="text-base font-extrabold text-slate-900 dark:text-slate-100">Trading dashboard</div>
              </div>
              <Link to="/app/live" className="text-xs text-violet-600 font-bold hover:underline">Live trading →</Link>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <div className="rounded-xl border border-slate-200 dark:border-slate-700 p-3">
                <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Live P&L today</div>
                <div className={`text-2xl font-extrabold tabular-nums mt-1 ${todayLiveClass}`}>{pnlSign(todayLive)}{fmtUsd(todayLive, 0)}</div>
                <div className="text-[10px] text-slate-400 mt-0.5">
                  realized {pnlSign(portfolio?.today_pnl || 0)}{fmtUsd(portfolio?.today_pnl || 0, 0)}
                  {portfolio?.today_unrealized_pnl !== undefined && portfolio?.today_unrealized_pnl !== 0 && (
                    <> · open {pnlSign(portfolio.today_unrealized_pnl)}{fmtUsd(portfolio.today_unrealized_pnl, 0)}</>
                  )}
                </div>
              </div>
              <div className="rounded-xl border border-slate-200 dark:border-slate-700 p-3">
                <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Paper P&L (lifetime)</div>
                <div className={`text-2xl font-extrabold tabular-nums mt-1 ${paperPnl > 0 ? 'text-emerald-500' : paperPnl < 0 ? 'text-rose-500' : 'text-slate-700 dark:text-slate-200'}`}>{pnlSign(paperPnl)}{fmtUsd(paperPnl, 0)}</div>
                <div className="text-[10px] text-slate-400 mt-0.5">{data?.paper_trading?.total_trades ?? 0} trades · {((data?.paper_trading?.win_rate ?? 0) * 100).toFixed(0)}% win</div>
              </div>
              <div className="rounded-xl border border-slate-200 dark:border-slate-700 p-3">
                <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Trades today</div>
                <div className="text-2xl font-extrabold text-slate-900 dark:text-slate-100 tabular-nums mt-1">{portfolio?.open_positions_count ?? 0}</div>
                <div className="text-[10px] text-slate-400 mt-0.5">open positions · {data?.strategy_count ?? 0} strategies</div>
              </div>
              <div className="rounded-xl border border-slate-200 dark:border-slate-700 p-3">
                <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Win rate (blended)</div>
                <div className="text-2xl font-extrabold text-slate-900 dark:text-slate-100 tabular-nums mt-1">{blendedWinRate.toFixed(1)}%</div>
                <div className="text-[10px] text-slate-400 mt-0.5">across all modes</div>
              </div>
            </div>
            {/* Accounts + balances grid */}
            {portfolio?.per_account && portfolio.per_account.length > 0 && (
              <div className="mt-4 pt-4 border-t border-slate-200 dark:border-slate-800">
                <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold mb-2">Accounts</div>
                <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-2">
                  {portfolio.per_account.map((a: any) => {
                    const isOn = a.trading_enabled !== false
                    return (
                      <div key={a.id} className="flex items-center justify-between bg-slate-50 dark:bg-slate-800/50 rounded-lg px-3 py-2">
                        <div className="min-w-0">
                          <div className="text-xs font-bold text-slate-900 dark:text-slate-100 truncate">{a.account_name}</div>
                          <div className="text-[10px] text-slate-500 dark:text-slate-400">{a.broker}{a.sandbox_mode ? ' · sandbox' : ''}</div>
                        </div>
                        <div className="text-right">
                          <div className="text-sm font-bold tabular-nums">${(a.equity || 0).toLocaleString(undefined, {maximumFractionDigits: 0})}</div>
                          <div className={`text-[10px] font-bold uppercase tracking-wider ${isOn ? 'text-emerald-600' : 'text-slate-400'}`}>
                            {isOn ? '● trading' : '○ paused'}
                          </div>
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}
          </div>
        )
      })()}

      {/* TRIAL BANNER */}
      {user?.subscription_tier === 'free_trial' && user?.trial_ends_at && (
        <div className="rounded-2xl border border-amber-200 dark:border-amber-900 bg-gradient-to-r from-amber-50 to-yellow-50 dark:from-amber-900/20 dark:to-yellow-900/20 px-5 py-4 flex items-center justify-between gap-4">
          <div className="min-w-0">
            <div className="font-extrabold text-amber-900 dark:text-amber-100 text-sm">Free trial active</div>
            <div className="text-xs text-amber-700 dark:text-amber-300 mt-0.5">
              Expires {new Date(user.trial_ends_at).toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' })}
              {' '}— upgrade for live trading + auto-execute
            </div>
          </div>
          <Link to="/pricing" className="bg-amber-600 hover:bg-amber-700 text-white text-xs font-bold px-4 py-2 rounded-lg flex-shrink-0">
            View Plans
          </Link>
        </div>
      )}

      {/* DAILY BIAS — futures bias for ES/NQ/RTY/YM, always shown */}
      <div>
        <SectionHeader
          title="Daily Bias · Futures"
          right={<span className="text-[10px] text-slate-400 dark:text-slate-500 uppercase tracking-wider">EMA(9/21) · auto-refresh · click any card for detail</span>}
        />
        {biasData?.biases && biasData.biases.length > 0 ? (
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            {biasData.biases.map(b => <BiasCard key={b.instrument} b={b as any}/>)}
          </div>
        ) : (
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            {['ES','NQ','RTY','YM'].map(t => (
              <div key={t} className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4">
                <div className="flex items-center justify-between mb-2">
                  <span className="font-extrabold text-slate-900 dark:text-slate-100">{t}</span>
                  <span className="text-[11px] text-slate-400 dark:text-slate-500">{biasLoading ? 'loading…' : (biasError ? 'unavailable' : 'no data yet')}</span>
                </div>
                <div className="h-1.5 bg-slate-100 dark:bg-slate-800 rounded-full animate-pulse"/>
                <div className="h-2 bg-slate-100 dark:bg-slate-800 rounded mt-3 animate-pulse"/>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* QUICK ACTIONS */}
      <div>
        <SectionHeader title="Quick actions"/>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <QuickActionCard to="/app/strategies"  icon={TrendingUp}  label="Strategies"     desc="Build, edit, and share rule-based trading strategies" accent="violet"/>
          <QuickActionCard to="/app/backtests"   icon={FlaskConical} label="Backtests"     desc="Test strategies on 2+ years of historical data" accent="blue"/>
          <QuickActionCard to="/app/paper"       icon={PlayCircle}   label="Paper Trading" desc="Simulate live trades with zero risk · futures & options" accent="emerald"/>
          <QuickActionCard to="/app/live"     icon={Zap}          label="Live Trading"  desc="Deploy to your linked broker with sizing controls" accent="rose"/>
        </div>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mt-3">
          <QuickActionCard to="/app/plain-english" icon={Sparkles}     label="AI Builder"    desc="Describe a strategy in plain English; we build it" accent="amber"/>
          <QuickActionCard to="/app/email-signals" icon={Activity}   label="Email Signals" desc="Email/push alerts for prop-firm accounts" accent="blue"/>
          <QuickActionCard to="/app/options"     icon={Target}         label="Options"       desc="Options scanner, chain explorer, premarket picks" accent="violet"/>
          <QuickActionCard to="/app/how-to-trade" icon={BookOpen}      label="How To Trade"  desc="Playbook for every strategy in your library" accent="emerald"/>
        </div>
      </div>

      {/* GETTING STARTED */}
      <div>
        <SectionHeader title="Getting started"/>
        <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 divide-y divide-slate-100 dark:divide-slate-800 overflow-hidden">
          {[
            { step: '1', label: 'Create your first strategy',  link: '/app/strategies', done: (data?.strategy_count ?? 0) > 0 },
            { step: '2', label: 'Run a backtest',              link: '/app/backtests',  done: (data?.backtest_count ?? 0) > 0 },
            { step: '3', label: 'Start paper trading',         link: '/app/paper',      done: (data?.paper_trading.total_trades ?? 0) > 0 },
            { step: '4', label: 'Connect a broker account',    link: '/app/live',       done: (portfolio?.accounts_count ?? 0) > 0 },
            { step: '5', label: 'Configure position sizing',   link: '/app/live',       done: false },
          ].map(({ step, label, link, done }) => (
            <Link key={step} to={link} className="flex items-center gap-4 px-5 py-4 hover:bg-slate-50 dark:hover:bg-slate-800/40 transition-colors">
              <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0 ${
                done
                  ? 'bg-emerald-500 text-white'
                  : 'bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400 border border-slate-200 dark:border-slate-700'
              }`}>
                {done ? <CheckCircle2 size={14}/> : step}
              </div>
              <span className={`text-sm font-semibold flex-1 ${done ? 'text-slate-400 dark:text-slate-600 line-through' : 'text-slate-800 dark:text-slate-200'}`}>{label}</span>
              {!done && <ArrowUpRight size={14} className="text-slate-300 dark:text-slate-600"/>}
            </Link>
          ))}
        </div>
      </div>

    </div>
  )
}
