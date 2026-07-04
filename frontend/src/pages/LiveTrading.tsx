import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { liveTradingApi, strategiesApi, tradesApi } from '../api/endpoints'
import { useState, useEffect } from 'react'
import LegalGate from '../components/LegalGate'
import { Zap, AlertTriangle, X, ShieldAlert, ServerCrash, Activity, Plus, Link2, Target } from 'lucide-react'
import CandlestickChart from '../components/CandlestickChart'
import RefreshButton from '../components/RefreshButton'
import ToggleSwitch from '../components/ToggleSwitch'
import { fmtEntryTime, fmtHold } from '../components/TradeMetrics'
import { TradeChartModal } from '../components/TradeChartModal'
import AcknowledgmentModal from '../components/AcknowledgmentModal'
import SizingModal from '../components/SizingModal'
import { LIVE_TRADING_CONSENT_TEXT } from '../utils/legalText'
import { classifyAssetClass, supportedClasses, type AssetClass } from '../utils/assetClass'

type Period = 'today' | 'month' | 'year' | 'all'
const PERIOD_LABELS: Record<Period, string> = { today: 'Today', month: 'Month', year: 'Year', all: 'All time' }
function filterByPeriod(t: any, period: Period): boolean {
  if (period === 'all') return true
  const ref = t.exit_time || t.entry_time
  if (!ref) return false
  const d = new Date(ref)
  const now = new Date()
  if (period === 'today') return d.toDateString() === now.toDateString()
  if (period === 'month') return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth()
  if (period === 'year')  return d.getFullYear() === now.getFullYear()
  return true
}
function ConsistencyProgress({ dailyPnl, dailyLimit }: { dailyPnl: number; dailyLimit: number }) {
  const pct = dailyLimit > 0 ? Math.min(Math.max(dailyPnl, 0) / dailyLimit * 100, 100) : 0
  const hit = dailyPnl >= dailyLimit
  return (
    <div className="mt-2">
      <div className="flex items-center justify-between text-[11px] mb-1">
        <span className="text-slate-500 dark:text-slate-400">Today</span>
        <span className={`font-semibold ${hit ? 'text-amber-600' : dailyPnl >= 0 ? 'text-green-600' : 'text-slate-600 dark:text-slate-300'}`}>
          {dailyPnl >= 0 ? '+' : ''}${dailyPnl.toFixed(2)}{' / '}${dailyLimit.toFixed(2)}
        </span>
      </div>
      <div className="h-1.5 rounded-full bg-slate-100 dark:bg-slate-700 overflow-hidden">
        <div className={`h-full transition-all ${hit ? 'bg-amber-500' : 'bg-green-500'}`} style={{ width: `${pct}%` }}/>
      </div>
    </div>
  )
}

function PeriodTabs({ value, onChange }: { value: Period; onChange: (v: Period) => void }) {
  const opts: Period[] = ['today', 'month', 'year', 'all']
  return (
    <div className="inline-flex rounded-lg bg-slate-100 dark:bg-slate-900 p-0.5 dark:bg-slate-800">
      {opts.map(o => (
        <button key={o} onClick={() => onChange(o)}
          className={`px-2 py-0.5 text-[10px] font-semibold rounded-md transition-colors ${ value === o ? 'bg-white dark:bg-slate-700 text-rose-600 dark:text-rose-300 shadow-sm' : 'text-slate-500 dark:text-slate-400 hover:text-slate-800 dark:hover:text-slate-200' }`}>
          {PERIOD_LABELS[o]}
        </button>
      ))}
    </div>
  )
}

