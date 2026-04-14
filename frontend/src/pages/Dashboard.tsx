import { useQuery } from '@tanstack/react-query'
import { dashboardApi } from '../api/endpoints'
import { useAuthStore } from '../stores/authStore'
import { Link } from 'react-router-dom'
import {
  TrendingUp, FlaskConical, PlayCircle, Zap,
  ArrowUpRight, ArrowDownRight, BarChart2, Activity,
} from 'lucide-react'

function StatCard({
  label, value, sub, trend, trendUp,
}: {
  label: string; value: string; sub?: string; trend?: string; trendUp?: boolean
}) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-5 shadow-sm">
      <div className="text-xs font-medium text-slate-400 uppercase tracking-wider mb-3">{label}</div>
      <div className="flex items-end justify-between">
        <div className="text-2xl font-extrabold text-slate-900">{value}</div>
        {trend && (
          <span className={`inline-flex items-center gap-0.5 text-xs font-semibold px-2 py-1 rounded-lg ${
            trendUp ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'
          }`}>
            {trendUp ? <ArrowUpRight size={12}/> : <ArrowDownRight size={12}/>}
            {trend}
          </span>
        )}
      </div>
      {sub && <div className="text-xs text-slate-400 mt-1">{sub}</div>}
    </div>
  )
}

function QuickAction({
  to, icon: Icon, label, desc, color,
}: {
  to: string; icon: any; label: string; desc: string; color: string
}) {
  return (
    <Link to={to}
      className="group bg-white rounded-xl border border-slate-200 p-5 hover:border-blue-300 hover:shadow-md transition-all duration-200 flex items-start gap-4">
      <div className={`w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0 ${color}`}>
        <Icon size={18}/>
      </div>
      <div>
        <div className="font-semibold text-slate-900 text-sm group-hover:text-blue-700 transition-colors">{label}</div>
        <div className="text-xs text-slate-400 mt-0.5 leading-relaxed">{desc}</div>
      </div>
    </Link>
  )
}

export default function Dashboard() {
  const { user } = useAuthStore()
  const { data, isLoading } = useQuery({
    queryKey: ['dashboard-summary'],
    queryFn: () => dashboardApi.summary().then((r) => r.data),
  })

  const fmt = (v: number) => {
    const sign = v >= 0 ? '+' : ''
    return `${sign}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`
  }

  const pnl   = data?.paper_trading.net_pnl ?? 0
  const lpnl  = data?.live_trading.net_pnl ?? 0

  return (
    <div className="p-8 max-w-6xl">
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-2xl font-extrabold text-slate-900">
          Good {new Date().getHours() < 12 ? 'morning' : new Date().getHours() < 17 ? 'afternoon' : 'evening'},{' '}
          <span className="text-blue-600">{user?.username}</span> 👋
        </h1>
        <p className="text-slate-500 text-sm mt-1">Here's an overview of your trading activity.</p>
      </div>

      {isLoading ? (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
          {[...Array(4)].map((_, i) => (
            <div key={i} className="bg-white rounded-xl border border-slate-200 p-5 h-24 animate-pulse"/>
          ))}
        </div>
      ) : (
        <>
          {/* Stats */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
            <StatCard label="Strategies" value={String(data?.strategy_count ?? 0)} sub="Total created"/>
            <StatCard label="Backtest Runs" value={String(data?.backtest_count ?? 0)} sub="Total executed"/>
            <StatCard
              label="Paper P&L"
              value={fmt(pnl)}
              sub={`${data?.paper_trading.total_trades ?? 0} trades · ${((data?.paper_trading.win_rate ?? 0) * 100).toFixed(1)}% WR`}
              trend={`${((data?.paper_trading.win_rate ?? 0) * 100).toFixed(1)}% WR`}
              trendUp={(data?.paper_trading.win_rate ?? 0) >= 0.5}
            />
            <StatCard
              label="Live P&L"
              value={fmt(lpnl)}
              sub={`${data?.live_trading.total_trades ?? 0} trades · ${((data?.live_trading.win_rate ?? 0) * 100).toFixed(1)}% WR`}
              trend={lpnl >= 0 ? 'Profit' : 'Loss'}
              trendUp={lpnl >= 0}
            />
          </div>
        </>
      )}

      {/* Trial banner */}
      {user?.subscription_tier === 'free_trial' && user?.trial_ends_at && (
        <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 mb-8 flex items-center justify-between">
          <div>
            <div className="font-semibold text-amber-800 text-sm">Free trial active</div>
            <div className="text-xs text-amber-600 mt-0.5">
              Expires {new Date(user.trial_ends_at).toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' })}
              {' '}· Upgrade to unlock live trading
            </div>
          </div>
          <Link to="/pricing" className="bg-amber-500 hover:bg-amber-600 text-white text-xs font-semibold px-4 py-2 rounded-lg transition-colors">
            View Plans
          </Link>
        </div>
      )}

      {/* Quick actions */}
      <h2 className="text-base font-bold text-slate-900 mb-4">Quick Actions</h2>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-10">
        <QuickAction to="/app/strategies"   icon={TrendingUp}  label="New Strategy"   desc="Build a rule-based trading strategy" color="bg-blue-50 text-blue-600"/>
        <QuickAction to="/app/backtests"    icon={FlaskConical} label="Run Backtest"   desc="Test on 2+ years of historical data"  color="bg-violet-50 text-violet-600"/>
        <QuickAction to="/app/paper"        icon={PlayCircle}   label="Paper Trade"    desc="Simulate with live market data"        color="bg-green-50 text-green-600"/>
        <QuickAction to="/app/live"         icon={Zap}          label="Go Live"        desc="Deploy to your Tradovate account"      color="bg-rose-50 text-rose-600"/>
      </div>

      {/* Getting started checklist */}
      <h2 className="text-base font-bold text-slate-900 mb-4">Getting Started</h2>
      <div className="bg-white rounded-xl border border-slate-200 divide-y divide-slate-100">
        {[
          { step: '1', label: 'Create your first strategy', link: '/app/strategies', done: (data?.strategy_count ?? 0) > 0 },
          { step: '2', label: 'Run a backtest',             link: '/app/backtests',   done: (data?.backtest_count ?? 0) > 0 },
          { step: '3', label: 'Start paper trading',        link: '/app/paper',       done: (data?.paper_trading.total_trades ?? 0) > 0 },
          { step: '4', label: 'Connect a broker account',   link: '/app/live',        done: false },
        ].map(({ step, label, link, done }) => (
          <Link key={step} to={link} className="flex items-center gap-4 px-5 py-4 hover:bg-slate-50 transition-colors">
            <div className={`w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0 ${
              done ? 'bg-blue-600 text-white' : 'bg-slate-100 text-slate-400'
            }`}>
              {done ? '✓' : step}
            </div>
            <span className={`text-sm font-medium ${done ? 'text-slate-400 line-through' : 'text-slate-700'}`}>{label}</span>
            {!done && <ArrowUpRight size={14} className="ml-auto text-slate-300"/>}
          </Link>
        ))}
      </div>
    </div>
  )
}
