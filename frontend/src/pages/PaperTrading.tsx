import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { paperTradingApi, strategiesApi, tradesApi, optionsPaperApi } from '../api/endpoints'
import { useState, useEffect } from 'react'
import { PlayCircle, StopCircle, X, Activity, AlertTriangle, Trash2, Power } from 'lucide-react'
import CandlestickChart from '../components/CandlestickChart'
import RefreshButton from '../components/RefreshButton'
import ToggleSwitch from '../components/ToggleSwitch'
import { fmtEntryTime, fmtHold } from '../components/TradeMetrics'
import { TradeChartModal } from '../components/TradeChartModal'

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-slate-100 dark:bg-slate-900/60 px-2 py-1.5 dark:bg-slate-800">
      <div className="text-[10px] text-slate-400 dark:text-slate-500 uppercase tracking-wider">{label}</div>
      <div className="text-xs font-semibold text-slate-700 dark:text-slate-200 mt-0.5">{value}</div>
    </div>
  )
}

function filterByPeriod(t: any, period: 'today' | 'month' | 'year' | 'all'): boolean {
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

const PERIOD_LABELS: Record<'today' | 'month' | 'year' | 'all', string> = {
  today: 'Today',
  month: 'Month',
  year:  'Year',
  all:   'All time',
}

function PeriodTabs({ value, onChange }: { value: 'today' | 'month' | 'year' | 'all'; onChange: (v: 'today' | 'month' | 'year' | 'all') => void }) {
  const opts: ('today' | 'month' | 'year' | 'all')[] = ['today', 'month', 'year', 'all']
  return (
    <div className="inline-flex rounded-lg bg-slate-100 dark:bg-slate-900 p-0.5 dark:bg-slate-800">
      {opts.map(o => (
        <button
          key={o}
          onClick={() => onChange(o)}
          className={`px-2 py-0.5 text-[10px] font-semibold rounded-md transition-colors ${ value === o ? 'bg-white dark:bg-slate-700 text-blue-600 dark:text-blue-300 shadow-sm' : 'text-slate-500 dark:text-slate-400 hover:text-slate-800 dark:hover:text-slate-200' }`}
        >
          {PERIOD_LABELS[o]}
        </button>
      ))}
    </div>
  )
}



// ── Options Paper sub-panel ────────────────────────────────────────────
function OptionsPaperPanel({ strategies }: { strategies: any[] }) {
  const qc = useQueryClient()
  const [strategyId, setStrategyId] = useState('')
  const [underlying, setUnderlying] = useState('SPY')
  const [error, setError] = useState<string | null>(null)

  const optStrats = strategies.filter((s: any) =>
    (s.options_mode || (s.instruments || []).some((i: string) =>
      ['SPY','QQQ','NVDA','AAPL','MSFT','TSLA','AMD','META','AMZN','GOOGL','JPM','KO'].includes(i)))
    && (s.status || '').toLowerCase() === 'active'
  )

  const { data: sessions = [] } = useQuery({
    queryKey: ['options-paper-sessions'],
    queryFn: () => optionsPaperApi.listSessions().then((r: any) => r.data),
    refetchInterval: 15000,
  })

  const startMut = useMutation({
    mutationFn: () => optionsPaperApi.startSession({ strategy_id: strategyId, underlying }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['options-paper-sessions'] })
      setError(null)
    },
    onError: (e: any) => setError(e?.response?.data?.detail || 'Failed to start.'),
  })

  const stopMut = useMutation({
    mutationFn: (id: string) => optionsPaperApi.stopSession(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['options-paper-sessions'] }),
  })

  const selected = optStrats.find((s: any) => s.id === strategyId)
  const universe = selected?.instruments || ['SPY','QQQ','NVDA','AAPL','MSFT']

  return (
    <div className="space-y-6">
      <div className="rounded-2xl border border-violet-200 dark:border-violet-800 bg-gradient-to-br from-violet-50 to-fuchsia-50 dark:from-violet-900/20 dark:to-fuchsia-900/20 p-5">
        <div className="flex items-center gap-2 mb-2">
          <span className="px-2 py-0.5 rounded bg-violet-600 text-white text-[10px] font-bold uppercase tracking-wider">Beta</span>
          <h3 className="font-extrabold text-slate-900 dark:text-slate-100">Options Paper — Swing simulator</h3>
        </div>
        <p className="text-xs text-slate-600 dark:text-slate-300 leading-relaxed">
          Simulates options trades against real underlying prices using Black-Scholes pricing.
          Picks strike + expiry from your strategy config (delta band + DTE band), applies theta decay,
          marks-to-market every minute, and closes on stop/target/expiry. No real broker needed.
        </p>
      </div>

      <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-5">
        <h4 className="text-xs uppercase tracking-wider font-extrabold text-slate-700 dark:text-slate-200 mb-3">Start an options session</h4>
        <div className="grid sm:grid-cols-2 gap-3 mb-3">
          <div>
            <label className="text-[11px] font-semibold text-slate-500 dark:text-slate-400 block mb-1">Strategy</label>
            <select value={strategyId} onChange={e => setStrategyId(e.target.value)}
              className="w-full border border-slate-300 dark:border-slate-700 rounded-lg px-3 py-2 text-sm bg-white dark:bg-slate-800">
              <option value="">Select an options strategy...</option>
              {optStrats.map((s: any) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
            {optStrats.length === 0 && (
              <p className="text-[11px] text-amber-600 dark:text-amber-400 mt-1">No options strategies — create one on Strategies.</p>
            )}
          </div>
          <div className="rounded-lg bg-violet-50 dark:bg-violet-900/20 border border-violet-200 dark:border-violet-900 p-3">
            <div className="text-[10px] uppercase tracking-wider text-violet-700 dark:text-violet-300 font-bold mb-1">Watchlist</div>
            <p className="text-[11px] text-slate-700 dark:text-slate-200 leading-snug">
              Scans <strong>20+ liquid optionable tickers</strong> (SPY, QQQ, IWM, NVDA, AAPL, MSFT, TSLA, AMD, META, AMZN, GOOGL, JPM, BAC, KO, DIS, NFLX, COIN, PLTR, UBER + your strategy universe). Picks the best setup per minute. No underlying selection needed.
            </p>
          </div>
        </div>
        {error && <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 text-red-600 text-xs px-3 py-2 rounded-lg mb-3">{error}</div>}
        <button onClick={() => startMut.mutate()}
          disabled={!strategyId || startMut.isPending}
          className="w-full bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white py-2 rounded-xl text-sm font-bold">
          {startMut.isPending ? 'Starting…' : 'Start Options Paper Session'}
        </button>
      </div>

      <div>
        <h4 className="text-xs uppercase tracking-wider font-extrabold text-slate-700 dark:text-slate-200 mb-2">Active sessions</h4>
        {sessions.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-300 dark:border-slate-700 p-6 text-center text-sm text-slate-400 dark:text-slate-500">
            No active options paper sessions.
          </div>
        ) : (
          <div className="space-y-2">
            {sessions.map((s: any) => (
              <div key={s.id} className="flex items-center justify-between rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-3">
                <div className="min-w-0">
                  <div className="font-bold text-sm text-slate-900 dark:text-slate-100 truncate">{s.strategy_name} · <span className="text-violet-600 dark:text-violet-400">{s.underlying}</span></div>
                  <div className="text-[11px] text-slate-500 dark:text-slate-400">
                    {s.is_active ? '● Active' : '○ Stopped'} · {s.total_trades} trades · P&L ${(s.net_pnl ?? 0).toFixed(2)}
                  </div>
                </div>
                {s.is_active && (
                  <button onClick={() => stopMut.mutate(s.id)} disabled={stopMut.isPending}
                    className="px-3 py-1.5 rounded-lg text-[11px] font-bold text-rose-600 dark:text-rose-400 hover:bg-rose-50 dark:hover:bg-rose-900/20 border border-rose-200 dark:border-rose-900">
                    Stop
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export default function PaperTrading() {
  const qc = useQueryClient()
  const [showStart, setShowStart] = useState(false)
  const [assetClass, setAssetClass] = useState<'futures'|'options'>('futures')
  const [error, setError] = useState<string | null>(null)
  const [form, setForm] = useState({ strategy_id: '', instruments: ['ES'] as string[], daily_loss_limit: '' })
  const [chartTradeId, setChartTradeId] = useState<string | null>(null)

  // Wall-clock tick: derives countdown + elapsed below from real time, so they
  // don't reset when the user navigates away and back to this page.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])

  const { data: sessions = [] }   = useQuery({ queryKey: ['paper-sessions'], queryFn: () => paperTradingApi.listSessions().then(r => r.data) })
  const { data: strategies = [] } = useQuery({ queryKey: ['strategies'], queryFn: () => strategiesApi.list().then(r => r.data) })
  const { data: trades = [] }     = useQuery({ queryKey: ['paper-trades'], queryFn: () => tradesApi.list({ mode: 'paper', limit: 1000 }).then(r => r.data), refetchInterval: 30000 })
  const { data: openPositions = [] } = useQuery({ queryKey: ['open-positions'], queryFn: () => tradesApi.openPositions().then(r => r.data), refetchInterval: 10000 })
  const { data: chartData }       = useQuery({ queryKey: ['paper-chart'], queryFn: () => tradesApi.getChartData('paper', 'ES').then(r => r.data), refetchInterval: 30000 })

  const startMutation = useMutation({
    mutationFn: () => paperTradingApi.startSession({
      strategy_id: form.strategy_id,
      instruments: Array.from(new Set(form.instruments)),
      daily_loss_limit: form.daily_loss_limit ? parseFloat(form.daily_loss_limit) : undefined,
    }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['paper-sessions'] }); setShowStart(false); setError(null) },
    onError: (e: any) => setError(e?.response?.data?.detail || 'Failed to start session'),
  })

  const stopMutation = useMutation({
    mutationFn: (id: string) => paperTradingApi.stopSession(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['paper-sessions'] }),
  })

  const stopAllMutation = useMutation({
    mutationFn: () => paperTradingApi.stopAllSessions(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['paper-sessions'] }),
  })

  const closeAllOpenMutation = useMutation({
    mutationFn: () => paperTradingApi.closeAllOpenPositions(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['open-positions'] })
      qc.invalidateQueries({ queryKey: ['paper-trades'] })
      qc.invalidateQueries({ queryKey: ['paper-sessions'] })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => paperTradingApi.deleteSession(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['paper-sessions'] }),
  })

  const activeSessions = sessions.filter((s: any) => s.is_active)

  function fmtElapsed(startedAtIso: string): string {
    const elapsedSec = Math.max(0, Math.floor((now - new Date(startedAtIso).getTime()) / 1000))
    const h = Math.floor(elapsedSec / 3600)
    const m = Math.floor((elapsedSec % 3600) / 60)
    const s = elapsedSec % 60
    if (h > 0) return `${h}h ${m}m`
    if (m > 0) return `${m}m ${s}s`
    return `${s}s`
  }

  function fmtCountdown(startedAtIso: string): string {
    const elapsedSec = Math.max(0, Math.floor((now - new Date(startedAtIso).getTime()) / 1000))
    const remaining = 300 - (elapsedSec % 300)
    return `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}`
  }

  // Stats
  const completedTrades = trades.filter((t: any) => t.status === 'closed')
  const allTrades = [...openPositions.map((p: any) => ({ id: p.session_id + '-open', instrument: p.instrument, direction: p.direction, entry_price: p.entry_price, exit_price: p.current_price, stop_loss: p.stop_loss, take_profit: p.take_profit, net_pnl: p.unrealized_pnl, exit_reason: null, status: 'open', contracts: p.contracts })), ...trades]

  type Period = 'today' | 'month' | 'year' | 'all'
  const [pnlPeriod, setPnlPeriod] = useState<Period>('all')
  const periodTrades = completedTrades.filter((t: any) => filterByPeriod(t, pnlPeriod))
  const totalPnl  = periodTrades.reduce((acc: number, t: any) => acc + (t.net_pnl ?? 0), 0)
  const wins      = periodTrades.filter((t: any) => (t.net_pnl ?? 0) > 0).length
  const winRate   = periodTrades.length > 0 ? (wins / periodTrades.length * 100).toFixed(1) : '—'

  return (
    <div className="p-8 max-w-6xl">
      {/* HERO */}
      <div className="rounded-3xl bg-gradient-to-br from-slate-900 via-slate-900 to-emerald-950 dark:from-slate-950 dark:via-slate-950 dark:to-emerald-950 text-white p-6 md:p-8 shadow-xl">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="min-w-0 flex-1">
            <div className="text-[10px] uppercase tracking-[0.2em] text-emerald-300 font-bold mb-1">Simulation</div>
            <h1 className="text-2xl md:text-3xl font-extrabold text-white">Paper Trading</h1>
            <p className="text-sm text-slate-400 mt-1">Real-time market data, zero risk · futures (live runner) & options (Black-Scholes sim)</p>
          </div>
          <div className="flex items-center gap-2">
            <RefreshButton onClick={() => qc.invalidateQueries()}/>
            <button onClick={() => setShowStart(true)}
              className="inline-flex items-center gap-2 bg-emerald-500 hover:bg-emerald-400 text-white px-4 py-2 rounded-xl text-sm font-bold transition-colors shadow-lg shadow-emerald-900/30">
              <PlayCircle size={15}/> Start Session
            </button>
          </div>
        </div>
      </div>

      {/* Asset class tabs */}
      <div className="inline-flex rounded-xl bg-slate-100 dark:bg-slate-900 p-0.5 mb-6 border border-slate-200 dark:border-slate-800">
        {(['futures','options'] as const).map(t => (
          <button key={t} onClick={() => setAssetClass(t)}
            className={`px-4 py-1.5 rounded-lg text-xs font-bold transition-all ${assetClass === t ? 'bg-white dark:bg-slate-800 text-violet-700 dark:text-violet-300 shadow-sm' : 'text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200'}`}>
            {t === 'futures' ? 'Futures (ES/NQ/RTY/YM)' : 'Options (Black-Scholes sim)'}
          </button>
        ))}
      </div>

      {assetClass === 'options' ? (
        <OptionsPaperPanel strategies={strategies}/>
      ) : (
      <>
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
          <div className="text-2xl font-extrabold text-slate-900 dark:text-slate-100">{periodTrades.length}</div>
          {periodTrades.length === 0 && (
            <div className="text-[11px] text-slate-400 dark:text-slate-500 mt-1">No closed trades in {PERIOD_LABELS[pnlPeriod].toLowerCase()}</div>
          )}
        </div>
        <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 dark:bg-slate-900 dark:border-slate-700">
          <div className="text-xs text-slate-400 uppercase tracking-wider font-medium mb-1.5 dark:text-slate-500">Win Rate</div>
          <div className={`text-2xl font-extrabold ${periodTrades.length > 0 && wins/periodTrades.length >= 0.5 ? 'text-green-600' : periodTrades.length === 0 ? 'text-slate-300 dark:text-slate-600' : 'text-slate-900 dark:text-slate-100'}`}>{periodTrades.length === 0 ? '—' : `${winRate}%`}</div>
          {periodTrades.length > 0 && (
            <div className="text-[11px] text-slate-400 dark:text-slate-500 mt-1">{wins}W / {periodTrades.length - wins}L</div>
          )}
        </div>
        <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 dark:bg-slate-900 dark:border-slate-700">
          <div className="text-xs text-slate-400 uppercase tracking-wider font-medium mb-1.5 dark:text-slate-500">Net P&L</div>
          <div className={`text-2xl font-extrabold ${periodTrades.length === 0 ? 'text-slate-300 dark:text-slate-600' : totalPnl >= 0 ? 'text-green-600' : 'text-red-500'}`}>
            {totalPnl >= 0 ? '+' : ''}${totalPnl.toLocaleString('en-US', { minimumFractionDigits: 2 })}
          </div>
        </div>
      </div>

      {/* Sessions list (active + stopped) */}
      {sessions.length > 0 && (() => {
        const openPnl = openPositions.reduce((acc: number, p: any) => acc + (p.unrealized_pnl ?? 0), 0)
        const openCount = openPositions.length
        return (
        <div className="mb-6">
          <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
            <div className="flex items-center gap-3 flex-wrap">
              <h2 className="text-sm font-bold text-slate-700 dark:text-slate-200">
                Sessions ({sessions.length}) · {activeSessions.length} active
              </h2>
              {openCount > 0 && (
                <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-[11px] font-semibold bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-900/40">
                  <span className="text-blue-700 dark:text-blue-300">{openCount} open · Open P&L</span>
                  <span className={openPnl >= 0 ? 'text-green-600' : 'text-red-500'}>
                    {openPnl >= 0 ? '+' : ''}${openPnl.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </span>
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              {openCount > 0 && (
                <button
                  onClick={() => { if (confirm(`Close all ${openCount} open position${openCount === 1 ? '' : 's'} at market?`)) closeAllOpenMutation.mutate() }}
                  disabled={closeAllOpenMutation.isPending}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold border border-amber-200 text-amber-700 hover:bg-amber-50 dark:hover:bg-amber-900/20 rounded-lg transition-colors disabled:opacity-50">
                  <StopCircle size={12}/> {closeAllOpenMutation.isPending ? 'Closing…' : 'Close all open'}
                </button>
              )}
              {activeSessions.length > 0 && (
                <button
                  onClick={() => stopAllMutation.mutate()}
                  disabled={stopAllMutation.isPending}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold border border-red-200 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg transition-colors disabled:opacity-50">
                  <Power size={12}/> Turn all off
                </button>
              )}
            </div>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
            {sessions.map((s: any) => {
              const wr = s.total_trades > 0 ? ((s.wins ?? 0) / s.total_trades * 100) : null
              const pnlColor = s.net_pnl >= 0 ? 'text-green-600' : 'text-red-500'
              const title = s.label || s.strategy_name || 'Session'
              return (
                <Link
                  key={s.id}
                  to={`/app/paper/${s.id}`}
                  className={`group rounded-xl border p-4 transition-all hover:shadow-md hover:-translate-y-0.5 ${ s.is_active ? 'bg-white dark:bg-slate-800 border-green-200 dark:border-green-900/40 hover:border-green-300' : 'bg-slate-50 dark:bg-slate-900 border-slate-200 dark:border-slate-700 hover:border-slate-300' }`}
                >
                  <div className="flex items-start justify-between gap-2 mb-3">
                    <div className="flex items-start gap-2 min-w-0">
                      <div className={`w-2 h-2 rounded-full flex-shrink-0 mt-2 ${ s.is_active ? 'bg-green-500 animate-pulse' : 'bg-slate-300 dark:bg-slate-600' }`}/>
                      <div className="min-w-0">
                        <div className="font-bold text-sm truncate text-slate-900 dark:text-slate-100 group-hover:text-blue-600 transition-colors">
                          {title}
                        </div>
                        <div className="text-[11px] text-slate-500 dark:text-slate-400 truncate mt-0.5">
                          {s.label && <span>{s.strategy_name} · </span>}
                          {s.instrument || '—'}
                          {s.is_active && <> · Active for {fmtElapsed(s.started_at)}</>}
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center gap-1 flex-shrink-0" onClick={(e) => e.preventDefault()}>
                      {s.is_active && (
                        <ToggleSwitch
                          checked={true}
                          disabled={stopMutation.isPending}
                          onChange={() => stopMutation.mutate(s.id)}
                        />
                      )}
                      <button
                        onClick={(e) => { e.preventDefault(); e.stopPropagation(); deleteMutation.mutate(s.id) }}
                        disabled={deleteMutation.isPending}
                        title="Delete session"
                        className="p-1.5 rounded-lg text-slate-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors disabled:opacity-50 dark:text-slate-500">
                        <Trash2 size={13}/>
                      </button>
                    </div>
                  </div>
                  <div className={`text-xl font-extrabold ${pnlColor} mb-2`}>
                    {s.net_pnl >= 0 ? '+' : ''}${s.net_pnl.toFixed(2)}
                  </div>
                  <div className="grid grid-cols-3 gap-2 text-[11px]">
                    <Stat label="Trades" value={String(s.total_trades)} />
                    <Stat label="W / L" value={`${s.wins ?? 0} / ${s.losses ?? 0}`} />
                    <Stat label="Win rate" value={wr != null ? `${wr.toFixed(0)}%` : '—'} />
                  </div>
                  {s.is_active && (
                    <div className="text-[10px] text-slate-400 dark:text-slate-500 mt-2.5 truncate">
                      Next update in {fmtCountdown(s.started_at)}
                    </div>
                  )}
                </Link>
              )
            })}
          </div>
        </div>
        )
      })()}

      {/* Price Chart */}
      {chartData && chartData.candles && chartData.candles.length > 0 && (
        <div className="mb-6">
          <h2 className="text-base font-bold text-slate-900 mb-3 dark:text-slate-100">Price Chart & Trades</h2>
          <CandlestickChart candles={chartData.candles} markers={chartData.markers || []} height={400} />
        </div>
      )}

      {/* Trade history */}
      <h2 className="text-base font-bold text-slate-900 mb-3 dark:text-slate-100">Trade History</h2>
      <div className="bg-slate-50 rounded-2xl border border-slate-200 overflow-hidden shadow-sm dark:bg-slate-900 dark:border-slate-700">
        {allTrades.length === 0 ? (
          <div className="p-14 text-center">
            <Activity size={32} className="mx-auto text-slate-200 mb-3"/>
            <p className="text-sm font-medium text-slate-400 dark:text-slate-500">No paper trades yet</p>
            <p className="text-xs text-slate-300 mt-1 dark:text-slate-600">Start a session to begin simulated trading</p>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-slate-50 border-b border-slate-200 dark:bg-slate-900 dark:border-slate-700">
                {['Instrument', 'Direction', 'Entry', 'Exit', 'Stop Loss', 'Take Profit', 'Net P&L', 'Exit Reason', 'Status', 'Chart'].map(h => (
                  <th key={h} className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider whitespace-nowrap dark:text-slate-400">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {allTrades.map((t: any) => (
                <tr key={t.id} className="hover:bg-slate-50 transition-colors dark:hover:bg-slate-800">
                  <td className="px-4 py-3.5 font-semibold text-slate-900 dark:text-slate-100">{t.instrument}</td>
                  <td className="px-4 py-3.5">
                    <span className={`badge ${t.direction === 'long' ? 'badge-green' : 'badge-red'}`}>
                      {t.direction.toUpperCase()}
                    </span>
                  </td>
                  <td className="px-4 py-3.5 text-slate-600 font-medium dark:text-slate-300">{t.entry_price?.toFixed(2) ?? '—'}</td>
                  <td className="px-4 py-3.5 text-slate-600 font-medium dark:text-slate-300">{t.exit_price?.toFixed(2) ?? 'Open'}</td>
                  <td className="px-4 py-3.5 text-slate-400 dark:text-slate-500">{t.stop_loss.toFixed(2)}</td>
                  <td className="px-4 py-3.5 text-slate-400 dark:text-slate-500">{t.take_profit.toFixed(2)}</td>
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
      {chartTradeId && <TradeChartModal tradeId={chartTradeId} onClose={() => setChartTradeId(null)} />}

      {/* Start modal */}
      </>
      )}
      {showStart && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-slate-50 rounded-2xl shadow-2xl w-full max-w-sm dark:bg-slate-900">
            <div className="flex items-center justify-between px-6 py-5 border-b border-slate-100 dark:border-slate-800">
              <h2 className="text-base font-bold text-slate-900 dark:text-slate-100">Start Paper Session</h2>
              <button onClick={() => setShowStart(false)} className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400 dark:text-slate-500 dark:hover:bg-slate-800"><X size={16}/></button>
            </div>
            <div className="px-6 py-5 space-y-4">
              {error && <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 text-red-600 text-xs px-3 py-2 rounded-lg flex items-center gap-2"><AlertTriangle size={13}/> {error}</div>}
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Strategy</label>
                <select value={form.strategy_id} onChange={e => setForm({...form, strategy_id: e.target.value})}
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700">
                  <option value="">Select a futures strategy...</option>
                  {(() => {
                    const OPT_TICKERS = ['SPY','QQQ','NVDA','AAPL','MSFT','TSLA','AMD','META','AMZN','GOOGL','JPM','KO']
                    const isOptions = (st: any) =>
                      !!st.options_mode ||
                      (st.instruments || []).some((i: string) => OPT_TICKERS.includes(i))
                    const futuresOnly = strategies.filter((st: any) => !isOptions(st) && (st.status || '').toLowerCase() === 'active')
                    if (futuresOnly.length === 0) {
                      return <option value="" disabled>No active futures strategies — create one on the Strategies page</option>
                    }
                    return futuresOnly.map((st: any) => <option key={st.id} value={st.id}>{st.name}</option>)
                  })()}
                </select>
                <p className="text-[11px] text-slate-400 mt-1.5 dark:text-slate-500">
                  Paper trading runs <strong>futures only</strong> (ES/NQ/RTY/YM). For options paper-trading, use a <a href="/app/live" className="text-blue-600 underline">Tradier sandbox account</a>.
                </p>
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Instruments</label>
                <div className="flex gap-2 flex-wrap">
                  {['ES', 'NQ', 'RTY', 'YM'].map(inst => (
                    <button key={inst} type="button"
                      onClick={() => setForm(f => ({
                        ...f,
                        instruments: f.instruments.includes(inst)
                          ? f.instruments.filter(i => i !== inst)
                          : [...f.instruments, inst],
                      }))}
                      className={`px-3.5 py-2 rounded-lg text-sm font-semibold border transition-all ${form.instruments.includes(inst) ? 'bg-green-600 text-white border-green-600 shadow-sm shadow-green-200' : 'bg-white dark:bg-slate-800 text-slate-500 border-slate-300 hover:border-slate-400'} dark:text-slate-400 dark:border-slate-700`}>
                      {inst}
                    </button>
                  ))}
                </div>
                <p className="text-[11px] text-slate-400 mt-1.5 dark:text-slate-500">
                  Click to toggle. All selected instruments run under a single session.
                </p>
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Daily Loss Limit ($) — optional</label>
                <input type="number" value={form.daily_loss_limit} onChange={e => setForm({...form, daily_loss_limit: e.target.value})}
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700" placeholder="e.g. 500"/>
              </div>
            </div>
            <div className="flex gap-3 px-6 py-4 border-t border-slate-100 dark:border-slate-800">
              <button onClick={() => setShowStart(false)} className="flex-1 border border-slate-200 text-slate-600 py-2.5 rounded-xl text-sm font-medium dark:text-slate-300 dark:border-slate-700">Cancel</button>
              <button onClick={() => startMutation.mutate()} disabled={!form.strategy_id || form.instruments.length === 0 || startMutation.isPending}
                className="flex-1 bg-green-600 hover:bg-green-700 disabled:opacity-50 text-white py-2.5 rounded-xl text-sm font-semibold transition-colors">
                Start Session
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