export default function LiveTrading() {
  const qc = useQueryClient()
  const [tab, setTab] = useState<'accounts' | 'trades'>('accounts')
  const [showAddAccount, setShowAddAccount] = useState(false)
  const [showStartSession, setShowStartSession] = useState(false)
  const [accountForm, setAccountForm] = useState({
    account_name: '', is_demo: true,
    credentials: { username: '', password: '', app_id: '', cid: '', sec: '' },
  })
  const [sessionForm, setSessionForm] = useState({ strategy_id: '', broker_account_id: '', instrument: 'ES' })
  const [killConfirm, setKillConfirm] = useState<string | null>(null)
  const [chartTradeId, setChartTradeId] = useState<string | null>(null)
  const [selectedBroker, setSelectedBroker] = useState<string | null>(null)
  // Holds the account we're trying to switch from Sandbox → Live so the
  // consent modal knows what to flip on accept.
  const [goLiveConfirmAccount, setGoLiveConfirmAccount] = useState<any | null>(null)
  const [sizingAccount, setSizingAccount] = useState<any | null>(null)
  const [consistencyAccount, setConsistencyAccount] = useState<any | null>(null)
  const [consistencyForm, setConsistencyForm] = useState<{ profit_target: string; consistency_pct: number }>({ profit_target: '', consistency_pct: 50 })

  // Sync consistency form to whichever account modal opens
  useEffect(() => {
    if (consistencyAccount) {
      setConsistencyForm({
        profit_target: consistencyAccount.profit_target?.toString() ?? '',
        consistency_pct: consistencyAccount.consistency_pct ?? 50,
      })
    }
  }, [consistencyAccount?.id])

  const consistencyMutation = useMutation({
    mutationFn: ({ id, profit_target, consistency_pct }: { id: string; profit_target: number | null; consistency_pct: number | null }) =>
      liveTradingApi.setConsistency(id, profit_target, consistency_pct),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['broker-accounts'] })
      setConsistencyAccount(null)
    },
  })

  const { data: accounts = [] }   = useQuery({ queryKey: ['broker-accounts'], queryFn: () => liveTradingApi.listAccounts().then(r => r.data) })
  const { data: strategies = [] } = useQuery({ queryKey: ["strategies"], queryFn: () => strategiesApi.list().then(r => r.data), staleTime: 30000, refetchOnMount: "always" })
  // Live sessions — used by the Deploy Strategy selector to flag strategies
  // that already have a running session with " · Active".
  const { data: liveSessions = [] } = useQuery({ queryKey: ['live-sessions'], queryFn: () => (liveTradingApi as any).listSessions().then((r: any) => r.data), refetchInterval: 30000 })
  const { data: liveTrades = [] } = useQuery({ queryKey: ['live-trades'],     queryFn: () => tradesApi.list({ mode: 'live', limit: 1000 }).then(r => r.data) })
  const { data: liveChartData }   = useQuery({ queryKey: ['live-chart'],      queryFn: () => tradesApi.getChartData('live', 'ES').then(r => r.data), refetchInterval: 30000 })

  const [accountError, setAccountError] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null)

  const addAccountMutation = useMutation({
    mutationFn: () => liveTradingApi.addAccount({ ...accountForm, broker: selectedBroker || 'tradovate' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['broker-accounts'] })
      setShowAddAccount(false)
      setAccountError(null)
      setTestResult(null)
    },
    onError: (e: any) => setAccountError(e?.response?.data?.detail || 'Failed to add account.'),
  })

  const testConnectionMutation = useMutation({
    mutationFn: () => liveTradingApi.testConnection({
      broker: selectedBroker || 'tradovate',
      is_demo: accountForm.is_demo,
      credentials: accountForm.credentials,
    }),
    onSuccess: (r: any) => {
      const env = r.data.environment === 'demo' ? (selectedBroker === 'tradier' ? '(Sandbox)' : '(Demo)') : '(Live)'
      const brokerName = (BROKERS.find(b => b.slug === selectedBroker)?.name) || 'broker'
      setTestResult({ ok: true, msg: `Connected to ${brokerName} ${env}.` })
    },
    onError: (e: any) => setTestResult({ ok: false, msg: e?.response?.data?.detail || 'Connection test failed.' }),
  })

  const startSessionMutation = useMutation({
    mutationFn: () => liveTradingApi.startSession(sessionForm),
    onSuccess: () => { qc.invalidateQueries({}); setShowStartSession(false) },
  })

  const killMutation = useMutation({
    mutationFn: (id: string) => liveTradingApi.killSwitch(id),
    onSuccess: () => { qc.invalidateQueries({}); setKillConfirm(null) },
  })

  // When the user flips on Trading for a non-sandbox (live) account, we
  // gate the mutation behind the risk + consent acks. `pendingEnable` holds
  // the account id while the gate runs; on completion we fire the mutation.
  const [pendingEnable, setPendingEnable] = useState<string | null>(null)

    const tradingEnabledMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      liveTradingApi.setTradingEnabled(id, enabled),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['broker-accounts'] }) },
  })

  const sandboxModeMutation = useMutation({
    mutationFn: ({ id, sandbox_mode }: { id: string; sandbox_mode: boolean }) =>
      (liveTradingApi as any).setSandboxMode(id, sandbox_mode),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['broker-accounts'] }) },
  })

  const activeAccount = accounts.find((a: any) => a.is_active)

  // P&L stats with period filter (mirrors Paper Trading)
  const [pnlPeriod, setPnlPeriod] = useState<Period>('all')
  const closedLive = liveTrades.filter((t: any) => t.status === 'closed')
  const periodLive = closedLive.filter((t: any) => filterByPeriod(t, pnlPeriod))
  const totalPnl   = periodLive.reduce((acc: number, t: any) => acc + (t.net_pnl ?? 0), 0)
  const wins       = periodLive.filter((t: any) => (t.net_pnl ?? 0) > 0).length
  const winRate    = periodLive.length > 0 ? (wins / periodLive.length * 100).toFixed(1) : '—'

  return (
    <div className="p-8 max-w-6xl">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-extrabold text-slate-900 dark:text-slate-100">Live Trading</h1>
          <p className="text-slate-500 text-sm mt-1 dark:text-slate-400">Brokerage-account auto-trading (Webull, Tradier, etc) · prop-firm accounts use Account Signals</p>
        </div>
        <div className="flex gap-2.5">
          <RefreshButton onClick={() => qc.invalidateQueries()}/>
          <button onClick={() => setShowAddAccount(true)}
            className="flex items-center gap-2 border border-slate-200 text-slate-700 hover:bg-slate-100 px-4 py-2.5 rounded-xl text-sm font-medium transition-colors dark:text-slate-200 dark:border-slate-700 dark:hover:bg-slate-800">
            <Plus size={14}/> Add Account
          </button>
          <button onClick={() => setShowStartSession(true)}
            className="flex items-center gap-2 bg-rose-600 hover:bg-rose-700 text-white px-4 py-2.5 rounded-xl text-sm font-semibold transition-colors shadow-sm shadow-rose-100">
            <Zap size={14}/> Deploy Strategy
          </button>
        </div>
      </div>

      {/* QQQ-as-Nasdaq explainer — points users to the right product for their account type */}
      <div className="rounded-xl border border-blue-200 dark:border-blue-800/50 bg-blue-50/60 dark:bg-blue-900/20 p-4 text-sm mb-4">
        <div className="font-bold text-slate-900 dark:text-slate-100 mb-1">Want fully-automated Nasdaq exposure on a brokerage account?</div>
        <p className="text-slate-700 dark:text-slate-300 leading-relaxed">
          Most futures prop firms ban algos. The workaround: trade <strong>QQQ options</strong> on a regular brokerage like <Link to="/app/options" className="text-blue-600 underline">Webull or Tradier</Link>. QQQ tracks the Nasdaq-100 — the same index NQ futures track — so a bullish/bearish NQ setup is also a bullish/bearish QQQ setup. Once the options engine ships, the bot can auto-trade those for you on a brokerage where automation is allowed.
        </p>
      </div>

      {/* Warning banner */}
      <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 rounded-xl p-4 mb-6 flex items-start gap-3">
        <AlertTriangle size={16} className="text-amber-500 flex-shrink-0 mt-0.5"/>
        <p className="text-xs text-amber-700 leading-relaxed">
          <span className="font-semibold">Live trading uses real money.</span> Start every account in <strong>Sandbox Mode</strong> until the bot has fired several signals you agree with — only then flip it to Live. Always set a daily loss limit and monitor positions. Futures and options trading carry substantial risk of loss, and options can expire worthless.
        </p>
      </div>

      {/* Stats bar */}
      <div className="flex items-center justify-between mb-2">
        <span className="text-[11px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">
          Showing: {PERIOD_LABELS[pnlPeriod]}
        </span>
        <PeriodTabs value={pnlPeriod} onChange={setPnlPeriod}/>
      </div>
      <div className="grid grid-cols-3 gap-4 mb-6">
        <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 dark:bg-slate-900 dark:border-slate-700">
          <div className="text-xs text-slate-400 uppercase tracking-wider font-medium mb-1.5 dark:text-slate-500">Total Trades</div>
          <div className="text-2xl font-extrabold text-slate-900 dark:text-slate-100">{periodLive.length}</div>
          {periodLive.length === 0 && (
            <div className="text-[11px] text-slate-400 dark:text-slate-500 mt-1">No closed trades in {PERIOD_LABELS[pnlPeriod].toLowerCase()}</div>
          )}
        </div>
        <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 dark:bg-slate-900 dark:border-slate-700">
          <div className="text-xs text-slate-400 uppercase tracking-wider font-medium mb-1.5 dark:text-slate-500">Win Rate</div>
          <div className={`text-2xl font-extrabold ${periodLive.length > 0 && wins/periodLive.length >= 0.5 ? 'text-green-600' : periodLive.length === 0 ? 'text-slate-300 dark:text-slate-600' : 'text-slate-900 dark:text-slate-100'}`}>{periodLive.length === 0 ? '—' : `${winRate}%`}</div>
          {periodLive.length > 0 && (
            <div className="text-[11px] text-slate-400 dark:text-slate-500 mt-1">{wins}W / {periodLive.length - wins}L</div>
          )}
        </div>
        <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 dark:bg-slate-900 dark:border-slate-700">
          <div className="text-xs text-slate-400 uppercase tracking-wider font-medium mb-1.5 dark:text-slate-500">Net P&L</div>
          <div className={`text-2xl font-extrabold ${periodLive.length === 0 ? 'text-slate-300 dark:text-slate-600' : totalPnl >= 0 ? 'text-green-600' : 'text-red-500'}`}>
            {totalPnl >= 0 ? '+' : ''}${totalPnl.toLocaleString('en-US', { minimumFractionDigits: 2 })}
          </div>
        </div>
      </div>

      {/* Active connection indicator */}
      {activeAccount && (
        <div className="bg-green-50 dark:bg-green-900/20 border border-green-200 rounded-xl p-4 mb-6 flex items-center gap-3">
          <div className="w-2.5 h-2.5 rounded-full bg-green-500 animate-pulse flex-shrink-0"/>
          <div>
            <div className="font-semibold text-green-800 text-sm">Connected to {activeAccount.account_name}</div>
            <div className="text-xs text-green-600 mt-0.5">
              {activeAccount.broker} · {activeAccount.is_demo ? 'Demo environment' : 'Live environment'}
            </div>
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-1 border-b border-slate-200 mb-6 dark:border-slate-700">
        {(['accounts', 'trades'] as const).map(t => (
          <button key={t} onClick={() => setTab(t)}
            className={`px-4 py-2.5 text-sm font-semibold capitalize border-b-2 transition-colors ${ tab === t ? 'border-blue-600 text-blue-600' : 'border-transparent text-slate-500 hover:text-slate-800' } dark:text-slate-400`}>
            {t === 'accounts' ? 'Broker Accounts' : 'Trade History'}
          </button>
        ))}
      </div>

      {/* Price Chart */}
      {liveChartData && liveChartData.candles && liveChartData.candles.length > 0 && (
        <div className="mb-6">
          <h2 className="text-base font-bold text-slate-900 mb-3 dark:text-slate-100">Price Chart & Trades</h2>
          <CandlestickChart candles={liveChartData.candles} markers={liveChartData.markers || []} height={400} />
        </div>
      )}

      {/* Accounts tab */}
      {tab === 'accounts' && (
        <div className="space-y-3">
          {accounts.length === 0 ? (
            <div className="bg-slate-50 rounded-2xl border border-dashed border-slate-200 p-14 text-center dark:bg-slate-900 dark:border-slate-700">
              <div className="w-14 h-14 bg-rose-50 rounded-2xl flex items-center justify-center mx-auto mb-5">
                <Link2 size={24} className="text-rose-500"/>
              </div>
              <p className="font-semibold text-slate-700 mb-1 dark:text-slate-200">No broker accounts connected</p>
              <p className="text-sm text-slate-400 mb-5 dark:text-slate-500">Connect your Tradovate account to start live trading</p>
              <button onClick={() => setShowAddAccount(true)}
                className="inline-flex items-center gap-2 bg-rose-600 hover:bg-rose-700 text-white px-4 py-2.5 rounded-xl text-sm font-semibold transition-colors">
                <Plus size={14}/> Connect Account
              </button>
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
              {accounts.map((a: any) => (
                <Link
                  key={a.id}
                  to={`/app/live/${a.id}`}
                  className="group rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-4 hover:shadow-md hover:-translate-y-0.5 hover:border-rose-300 transition-all"
                >
                  <div className="flex items-start justify-between gap-2 mb-2">
                    <div className="flex items-start gap-2.5 min-w-0">
                      <div className="w-9 h-9 bg-rose-50 dark:bg-rose-900/20 rounded-lg flex items-center justify-center flex-shrink-0">
                        <Zap size={16} className="text-rose-600 dark:text-rose-400"/>
                      </div>
                      <div className="min-w-0">
                        <div className="font-bold text-sm truncate text-slate-900 dark:text-slate-100 group-hover:text-rose-600 transition-colors">
                          {a.account_name}
                        </div>
                        <div className="text-[11px] text-slate-500 dark:text-slate-400 truncate mt-0.5">
                          {a.broker} · {a.is_demo ? 'Demo' : 'Live'}
                        </div>
                      </div>
                    </div>
                    <span className={`badge ${a.consistency_locked_at ? 'badge-amber' : a.is_active ? 'badge-green' : 'badge-grey'} flex-shrink-0`}>
                      {a.consistency_locked_at ? 'Locked' : a.is_active ? 'Connected' : 'Inactive'}
                    </span>
                  </div>

                  {a.daily_limit != null && (
                    <ConsistencyProgress dailyPnl={a.daily_pnl ?? 0} dailyLimit={a.daily_limit}/>
                  )}

                  {/* Sandbox / Live toggle — bot will SIMULATE trades when sandbox is on */}
                  <div className={`mt-3 px-3 py-2 rounded-lg text-[11px] flex items-center justify-between ${a.sandbox_mode ? 'bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800' : 'bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800'}`} onClick={(e) => e.preventDefault()}>
                    <div className="min-w-0">
                      <div className={`font-bold ${a.sandbox_mode ? 'text-amber-700 dark:text-amber-300' : 'text-green-700 dark:text-green-300'}`}>
                        {a.sandbox_mode ? '🧪 SANDBOX — simulated only' : '🔴 LIVE — real money'}
                      </div>
                      <div className="text-[10px] text-slate-500 dark:text-slate-400 mt-0.5">
                        {a.sandbox_mode ? 'Bot logs trades but does not place orders. Verify behavior before going live.' : 'Orders route to broker. Real P&L.'}
                      </div>
                    </div>
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        // Going from Sandbox (true) → Live (false) requires the consent modal
                        if (a.sandbox_mode) {
                          setGoLiveConfirmAccount(a)
                        } else {
                          // Going back to Sandbox is always allowed without consent
                          sandboxModeMutation.mutate({ id: a.id, sandbox_mode: true })
                        }
                      }}
                      disabled={sandboxModeMutation.isPending}
                      className={`ml-2 px-2.5 py-1 rounded-md text-[10px] font-bold uppercase tracking-wider whitespace-nowrap ${a.sandbox_mode ? 'bg-green-600 hover:bg-green-700 text-white' : 'bg-amber-500 hover:bg-amber-600 text-white'}`}>
                      {a.sandbox_mode ? 'Go Live' : 'Sandbox'}
                    </button>
                  </div>

                  {a.is_active && (
                    <div className="flex items-center justify-between mt-3 pt-3 border-t border-slate-100 dark:border-slate-700 dark:border-slate-800" onClick={(e) => e.preventDefault()}>
                      <ToggleSwitch
                        label={a.trading_enabled ? 'Trading' : 'Paused'}
                        checked={!!a.trading_enabled}
                        disabled={tradingEnabledMutation.isPending}
                        onChange={(next) => {
                          // Off is always allowed (safety release). Sandbox accounts also
                          // skip the gate. Otherwise route through the legal gate first.
                          if (!next || a.sandbox_mode) {
                            tradingEnabledMutation.mutate({ id: a.id, enabled: next })
                          } else {
                            setPendingEnable(a.id)
                          }
                        }}
                      />
                      <div className="flex items-center gap-1.5">
                        <button
                          onClick={(e) => { e.preventDefault(); e.stopPropagation(); setConsistencyAccount(a) }}
                          title="Set consistency rule"
                          className="flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] font-semibold border border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-700 transition-colors">
                          <Target size={11}/> {a.daily_limit != null ? `${a.consistency_pct?.toFixed(0)}%` : 'Consistency'}
                        </button>
                        <button
                          onClick={(e) => { e.preventDefault(); e.stopPropagation(); setKillConfirm(a.id) }}
                          className="flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] font-semibold border border-red-200 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors">
                          <ShieldAlert size={11}/> Kill
                        </button>
                      </div>
                    </div>
                  )}
                </Link>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Trades tab */}
      {tab === 'trades' && (
        <div className="bg-slate-50 rounded-2xl border border-slate-200 overflow-hidden shadow-sm dark:bg-slate-900 dark:border-slate-700">
          {liveTrades.length === 0 ? (
            <div className="p-14 text-center">
              <Activity size={32} className="mx-auto text-slate-200 mb-3"/>
              <p className="text-sm font-medium text-slate-400 dark:text-slate-500">No live trades yet</p>
              <p className="text-xs text-slate-300 mt-1 dark:text-slate-600">Deploy a strategy to begin live execution</p>
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-slate-100 border-b border-slate-200 dark:bg-slate-800 dark:border-slate-700">
                  {['Entered', 'Hold', 'Instrument', 'Direction', 'Entry', 'Exit', 'Stop Loss', 'Take Profit', 'Net P&L', 'Exit Reason', 'Status', 'Chart'].map(h => (
                    <th key={h} className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider whitespace-nowrap dark:text-slate-400">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {liveTrades.map((t: any) => (
                  <tr key={t.id} className="hover:bg-slate-100 transition-colors dark:hover:bg-slate-800">
                    <td className="px-4 py-3.5 text-slate-500 dark:text-slate-400 whitespace-nowrap">{fmtEntryTime(t.entry_time)}</td>
                    <td className="px-4 py-3.5 text-slate-500 dark:text-slate-400 whitespace-nowrap">{fmtHold(t.entry_time, t.exit_time)}</td>
                    <td className="px-4 py-3.5 font-semibold text-slate-900 dark:text-slate-100">{t.instrument}</td>
                    <td className="px-4 py-3.5">
                      <span className={`badge ${t.direction === 'long' ? 'badge-green' : 'badge-red'}`}>
                        {t.direction.toUpperCase()}
                      </span>
                    </td>
                    <td className="px-4 py-3.5 text-slate-600 font-medium dark:text-slate-300">{t.entry_price?.toFixed(2) ?? '—'}</td>
                    <td className="px-4 py-3.5 text-slate-600 font-medium dark:text-slate-300">{t.exit_price?.toFixed(2) ?? 'Open'}</td>
                    <td className="px-4 py-3.5 text-slate-400 dark:text-slate-500">{t.stop_loss?.toFixed(2) ?? '—'}</td>
                    <td className="px-4 py-3.5 text-slate-400 dark:text-slate-500">{t.take_profit?.toFixed(2) ?? '—'}</td>
                    <td className={`px-4 py-3.5 font-bold ${(t.net_pnl ?? 0) >= 0 ? 'text-green-600' : 'text-red-500'}`}>
                      {t.net_pnl != null ? `${t.net_pnl >= 0 ? '+' : ''}$${t.net_pnl.toFixed(2)}` : '—'}
                    </td>
                    <td className="px-4 py-3.5 text-slate-400 text-xs dark:text-slate-500">{t.exit_reason ?? '—'}</td>
                    <td className="px-4 py-3.5">
                      <span className={`badge ${t.status === 'closed' ? 'badge-grey' : t.status === 'open' ? 'badge-blue' : 'badge-amber'}`}>
                        {t.status}
                      </span>
                    </td>
                    <td className="px-4 py-3.5">
                      <button
                        onClick={() => setChartTradeId(t.id)}
                        className="text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300 text-xs font-semibold underline"
                      >
                        View
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
      {chartTradeId && <TradeChartModal tradeId={chartTradeId} onClose={() => setChartTradeId(null)} />}

      {/* Consistency rule modal */}
      {consistencyAccount && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-white dark:bg-slate-800 rounded-2xl shadow-2xl w-full max-w-sm">
            <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100 dark:border-slate-700">
              <h2 className="text-base font-bold text-slate-900 dark:text-slate-100">Consistency Rule</h2>
              <button onClick={() => setConsistencyAccount(null)} className="p-1.5 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-700 text-slate-400">
                <X size={16}/>
              </button>
            </div>
            <div className="px-6 py-5 space-y-4">
              <p className="text-xs text-slate-500 dark:text-slate-400 leading-relaxed">
                Auto-pause this account once today's profit hits the cap. Useful for prop-firm consistency rules (Apex, Topstep). You'll get an email when it triggers and the account stays off until you toggle it back on.
              </p>
              <div>
                <label className="text-xs font-semibold text-slate-600 dark:text-slate-300 uppercase tracking-wider block mb-1.5">Profit target ($)</label>
                <input
                  type="number"
                  value={consistencyForm.profit_target}
                  onChange={(e) => setConsistencyForm(f => ({ ...f, profit_target: e.target.value }))}
                  placeholder="e.g. 3000"
                  className="w-full border border-slate-300 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-600 dark:text-slate-300 uppercase tracking-wider block mb-1.5">Daily cap (% of target)</label>
                <div className="grid grid-cols-5 gap-1.5">
                  {[20, 30, 40, 50, 60].map(p => (
                    <button
                      key={p}
                      type="button"
                      onClick={() => setConsistencyForm(f => ({ ...f, consistency_pct: p }))}
                      className={`px-2 py-2 rounded-lg text-xs font-semibold border transition-colors ${
                        consistencyForm.consistency_pct === p
                          ? 'bg-blue-600 text-white border-blue-600'
                          : 'bg-white dark:bg-slate-900 text-slate-600 dark:text-slate-300 border-slate-200 dark:border-slate-700 hover:border-blue-300'
                      }`}
                    >
                      {p}%
                    </button>
                  ))}
                </div>
                {consistencyForm.profit_target && Number(consistencyForm.profit_target) > 0 && (
                  <p className="text-[11px] text-slate-500 dark:text-slate-400 mt-2">
                    Daily cap = ${(Number(consistencyForm.profit_target) * consistencyForm.consistency_pct / 100).toFixed(2)}
                  </p>
                )}
              </div>
            </div>
            <div className="flex gap-2 px-6 py-4 border-t border-slate-100 dark:border-slate-700">
              <button
                onClick={() => consistencyMutation.mutate({ id: consistencyAccount.id, profit_target: null, consistency_pct: null })}
                disabled={consistencyMutation.isPending}
                className="flex-1 border border-slate-200 dark:border-slate-700 text-slate-500 dark:text-slate-400 py-2.5 rounded-xl text-sm font-medium hover:bg-slate-50 dark:hover:bg-slate-800">
                Clear rule
              </button>
              <button
                onClick={() => {
                  const pt = parseFloat(consistencyForm.profit_target)
                  if (!pt || pt <= 0) return
                  consistencyMutation.mutate({ id: consistencyAccount.id, profit_target: pt, consistency_pct: consistencyForm.consistency_pct })
                }}
                disabled={consistencyMutation.isPending || !parseFloat(consistencyForm.profit_target)}
                className="flex-1 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white py-2.5 rounded-xl text-sm font-semibold">
                {consistencyMutation.isPending ? 'Saving…' : 'Save rule'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Kill switch confirm modal */}
      {killConfirm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-slate-50 rounded-2xl shadow-2xl w-full max-w-sm dark:bg-slate-900">
            <div className="p-6 text-center">
              <div className="w-14 h-14 bg-red-50 dark:bg-red-900/20 rounded-2xl flex items-center justify-center mx-auto mb-4">
                <ServerCrash size={26} className="text-red-500"/>
              </div>
              <h2 className="text-lg font-extrabold text-slate-900 mb-2 dark:text-slate-100">Trigger Kill Switch?</h2>
              <p className="text-sm text-slate-500 mb-6 leading-relaxed dark:text-slate-400">
                This will immediately halt all trading activity, cancel all open orders, and close any open positions for this session.
              </p>
              <div className="flex gap-3">
                <button onClick={() => setKillConfirm(null)} className="flex-1 border border-slate-200 text-slate-600 py-2.5 rounded-xl text-sm font-medium dark:text-slate-300 dark:border-slate-700">Cancel</button>
                <button onClick={() => killMutation.mutate(killConfirm)} disabled={killMutation.isPending}
                  className="flex-1 bg-red-600 hover:bg-red-700 disabled:opacity-50 text-white py-2.5 rounded-xl text-sm font-bold transition-colors">
                  {killMutation.isPending ? 'Stopping...' : 'Confirm Kill Switch'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Add account modal — TradingView-style broker picker */}
      {showAddAccount && (
        <BrokerConnectModal
          onClose={() => { setShowAddAccount(false); setAccountError(null); setTestResult(null); setSelectedBroker(null) }}
          selectedBroker={selectedBroker}
          setSelectedBroker={setSelectedBroker}
          accountForm={accountForm}
          setAccountForm={setAccountForm}
          testResult={testResult}
          setTestResult={setTestResult}
          accountError={accountError}
          setAccountError={setAccountError}
          onTest={() => testConnectionMutation.mutate()}
          onConnect={() => addAccountMutation.mutate()}
          testing={testConnectionMutation.isPending}
          connecting={addAccountMutation.isPending}
        />
      )}

      {/* Deploy strategy modal */}
      {showStartSession && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-slate-50 rounded-2xl shadow-2xl w-full max-w-sm dark:bg-slate-900">
            <div className="flex items-center justify-between px-6 py-5 border-b border-slate-100 dark:border-slate-800">
              <h2 className="text-base font-bold text-slate-900 dark:text-slate-100">Deploy Strategy Live</h2>
              <button onClick={() => setShowStartSession(false)} className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400 dark:text-slate-500 dark:hover:bg-slate-800"><X size={16}/></button>
            </div>
            <div className="px-6 py-5 space-y-4">
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Strategy</label>
                <select value={sessionForm.strategy_id} onChange={e => setSessionForm({...sessionForm, strategy_id: e.target.value})}
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700">
                  {(() => {
                    // Mirrors the LiveTradingV2 filter so the classic and
                    // v2 pages agree on which strategies are deployable
                    // to which broker. Skips template strategies (empty
                    // instruments → classifyAssetClass returns 'unknown')
                    // and adds Theta Scanner under Stocks when the broker
                    // is stock-capable.
                    const acct = accounts.find((a: any) => a.id === sessionForm.broker_account_id)
                    if (!sessionForm.broker_account_id) {
                      return <option value="">Select a broker account first to see compatible strategies.</option>
                    }
                    if (!acct) {
                      return <option value="">Loading account info…</option>
                    }
                    const accountClasses = supportedClasses(acct.broker) as ReadonlyArray<AssetClass>
                    const cls = (st: any): AssetClass => (st.asset_class as AssetClass) || classifyAssetClass(st.instruments || [])
                    // Strategy ids with a running live session → flagged " · Active".
                    const runningIds = new Set<string>(
                      liveSessions.filter((s: any) => s.is_active).map((s: any) => String(s.strategy_id))
                    )
                    const active = strategies.filter((st: any) => (st.status || "").toLowerCase() === "active")
                    const byClass: Record<AssetClass, any[]> = { futures: [], options: [], stock: [], unknown: [] }
                    for (const st of active) {
                      const c = cls(st)
                      if (accountClasses.includes(c)) byClass[c].push(st)
                    }
                    if (accountClasses.includes("stock")) {
                      byClass.stock = [{ id: "theta_scanner", name: "🎯 Saro — daily premarket pick (built-in)" }, ...byClass.stock]
                    }
                    const groups = [
                      { label: "Futures", emoji: "⚡", items: byClass.futures },
                      { label: "Options", emoji: "🎯", items: byClass.options },
                      { label: "Stocks",  emoji: "📈", items: byClass.stock },
                    ].filter(g => g.items.length > 0)
                    const total = groups.reduce((s, g) => s + g.items.length, 0)
                    // Strategies whose asset class the broker supports but that
                    // are draft/paused: not deployable live (live requires
                    // active), but shown DISABLED with a reason instead of being
                    // silently dropped.
                    const ineligible = strategies.filter((st: any) =>
                      accountClasses.includes(cls(st))
                      && ["draft", "paused"].includes((st.status || "").toLowerCase())
                    )
                    if (total === 0 && ineligible.length === 0) {
                      return <option value="">No compatible strategies for {acct.broker} — create one at /app/strategies.</option>
                    }
                    return (
                      <>
                        <option value="">Select a strategy… ({total} available for {acct.broker})</option>
                        {groups.map(g => (
                          <optgroup key={g.label} label={`${g.emoji} ${g.label}`}>
                            {g.items.map((st: any) => {
                              const running = runningIds.has(String(st.id))
                              return (
                                <option key={st.id} value={st.id}>
                                  {st.name}{running ? ' · Active 🟢' : ''}
                                </option>
                              )
                            })}
                          </optgroup>
                        ))}
                        {ineligible.length > 0 && (
                          <optgroup label="Not deployable (activate first)">
                            {ineligible.map((st: any) => {
                              const status = (st.status || "").toLowerCase()
                              return (
                                <option key={st.id} value={st.id} disabled
                                  title={`${status} — set this strategy to Active on the Strategies page before deploying live`}>
                                  {st.name} — {status}, activate first
                                </option>
                              )
                            })}
                          </optgroup>
                        )}
                      </>
                    )
                  })()}
                </select>
                {strategies.length === 0 && (
                  <p className="text-[11px] text-amber-600 dark:text-amber-400 mt-1.5">
                    No strategies yet — <Link to="/app/strategies" className="underline font-semibold">create one here</Link>.
                  </p>
                )}
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Broker Account</label>
                <select value={sessionForm.broker_account_id} onChange={e => setSessionForm({...sessionForm, broker_account_id: e.target.value})}
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700">
                  <option value="">Select an account...</option>
                  {accounts.map((a: any) => <option key={a.id} value={a.id}>{a.account_name}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Instrument</label>
                <select value={sessionForm.instrument} onChange={e => setSessionForm({...sessionForm, instrument: e.target.value})}
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700">
                  {['ES', 'NQ', 'RTY', 'YM'].map(i => <option key={i}>{i}</option>)}
                </select>
              </div>
              <div className="bg-red-50 dark:bg-red-900/20 border border-red-100 rounded-xl p-3 text-xs text-red-700">
                <span className="font-semibold">Warning:</span> This will execute real trades with real money. Confirm your strategy is fully tested.
              </div>
            </div>
            <div className="flex gap-3 px-6 py-4 border-t border-slate-100 dark:border-slate-800">
              <button onClick={() => setShowStartSession(false)} className="flex-1 border border-slate-200 text-slate-600 py-2.5 rounded-xl text-sm font-medium dark:text-slate-300 dark:border-slate-700">Cancel</button>
              <button onClick={() => startSessionMutation.mutate()} disabled={!sessionForm.strategy_id || !sessionForm.broker_account_id || startSessionMutation.isPending}
                className="flex-1 bg-rose-600 hover:bg-rose-700 disabled:opacity-50 text-white py-2.5 rounded-xl text-sm font-semibold transition-colors">
                {startSessionMutation.isPending ? 'Deploying...' : 'Deploy Live'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Liability disclosure */}
      <p className="mt-12 text-[11px] leading-relaxed text-slate-400 dark:text-slate-500 italic max-w-3xl">
        Theta Algos LLC provides software tools and execution-support
        infrastructure only. We are not a registered investment adviser, broker-dealer,
        or futures commission merchant, and nothing on this platform constitutes
        investment advice or a recommendation to trade any security, futures contract,
        options contract, or currency. Past performance, backtest results, simulated
        results, and paper-trading metrics are not indicative of future returns.
        Trading futures and options involves substantial risk of loss and is not
        suitable for every investor — options can expire worthless, resulting in a
        100% loss of premium paid. Forex trading (coming soon) carries similar risk.
        Most prop firms prohibit automated trading; rules change frequently and
        violations may result in account closure with no payout — verifying compliance
        with your firm is your responsibility. By using this platform you acknowledge
        that you alone are responsible for your trading decisions and any resulting
        profits or losses, and that Theta Algos LLC, its affiliates, and
        its operators expressly disclaim any liability for losses incurred in
        connection with the use of this software.
      </p>

      {/* Sandbox → Live consent modal */}
      {sizingAccount && (
        <SizingModal account={sizingAccount} onClose={() => setSizingAccount(null)}/>
      )}

      {goLiveConfirmAccount && (
        <AcknowledgmentModal
          kind="live_trading_consent"
          title={`Switch ${goLiveConfirmAccount.account_name} to LIVE mode?`}
          body={LIVE_TRADING_CONSENT_TEXT}
          requireScroll={true}
          detail={`Account ${goLiveConfirmAccount.account_name} (${goLiveConfirmAccount.broker})`}
          acceptLabel="I understand — switch to Live"
          declineLabel="Keep in Sandbox"
          onDecline={() => setGoLiveConfirmAccount(null)}
          onAccept={() => {
            sandboxModeMutation.mutate({ id: goLiveConfirmAccount.id, sandbox_mode: false })
            setGoLiveConfirmAccount(null)
          }}
        />
      )}

      {pendingEnable && (
        <LegalGate
          kinds={['risk_disclosure', 'live_trading_consent']}
          onComplete={() => {
            tradingEnabledMutation.mutate({ id: pendingEnable, enabled: true })
            setPendingEnable(null)
          }}
          onCancel={() => setPendingEnable(null)}
        />
      )}
    </div>
  )
}


// ─── Broker connect modal — TradingView-style picker ─────────────────────

type BrokerStatus = 'available' | 'pending_broker' | 'pending_assets'
type BrokerCategory = 'futures' | 'multi_asset'
type CredField = { key: string; label: string; type: 'text' | 'password'; hint: string }

const BROKERS: {
  slug: string
  name: string
  tagline: string
  domain: string  // used for Clearbit logo lookup (https://logo.clearbit.com/<domain>)
  initials: string
  accent: string  // fallback tile color if the logo image 404s
  status: BrokerStatus
  category: BrokerCategory
  helpUrl?: string
  fields?: CredField[]
}[] = [
  // ── Futures brokers (the platform's core asset class) ────────────────
  {
    slug: 'tradovate', name: 'Tradovate',
    tagline: 'Futures · CME · 24/5',
    domain: 'tradovate.com', initials: 'TV', accent: 'bg-blue-600', status: 'available',
    category: 'futures',
    helpUrl: 'https://api.tradovate.com',
    fields: [
      { key: 'username', label: 'Username',  type: 'text',     hint: 'Email/username you use to log into Tradovate.' },
      { key: 'password', label: 'Password',  type: 'password', hint: 'Your Tradovate account password.' },
      { key: 'app_id',   label: 'App ID',    type: 'text',     hint: 'A label of your choice — e.g. "Edge".' },
      { key: 'cid',      label: 'CID',       type: 'text',     hint: 'API Key Client ID — generated in Tradovate.' },
      { key: 'sec',      label: 'Secret',    type: 'password', hint: 'API Key Secret — shown once at key creation.' },
    ],
  },
  {
    slug: 'tradestation', name: 'TradeStation',
    tagline: 'Futures · Stocks · OAuth login',
    domain: 'tradestation.com', initials: 'TS', accent: 'bg-emerald-600', status: 'pending_broker',
    category: 'futures', helpUrl: 'https://api.tradestation.com',
  },
  {
    slug: 'ibkr', name: 'Interactive Brokers',
    tagline: 'Multi-asset · Global · Client Portal API',
    domain: 'interactivebrokers.com', initials: 'IB', accent: 'bg-rose-700', status: 'pending_broker',
    category: 'futures', helpUrl: 'https://www.interactivebrokers.com/en/trading/ib-api.php',
  },
  {
    slug: 'rithmic', name: 'AMP / Rithmic',
    tagline: 'Futures · Direct CME via R | API',
    domain: 'ampfutures.com', initials: 'AR', accent: 'bg-amber-600', status: 'pending_broker',
    category: 'futures', helpUrl: 'https://yyy3.rithmic.com',
  },
  {
    slug: 'optimus', name: 'Optimus Futures',
    tagline: 'Futures · Rithmic / CQG access',
    domain: 'optimusfutures.com', initials: 'OF', accent: 'bg-violet-600', status: 'pending_broker',
    category: 'futures',
  },
  {
    slug: 'cqg', name: 'CQG',
    tagline: 'Futures · Direct exchange API',
    domain: 'cqg.com', initials: 'CQ', accent: 'bg-fuchsia-600', status: 'pending_broker',
    category: 'futures',
  },
  {
    slug: 'stonex', name: 'StoneX',
    tagline: 'Futures · Institutional-grade',
    domain: 'stonex.com', initials: 'SX', accent: 'bg-indigo-600', status: 'pending_broker',
    category: 'futures',
  },

  // ── Multi-asset brokers — gated on stocks/options/forex going live ───
  {
    slug: 'tradier', name: 'Tradier',
    tagline: 'Stocks · Options · Sandbox + Live',
    domain: 'tradier.com', initials: 'TR', accent: 'bg-orange-600', status: 'available',
    category: 'multi_asset',
    helpUrl: 'https://documentation.tradier.com/',
    fields: [
      { key: 'access_token', label: 'Access Token', type: 'password', hint: 'Generate at developer.tradier.com → Sandbox/Production → API Access Keys.' },
      { key: 'account_id',   label: 'Account Number (optional)', type: 'text', hint: 'Leave blank to auto-pick your first account, or paste a specific account number (e.g. VA12345678).' },
    ],
  },
  {
    slug: 'webull', name: 'Webull',
    tagline: 'Stocks · Options',
    domain: 'webull.com', initials: 'WB', accent: 'bg-cyan-600', status: 'pending_assets',
    category: 'multi_asset',
  },
  {
    slug: 'robinhood', name: 'Robinhood',
    tagline: 'Stocks · Options · Crypto',
    domain: 'robinhood.com', initials: 'RH', accent: 'bg-lime-600', status: 'pending_assets',
    category: 'multi_asset',
  },
  {
    slug: 'schwab', name: 'Charles Schwab',
    tagline: 'Stocks · Options · Mutual funds',
    domain: 'schwab.com', initials: 'CS', accent: 'bg-sky-700', status: 'pending_assets',
    category: 'multi_asset',
  },
  {
    slug: 'etrade', name: 'E*TRADE',
    tagline: 'Stocks · Options',
    domain: 'etrade.com', initials: 'ET', accent: 'bg-purple-700', status: 'pending_assets',
    category: 'multi_asset',
  },
  {
    slug: 'oanda', name: 'OANDA',
    tagline: 'Forex · CFDs',
    domain: 'oanda.com', initials: 'OA', accent: 'bg-teal-600', status: 'pending_assets',
    category: 'multi_asset',
  },
]

// Logo tile with multi-source fallback chain:
//   1. Clearbit (best quality when it works — but their free tier is flaky now)
//   2. Google's favicon service (reliable for any public domain)
//   3. Colored initials tile
function BrokerLogo({ broker, size = 12 }: { broker: typeof BROKERS[number]; size?: 10 | 12 | 14 }) {
  const sources = [
    `https://logo.clearbit.com/${broker.domain}`,
    `https://www.google.com/s2/favicons?domain=${broker.domain}&sz=128`,
  ]
  const [idx, setIdx] = useState(0)
  const [allFailed, setAllFailed] = useState(false)
  const px = size === 14 ? 'w-14 h-14' : size === 10 ? 'w-10 h-10' : 'w-12 h-12'

  if (allFailed) {
    return (
      <div className={`${px} rounded-xl ${broker.accent} flex items-center justify-center text-white font-extrabold text-base flex-shrink-0`}>
        {broker.initials}
      </div>
    )
  }
  return (
    <div className={`${px} rounded-xl bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 flex items-center justify-center overflow-hidden flex-shrink-0`}>
      <img src={sources[idx]}
           alt={broker.name}
           referrerPolicy="no-referrer"
           onError={() => {
             if (idx + 1 < sources.length) setIdx(idx + 1)
             else setAllFailed(true)
           }}
           className="w-full h-full object-contain p-1.5"/>
    </div>
  )
}

function BrokerConnectModal({
  onClose, selectedBroker, setSelectedBroker,
  accountForm, setAccountForm,
  testResult, setTestResult,
  accountError, setAccountError,
  onTest, onConnect, testing, connecting,
}: {
  onClose: () => void
  selectedBroker: string | null
  setSelectedBroker: (s: string | null) => void
  accountForm: any
  setAccountForm: (f: any) => void
  testResult: { ok: boolean; msg: string } | null
  setTestResult: (r: { ok: boolean; msg: string } | null) => void
  accountError: string | null
  setAccountError: (s: string | null) => void
  onTest: () => void
  onConnect: () => void
  testing: boolean
  connecting: boolean
}) {
  const broker = BROKERS.find(b => b.slug === selectedBroker)

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-3xl max-h-[90vh] flex flex-col dark:bg-slate-900">
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-200 dark:border-slate-800">
          <div className="flex items-center gap-3 min-w-0">
            {broker && (
              <button onClick={() => { setSelectedBroker(null); setTestResult(null); setAccountError(null) }}
                className="text-slate-400 hover:text-slate-700 text-sm font-medium flex-shrink-0">← Back</button>
            )}
            {broker && <BrokerLogo broker={broker} size={10} />}
            <h2 className="text-lg font-extrabold text-slate-900 dark:text-slate-100 truncate">
              {broker ? `Connect ${broker.name}` : 'Choose your broker'}
            </h2>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400 dark:text-slate-500 dark:hover:bg-slate-800 flex-shrink-0"><X size={18}/></button>
        </div>

        {/* Step 1 — broker grid (split: Futures available now / Multi-asset coming with new asset classes) */}
        {!broker && (() => {
          const futures = BROKERS.filter(b => b.category === 'futures')
          const multi   = BROKERS.filter(b => b.category === 'multi_asset')
          const renderCard = (b: typeof BROKERS[number]) => {
            const live = b.status === 'available'
            return (
              <button
                key={b.slug}
                disabled={!live}
                onClick={() => live && setSelectedBroker(b.slug)}
                className={`text-left rounded-xl border p-4 transition ${
                  live
                    ? 'border-slate-200 hover:border-blue-500 hover:shadow-md dark:border-slate-700 dark:hover:border-blue-400 cursor-pointer bg-white dark:bg-slate-900'
                    : 'border-slate-100 bg-slate-50 dark:bg-slate-900/50 dark:border-slate-800 opacity-75 cursor-not-allowed'
                }`}>
                <div className="flex items-center gap-3">
                  <BrokerLogo broker={b} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-bold text-slate-900 dark:text-slate-100 truncate">{b.name}</span>
                      {b.status === 'available' && <span className="text-[10px] font-bold uppercase tracking-wider text-green-700 bg-green-100 dark:text-green-300 dark:bg-green-900/40 px-1.5 py-0.5 rounded">Available</span>}
                      {b.status === 'pending_broker' && <span className="text-[10px] font-bold uppercase tracking-wider text-amber-700 bg-amber-100 dark:text-amber-300 dark:bg-amber-900/40 px-1.5 py-0.5 rounded">Coming soon</span>}
                      {b.status === 'pending_assets' && <span className="text-[10px] font-bold uppercase tracking-wider text-slate-500 bg-slate-100 dark:text-slate-400 dark:bg-slate-800 px-1.5 py-0.5 rounded">Coming soon</span>}
                    </div>
                    <div className="text-xs text-slate-500 dark:text-slate-400 mt-1 line-clamp-2">{b.tagline}</div>
                  </div>
                </div>
              </button>
            )
          }
          return (
            <div className="flex-1 overflow-y-auto px-6 py-5 space-y-6">
              <p className="text-sm text-slate-500 dark:text-slate-400">Select the broker where your trading account is held. Your credentials never leave your account — they're encrypted with AES-256 before storage.</p>

              <div>
                <div className="text-[11px] font-bold uppercase tracking-widest text-slate-500 dark:text-slate-400 mb-3">Futures Brokers</div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {futures.map(renderCard)}
                </div>
              </div>

              <div>
                <div className="text-[11px] font-bold uppercase tracking-widest text-slate-500 dark:text-slate-400 mb-1">Stocks · Options · Forex</div>
                <div className="text-[11px] text-slate-400 dark:text-slate-500 mb-3">Available once stocks, options, and forex are added to the platform.</div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {multi.map(renderCard)}
                </div>
              </div>
            </div>
          )
        })()}

        {/* Step 2 — broker-specific credential form */}
        {broker && (
          <>
            <div className="flex-1 overflow-y-auto px-6 py-5 space-y-4">
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Account Name</label>
                <input value={accountForm.account_name} onChange={e => setAccountForm({ ...accountForm, account_name: e.target.value })}
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
                  placeholder={`e.g. ${broker.name} Main`}/>
              </div>

              <div className="border-t border-slate-100 pt-4 dark:border-slate-800">
                <div className="flex items-center justify-between mb-3">
                  <div className="text-xs font-semibold text-slate-600 uppercase tracking-wider dark:text-slate-300">{broker.name} Credentials</div>
                  {broker.helpUrl && (
                    <a href={broker.helpUrl} target="_blank" rel="noopener noreferrer" className="text-[11px] font-medium text-blue-600 hover:underline">Where do I find these?</a>
                  )}
                </div>
                <div className="space-y-3">
                  {(broker.fields || []).map(({ key, label, type, hint }) => (
                    <div key={key}>
                      <label className="text-xs font-medium text-slate-700 dark:text-slate-300 block mb-1">{label}</label>
                      <input type={type} value={accountForm.credentials[key] ?? ''}
                        onChange={e => { setAccountForm({ ...accountForm, credentials: { ...accountForm.credentials, [key]: e.target.value } }); setTestResult(null); setAccountError(null) }}
                        autoComplete="off"
                        className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"/>
                      <p className="text-[10.5px] text-slate-400 dark:text-slate-500 mt-1 leading-snug">{hint}</p>
                    </div>
                  ))}
                </div>
              </div>

              <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-xl p-3.5 flex items-start gap-3">
                <input type="checkbox" id="is_demo" checked={accountForm.is_demo}
                  onChange={e => setAccountForm({ ...accountForm, is_demo: e.target.checked })}
                  className="w-4 h-4 rounded text-amber-600 mt-0.5"/>
                <div className="flex-1">
                  <label htmlFor="is_demo" className="text-sm font-bold text-amber-900 dark:text-amber-200 cursor-pointer block">
                    {broker?.slug === 'tradier' ? 'Sandbox mode (recommended)' : 'Use demo / simulator environment'}
                  </label>
                  <div className="text-[11px] text-amber-700 dark:text-amber-300 mt-0.5">
                    {broker?.slug === 'tradier'
                      ? 'Sandbox = simulated fills, no money down required. Real market data. Flip to production later with a separate Tradier token (production requires a funded account).'
                      : 'Bot will log signals but won’t place real orders. Verify behavior before going live.'}
                  </div>
                </div>
              </div>

              <div className="bg-blue-50 border border-blue-100 rounded-xl p-3 text-xs text-blue-700 dark:bg-blue-900/20 dark:border-blue-900 dark:text-blue-300">
                Your credentials are encrypted with AES-256 before storage and are never logged or transmitted in plaintext.
              </div>

              {testResult && (
                <div className={`rounded-xl p-3 text-xs ${testResult.ok ? 'bg-green-50 border border-green-200 text-green-700 dark:bg-green-900/20 dark:border-green-900 dark:text-green-300' : 'bg-red-50 border border-red-200 text-red-700 dark:bg-red-900/20 dark:border-red-900 dark:text-red-300'}`}>
                  {testResult.ok ? '✓ ' : ''}{testResult.msg}
                </div>
              )}
              {accountError && (
                <div className="rounded-xl p-3 text-xs bg-red-50 border border-red-200 text-red-700 dark:bg-red-900/20 dark:border-red-900 dark:text-red-300">
                  {accountError}
                </div>
              )}
            </div>
            <div className="flex gap-3 px-6 py-4 border-t border-slate-100 dark:border-slate-800">
              <button onClick={onClose} className="flex-1 border border-slate-200 text-slate-600 py-2.5 rounded-xl text-sm font-medium dark:text-slate-300 dark:border-slate-700">Cancel</button>
              <button onClick={onTest} disabled={(!accountForm.credentials.access_token && (!accountForm.credentials.username || !accountForm.credentials.password)) || testing}
                className="flex-1 border border-slate-200 text-slate-700 hover:bg-slate-100 disabled:opacity-50 py-2.5 rounded-xl text-sm font-semibold dark:text-slate-200 dark:border-slate-700 dark:hover:bg-slate-800">
                {testing ? 'Testing…' : 'Test Connection'}
              </button>
              <button onClick={onConnect} disabled={!accountForm.account_name || (!accountForm.credentials.access_token && !accountForm.credentials.username) || connecting}
                className="flex-1 bg-rose-600 hover:bg-rose-700 disabled:opacity-50 text-white py-2.5 rounded-xl text-sm font-semibold transition-colors">
                {connecting ? 'Connecting…' : 'Connect Account'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>

  )
}
