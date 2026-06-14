import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { LineChart, Line, XAxis, YAxis, ResponsiveContainer, CartesianGrid, Tooltip } from 'recharts'
import { Play, Trash2, FlaskConical, X, Sparkles } from 'lucide-react'
import { strategiesApi, backtestsApi, DEFAULT_OPT_GRID } from '../api/endpoints'
import api from '../api/client'

const STATUS_COLORS: Record<string, string> = {
  completed: 'badge-green',
  running: 'badge-amber',
  failed: 'badge-red',
  queued: 'badge-grey',
  cancelled: 'bg-slate-100 text-slate-500'}

/**
 * OptimizePrompt — appears below the metrics of a just-completed backtest.
 * Asks "Optimize for better results?" — Yes spins up a parameter grid using
 * the same strategy/instrument/date_range as the baseline backtest, runs it,
 * then shows the best result vs the baseline side-by-side.
 */
function OptimizePrompt({ selectedRun, baselineMetrics }: { selectedRun: any; baselineMetrics: any }) {
  const [phase, setPhase] = useState<'idle' | 'starting' | 'running' | 'done' | 'error'>('idle')
  const [optRunId, setOptRunId] = useState<string | null>(null)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)

  // Poll the optimization status when running
  const { data: optRun }: any = useQuery({
    queryKey: ['opt-run', optRunId],
    queryFn: () => api.get(`/api/v1/optimization/${optRunId}`).then((r: any) => r.data),
    enabled: !!optRunId && phase === 'running',
    refetchInterval: 2000,
  })
  const { data: optResults }: any = useQuery({
    queryKey: ['opt-results', optRunId],
    queryFn: () => api.get(`/api/v1/optimization/${optRunId}/results`).then((r: any) => r.data),
    enabled: !!optRunId && phase === 'done',
  })

  // Transition phase to 'done' when the optimization completes
  if (phase === 'running' && optRun?.status && (optRun.status === 'COMPLETED' || optRun.status === 'completed')) {
    setPhase('done')
  }
  if (phase === 'running' && optRun?.status && (optRun.status === 'FAILED' || optRun.status === 'failed')) {
    setErrorMsg(optRun.error_message || 'Optimization failed')
    setPhase('error')
  }

  async function start() {
    setPhase('starting'); setErrorMsg(null)
    try {
      // Same default grid as the standalone Optimization page (shared constant).
      const param_grid = DEFAULT_OPT_GRID
      const body = {
        strategy_id: selectedRun.strategy_id,
        instrument: selectedRun.instrument,
        start_date: selectedRun.start_date,
        end_date: selectedRun.end_date,
        timeframe: selectedRun.timeframe || '15m',
        parameter_grid: param_grid,
        optimization_metric: 'profit_factor',
      }
      const r = await api.post('/api/v1/optimization/', body)
      setOptRunId((r as any).data.id)
      setPhase('running')
    } catch (e: any) {
      setErrorMsg(e?.response?.data?.detail || e?.message || 'Failed to start optimization')
      setPhase('error')
    }
  }

  if (phase === 'idle') {
    return (
      <div className="rounded-2xl border-2 border-dashed border-violet-300 dark:border-violet-700 bg-violet-50/40 dark:bg-violet-950/20 p-5">
        <div className="flex items-start gap-3">
          <Sparkles className="text-violet-600 flex-shrink-0 mt-1" size={20} />
          <div className="flex-1">
            <div className="font-bold text-slate-900 dark:text-slate-100 mb-1">Optimize for better results?</div>
            <div className="text-xs text-slate-600 dark:text-slate-400 mb-3">
              I'll run 48 parameter combinations on this exact strategy + instrument + date range, then show you the top performer vs your current result.
              <br/>Time depends on your date range — a full year can take <strong>15–40 min</strong> (the first result lands after the first combo, ~1–2 min). You'll see live progress + a real ETA once it starts.
            </div>
            <div className="flex gap-2">
              <button onClick={start} className="bg-violet-600 hover:bg-violet-500 text-white font-bold text-xs px-4 py-2 rounded-lg flex items-center gap-1.5">
                <Sparkles size={14}/> Yes — optimize
              </button>
              <Link to="/app/optimization" className="bg-slate-200 dark:bg-slate-800 hover:bg-slate-300 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-200 font-semibold text-xs px-4 py-2 rounded-lg">
                Advanced grid →
              </Link>
            </div>
          </div>
        </div>
      </div>
    )
  }

  if (phase === 'starting' || phase === 'running') {
    const prog = optRun?.progress ?? 0
    const etaS = optRun?.eta_seconds
    const etaTxt = (etaS == null) ? null : (etaS < 60 ? `~${Math.round(etaS)}s left` : `~${Math.round(etaS/60)} min left`)
    const done = optRun?.completed_combinations || 0
    const total = optRun?.total_combinations || 48
    return (
      <div className="rounded-2xl border border-violet-300 dark:border-violet-700 bg-violet-50 dark:bg-violet-950/30 p-5">
        <div className="flex items-center gap-3 mb-2">
          <div className="inline-block w-5 h-5 border-2 border-violet-600 border-r-transparent rounded-full animate-spin"/>
          <div className="font-bold text-slate-900 dark:text-slate-100">Optimizing… {prog.toFixed(0)}%{etaTxt ? <span className="font-normal text-slate-500"> · {etaTxt}</span> : null}</div>
        </div>
        <div className="h-2 bg-slate-200 dark:bg-slate-700 rounded-full overflow-hidden mb-2">
          <div className="h-full bg-gradient-to-r from-violet-500 to-fuchsia-500 transition-all duration-700" style={{ width: `${Math.max(2, prog)}%` }}/>
        </div>
        <div className="text-[11px] text-slate-500 dark:text-slate-400">
          {done} of {total} combos done · running in parallel across CPU cores. {done === 0 ? 'Warming up — the first result takes a few minutes on a long date range (a year ≈ 3 min/combo); this is working, not stuck.' : 'Feel free to leave this tab open.'}
        </div>
      </div>
    )
  }

  if (phase === 'error') {
    return (
      <div className="rounded-2xl border border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-950/30 p-5">
        <div className="font-bold text-red-800 dark:text-red-200 mb-1">Optimization failed</div>
        <div className="text-xs text-red-700 dark:text-red-300 mb-3">{errorMsg}</div>
        <button onClick={() => { setPhase('idle'); setErrorMsg(null) }} className="bg-red-600 hover:bg-red-500 text-white text-xs font-bold px-3 py-1.5 rounded-lg">Try again</button>
      </div>
    )
  }

  // phase === 'done'
  const top = (optResults?.results || [])[0]
  if (!top) return <div className="text-sm text-slate-500">Loading top result...</div>
  const baseline = baselineMetrics
  const diffPct = (b: number, t: number) => (b === 0 ? (t > 0 ? '+inf' : '0') : `${((t - b) / Math.abs(b) * 100).toFixed(1)}%`)
  const arrow = (b: number, t: number) => (t > b ? '↗' : t < b ? '↘' : '→')
  const better = (b: number, t: number) => (t > b ? 'text-green-600' : t < b ? 'text-red-500' : 'text-slate-500')

  return (
    <div className="rounded-2xl border border-green-300 dark:border-green-800 bg-green-50/60 dark:bg-green-950/30 p-5">
      <div className="flex items-center gap-2 mb-3">
        <Sparkles className="text-green-600" size={18}/>
        <div className="font-bold text-slate-900 dark:text-slate-100">Optimization complete — top result vs your backtest</div>
      </div>
      <div className="grid grid-cols-3 gap-3 mb-4">
        <div className="text-xs">
          <div className="font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-2">Original</div>
          <div className="space-y-1">
            <div>WR: <b>{(baseline.win_rate * 100).toFixed(1)}%</b></div>
            <div>PF: <b>{baseline.profit_factor.toFixed(2)}</b></div>
            <div>Net: <b>${baseline.net_profit.toLocaleString()}</b></div>
            <div>DD: <b>{baseline.max_drawdown_pct.toFixed(1)}%</b></div>
            <div>Trades: <b>{baseline.total_trades}</b></div>
          </div>
        </div>
        <div className="text-xs">
          <div className="font-bold text-green-700 dark:text-green-400 uppercase tracking-wider mb-2">Optimized</div>
          <div className="space-y-1">
            <div>WR: <b>{(top.win_rate * 100).toFixed(1)}%</b></div>
            <div>PF: <b>{top.profit_factor.toFixed(2)}</b></div>
            <div>Net: <b>${top.net_profit.toLocaleString()}</b></div>
            <div>DD: <b>{top.max_drawdown.toFixed(1)}%</b></div>
            <div>Trades: <b>{top.total_trades}</b></div>
          </div>
        </div>
        <div className="text-xs">
          <div className="font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-2">Change</div>
          <div className="space-y-1">
            <div className={better(baseline.win_rate, top.win_rate)}>{arrow(baseline.win_rate, top.win_rate)} {diffPct(baseline.win_rate, top.win_rate)}</div>
            <div className={better(baseline.profit_factor, top.profit_factor)}>{arrow(baseline.profit_factor, top.profit_factor)} {diffPct(baseline.profit_factor, top.profit_factor)}</div>
            <div className={better(baseline.net_profit, top.net_profit)}>{arrow(baseline.net_profit, top.net_profit)} {diffPct(baseline.net_profit, top.net_profit)}</div>
            <div className={better(top.max_drawdown, baseline.max_drawdown_pct)}>{top.max_drawdown < baseline.max_drawdown_pct ? '↘ smaller' : '↗ bigger'}</div>
            <div className="text-slate-500">{top.total_trades - baseline.total_trades > 0 ? '+' : ''}{top.total_trades - baseline.total_trades}</div>
          </div>
        </div>
      </div>
      <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-lg p-3 text-xs">
        <div className="font-bold text-slate-700 dark:text-slate-200 mb-1">Best parameters</div>
        <div className="font-mono text-[11px] text-slate-600 dark:text-slate-300">{JSON.stringify(top.parameters, null, 2)}</div>
      </div>
      <div className="text-[11px] text-slate-500 dark:text-slate-400 mt-3">
        Apply these to the strategy on the <Link to="/app/optimization" className="text-violet-600 underline">Optimization page</Link> (full results + Apply button).
      </div>
    </div>
  )
}

function MetricCard({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 dark:bg-slate-900 dark:border-slate-700">
      <div className="text-xs text-slate-400 font-medium uppercase tracking-wider mb-2 dark:text-slate-500">{label}</div>
      <div className={`text-xl font-extrabold ${color ?? 'text-slate-900 dark:text-slate-100'}`}>{value}</div>
      {sub && <div className="text-xs text-slate-400 mt-0.5 dark:text-slate-500">{sub}</div>}
    </div>
  )
}

const CustomTooltip = ({ active, payload, label }: any) => {
  if (active && payload?.length) {
    return (
      <div className="bg-slate-50 border border-slate-200 rounded-lg shadow-lg px-3 py-2 text-xs dark:bg-slate-900 dark:border-slate-700">
        <div className="text-slate-400 mb-1 dark:text-slate-500">{label}</div>
        <div className="font-bold text-slate-900 dark:text-slate-100">${payload[0].value?.toLocaleString()}</div>
      </div>
    )
  }
  return null
}

const today = () => new Date().toISOString().split('T')[0]
const yearAgo = () => { const d = new Date(); d.setFullYear(d.getFullYear() - 1); return d.toISOString().split('T')[0] }

export default function Backtests() {
  const qc = useQueryClient()
  const [showNew, setShowNew] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [form, setForm] = useState({
    strategy_id: '',
    instrument: 'ES',
    start_date: yearAgo(),
    end_date: today(),
    timeframe: '15m',
    initial_capital: 100000,
    commission_per_side: 2.25,
    slippage_ticks: 1,
    risk_per_trade_pct: 1.0,
    trailing_drawdown: 0,
    daily_loss_limit: 0,
  })

  const { data: strategies = [] } = useQuery({ queryKey: ['strategies'], queryFn: () => strategiesApi.list().then(r => r.data) })
  const { data: runs = [] }       = useQuery({ queryKey: ['backtests'], queryFn: () => backtestsApi.list().then(r => r.data), refetchInterval: (q: any) => ((q?.state?.data as any[]) || []).some((r: any) => { const s = (r?.status || '').toLowerCase(); return s === 'running' || s === 'queued' || s === 'pending' }) ? 2000 : false })
  const { data: metrics, isLoading: metricsLoading, error: metricsError, refetch: refetchMetrics }: any = useQuery({
    queryKey: ['backtest-metrics', selected],
    queryFn: () => backtestsApi.getMetrics(selected!).then(r => r.data),
    enabled: !!selected,
    // Bug #6 fix: if the run JUST completed, metrics row may not be written
    // yet — refetch every 2 sec while no metrics, stop once we have them.
    refetchInterval: (q: any) => (q?.state?.data ? false : 2000),
    // Force refetch on click — even if cached, the row may have moved from
    // RUNNING to COMPLETED since last fetch.
    refetchOnMount: 'always',
    retry: 3,
    retryDelay: 1500,
  })

  const runMutation = useMutation({
    mutationFn: () => backtestsApi.run(form),
    onSuccess: (res: any) => {
      // Drop the new run into the cache immediately so the detail panel
      // can render the progress bar before the list query refetches.
      const strat = strategies.find((s: any) => s.id === form.strategy_id)
      const newRun = {
        ...res.data,
        strategy_name: strat?.name || 'Strategy',
        instrument: form.instrument,
        start_date: form.start_date,
        end_date: form.end_date,
        status: 'queued',
        progress: 0,
      }
      qc.setQueryData(['backtests'], (old: any) => Array.isArray(old) ? [newRun, ...old] : [newRun])
      qc.invalidateQueries({ queryKey: ['backtests'] })
      setSelected(res.data.id)
      setShowNew(false)
    },
  })
  const deleteMutation = useMutation({
    mutationFn: (id: string) => backtestsApi.delete(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['backtests'] }); if (selected) setSelected(null) },
  })

  const selectedRun: any = runs.find((r: any) => r.id === selected)

  return (
    <div className="space-y-6 max-w-7xl mx-auto px-4 sm:px-6 py-6">
      {/* HERO */}
      <div className="rounded-3xl bg-gradient-to-br from-slate-900 via-slate-900 to-blue-950 dark:from-slate-950 dark:via-slate-950 dark:to-blue-950 text-white p-6 md:p-8 shadow-xl">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="min-w-0 flex-1">
            <div className="text-[10px] uppercase tracking-[0.2em] text-blue-300 font-bold mb-1">Research</div>
            <h1 className="text-2xl md:text-3xl font-extrabold text-white flex items-center gap-2.5">
              <FlaskConical size={26}/> Backtests
            </h1>
            <p className="text-sm text-slate-400 mt-1">Test strategies against historical ES & NQ data — see win rate, drawdown, equity curve</p>
          </div>
          <button onClick={() => setShowNew(true)}
            className="inline-flex items-center gap-2 bg-blue-500 hover:bg-blue-400 text-white px-4 py-2 rounded-xl text-sm font-bold transition-colors shadow-lg shadow-blue-900/30">
            <Play size={14}/> New Backtest
          </button>
        </div>
        <div className="grid grid-cols-3 gap-4 mt-6 pt-6 border-t border-white/10">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Total runs</div>
            <div className="text-2xl font-extrabold tabular-nums">{runs.length}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Running</div>
            <div className="text-2xl font-extrabold tabular-nums text-amber-300">{runs.filter((r: any) => (r.status||'').toLowerCase()==='running' || (r.status||'').toLowerCase()==='queued').length}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Completed</div>
            <div className="text-2xl font-extrabold tabular-nums text-emerald-300">{runs.filter((r: any) => (r.status||'').toLowerCase()==='completed').length}</div>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-5">
        {/* Run list */}
        <div className="space-y-2">
          {runs.length === 0 ? (
            <div className="text-center text-sm text-slate-400 dark:text-slate-500 py-12 border border-dashed rounded-xl border-slate-300 dark:border-slate-700">
              No backtests yet
            </div>
          ) : runs.map((r: any) => (
            <button key={r.id} onClick={() => setSelected(r.id)}
              className={`w-full text-left rounded-xl border p-3 transition-all ${selected === r.id ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20' : 'border-slate-200 bg-white hover:border-blue-300 dark:bg-slate-900 dark:border-slate-700'}`}>
              <div className="flex items-center justify-between gap-2 mb-1">
                <div className="text-sm font-bold text-slate-900 dark:text-slate-100 truncate">{r.strategy_name || 'Strategy'}</div>
                <span className={`badge ${STATUS_COLORS[r.status] || 'badge-grey'} text-[10px]`}>{r.status.toLowerCase()}</span>
              </div>
              <div className="text-[11px] text-slate-500 dark:text-slate-400">{r.instrument}</div>
              <div className="text-[10px] text-slate-400 dark:text-slate-500 mt-0.5">
                {r.start_date?.slice(0, 10)} → {r.end_date?.slice(0, 10)}
              </div>
              {((r.status||'').toLowerCase() === 'running' || (r.status||'').toLowerCase() === 'queued' || (r.status||'').toLowerCase() === 'pending') && (
                <div className="mt-2">
                  <div className="flex items-center justify-between text-[10px] text-blue-700 dark:text-blue-300 mb-1">
                    <span className="font-bold">{(r.status||'').toLowerCase() === 'queued' || (r.status||'').toLowerCase() === 'pending' ? 'Queued' : 'Running'}</span>
                    <span className="font-mono font-bold">{(r.progress ?? 0).toFixed(0)}%</span>
                  </div>
                  <div className="h-1.5 bg-slate-200 dark:bg-slate-700 rounded-full overflow-hidden">
                    <div className="h-full bg-gradient-to-r from-blue-500 to-violet-600 transition-all duration-700" style={{ width: `${Math.max(3, r.progress ?? 0)}%` }}/>
                  </div>
                </div>
              )}
            </button>
          ))}
        </div>

        {/* Detail */}
        <div className="lg:col-span-3">
          {!selectedRun ? (
            <div className="text-center text-sm text-slate-400 dark:text-slate-500 py-24 border border-dashed rounded-2xl border-slate-300 dark:border-slate-700">
              Select a backtest on the left to view results, or click "New Backtest" to run one.
            </div>
          ) : (
            <div className="space-y-5">
              <div className="bg-slate-50 dark:bg-slate-900 dark:border-slate-700 rounded-xl border border-slate-200 px-5 py-3">
                <div className="flex items-center justify-between gap-4">
                  <div className="flex items-center gap-3 min-w-0">
                    <span className="font-bold text-slate-900 dark:text-slate-100 truncate">{selectedRun.strategy_name || 'Strategy'}</span>
                    <span className="text-slate-400 dark:text-slate-500">·</span>
                    <span className="text-sm text-slate-500 dark:text-slate-400">{selectedRun.instrument}</span>
                    <span className="text-slate-400 dark:text-slate-500">·</span>
                    <span className="text-sm text-slate-500 dark:text-slate-400">{selectedRun.start_date?.slice(0,10)} to {selectedRun.end_date?.slice(0,10)}</span>
                  </div>
                  <div className="flex items-center gap-2 flex-shrink-0">
                    {((selectedRun.status||'').toLowerCase() === 'running' || (selectedRun.status||'').toLowerCase() === 'queued' || (selectedRun.status||'').toLowerCase() === 'pending') && (
                      <span className="inline-block w-3 h-3 border-2 border-blue-600 dark:border-blue-400 border-r-transparent rounded-full animate-spin"/>
                    )}
                    <span className={`badge ${STATUS_COLORS[selectedRun.status] || 'badge-grey'}`}>{selectedRun.status.toLowerCase()}</span>
                    {((selectedRun.status||'').toLowerCase() === 'running' || (selectedRun.status||'').toLowerCase() === 'queued' || (selectedRun.status||'').toLowerCase() === 'pending') && (
                      <span className="font-mono font-extrabold text-sm text-blue-700 dark:text-blue-300 tabular-nums">
                        {(selectedRun.progress ?? 0).toFixed(0)}%
                      </span>
                    )}
                    <button onClick={() => deleteMutation.mutate(selectedRun.id)} className="p-1.5 rounded-lg text-slate-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20">
                      <Trash2 size={14}/>
                    </button>
                  </div>
                </div>
                {/* Inline progress bar — sits right under the status row */}
                {((selectedRun.status||'').toLowerCase() === 'running' || (selectedRun.status||'').toLowerCase() === 'queued' || (selectedRun.status||'').toLowerCase() === 'pending') && (
                  <div className="mt-3">
                    <div className="h-2 bg-slate-200 dark:bg-slate-700 rounded-full overflow-hidden">
                      <div className="h-full bg-gradient-to-r from-blue-500 to-violet-600 transition-all duration-700 ease-out"
                        style={{ width: `${Math.max(2, selectedRun.progress ?? 0)}%` }}/>
                    </div>
                    <div className="text-[10.5px] text-slate-500 dark:text-slate-400 mt-1.5">
                      {(selectedRun.progress ?? 0) < 20 ? 'Loading historical data' :
                       (selectedRun.progress ?? 0) < 40 ? 'Preparing the strategy engine' :
                       (selectedRun.progress ?? 0) < 80 ? 'Iterating bars and simulating trades' :
                       'Computing metrics + writing trades'}
                    </div>
                  </div>
                )}
              </div>

              {/* Loading + error states so the user knows something's happening */}
              {!metrics && metricsLoading && (selectedRun.status||'').toLowerCase() === 'completed' && (
                <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-6 text-center">
                  <div className="inline-block w-6 h-6 border-2 border-blue-500 border-r-transparent rounded-full animate-spin mb-2"/>
                  <div className="text-sm text-slate-600 dark:text-slate-300">Loading results...</div>
                </div>
              )}
              {!metrics && metricsError && (selectedRun.status||'').toLowerCase() === 'completed' && (
                <div className="rounded-2xl border border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30 p-5">
                  <div className="font-bold text-amber-800 dark:text-amber-200 mb-1">Metrics not ready yet</div>
                  <div className="text-xs text-amber-700 dark:text-amber-300 mb-3">The run finished but the metrics row wasn't written. This is rare — usually means the backtest had no trades or crashed at the very end.</div>
                  <button onClick={() => refetchMetrics()} className="bg-amber-600 hover:bg-amber-500 text-white text-xs font-bold px-3 py-1.5 rounded-lg">Retry</button>
                </div>
              )}

              {metrics && (
                <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
                  <MetricCard
                    label="Win Rate"
                    value={`${(metrics.win_rate * 100).toFixed(1)}%`}
                    sub={metrics.breakeven_trades > 0 ? `${metrics.breakeven_trades} BE counted as wins` : undefined}
                    color={(metrics.win_rate >= 0.5) ? 'text-green-600' : 'text-red-500'}
                  />
                  {metrics.breakeven_trades > 0 && (
                    <MetricCard
                      label="Effective WR"
                      value={`${(metrics.effective_win_rate * 100).toFixed(1)}%`}
                      sub="Strict view — excludes BE from wins"
                      color={metrics.effective_win_rate >= 0.6 ? 'text-green-600' : 'text-amber-600'}
                    />
                  )}
                  <MetricCard label="Profit Factor" value={metrics.profit_factor.toFixed(2)} color={metrics.profit_factor >= 1.5 ? 'text-green-600' : 'text-amber-600'}/>
                  <MetricCard label="Net Profit" value={`$${metrics.net_profit.toLocaleString()}`} color={metrics.net_profit >= 0 ? 'text-green-600' : 'text-red-500'}/>
                  <MetricCard label="Max Drawdown" value={`${metrics.max_drawdown_pct.toFixed(1)}%`} sub="Peak-to-trough" color="text-red-500"/>
                  <MetricCard
                    label="Total Trades"
                    value={String(metrics.total_trades)}
                    sub={metrics.breakeven_trades > 0 ? `${metrics.breakeven_trades} broke even` : undefined}
                  />
                  {metrics.breakeven_trades > 0 && (
                    <MetricCard
                      label="Break-Even"
                      value={String(metrics.breakeven_trades)}
                      sub="Stop moved to entry, exited flat"
                      color="text-amber-600"
                    />
                  )}
                  <MetricCard label="Avg R:R" value={metrics.avg_rr.toFixed(2)} color="text-blue-600"/>
                  {metrics.sharpe_ratio != null && (
                    <MetricCard label="Sharpe Ratio" value={metrics.sharpe_ratio.toFixed(2)} color={metrics.sharpe_ratio >= 1 ? 'text-green-600' : 'text-slate-500'}/>
                  )}
                </div>
              )}

              {/* "Optimize for better results?" — only shows on completed backtests */}
              {metrics && (selectedRun.status||'').toLowerCase() === 'completed' && (
                <OptimizePrompt selectedRun={selectedRun} baselineMetrics={metrics}/>
              )}

              {metrics?.equity_curve?.length > 0 && (
                <div className="bg-slate-50 rounded-xl border border-slate-200 p-5 dark:bg-slate-900 dark:border-slate-700">
                  <div className="text-sm font-semibold text-slate-700 mb-4 dark:text-slate-200">Equity Curve</div>
                  <ResponsiveContainer width="100%" height={240}>
                    <LineChart data={metrics.equity_curve}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} className="dark:opacity-20"/>
                      <XAxis dataKey="timestamp" tick={{ fill: '#94a3b8', fontSize: 10 }} tickLine={false} axisLine={false} interval="preserveStartEnd"/>
                      <YAxis tick={{ fill: '#94a3b8', fontSize: 10 }} tickLine={false} axisLine={false} tickFormatter={(v) => `$${(v / 1000).toFixed(0)}k`} width={56}/>
                      <Tooltip content={<CustomTooltip />}/>
                      <Line type="monotone" dataKey="equity" stroke="#2563eb" strokeWidth={2} dot={false} activeDot={{ r: 4, fill: '#2563eb' }}/>
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              )}

              {/* Monthly Returns — one row per month with P&L color-coded */}
              {metrics?.monthly_returns && Object.keys(metrics.monthly_returns).length > 0 && (() => {
                const months = Object.entries(metrics.monthly_returns as Record<string, number>)
                  .sort(([a], [b]) => a.localeCompare(b))
                const maxAbs = Math.max(1, ...months.map(([, v]) => Math.abs(v)))
                const MONTH_LABELS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
                return (
                  <div className="bg-slate-50 rounded-xl border border-slate-200 p-5 dark:bg-slate-900 dark:border-slate-700">
                    <div className="text-sm font-semibold text-slate-700 mb-4 dark:text-slate-200">Monthly Returns</div>
                    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2">
                      {months.map(([key, pnl]) => {
                        const [y, m] = key.split('-')
                        const label = `${MONTH_LABELS[parseInt(m) - 1]} ${y}`
                        const pct = Math.min(100, (Math.abs(pnl) / maxAbs) * 100)
                        const positive = pnl >= 0
                        return (
                          <div key={key} className="rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-3">
                            <div className="flex items-center justify-between mb-1.5">
                              <span className="text-[11px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">{label}</span>
                              <span className={`text-sm font-extrabold tabular-nums ${positive ? 'text-green-600' : 'text-red-500'}`}>
                                {positive ? '+' : '−'}${Math.abs(pnl).toLocaleString(undefined, {maximumFractionDigits: 0})}
                              </span>
                            </div>
                            <div className="h-1.5 bg-slate-100 dark:bg-slate-700 rounded-full overflow-hidden">
                              <div className={`h-full transition-all ${positive ? 'bg-green-500' : 'bg-red-500'}`} style={{ width: `${pct}%` }}/>
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  </div>
                )
              })()}
            </div>
          )}
        </div>
      </div>

      {showNew && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white dark:bg-slate-900 rounded-2xl shadow-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between px-6 py-4 border-b border-slate-200 dark:border-slate-700">
              <h2 className="font-bold text-slate-900 dark:text-slate-100">New Backtest</h2>
              <button onClick={() => setShowNew(false)} className="p-1.5 text-slate-400"><X size={16}/></button>
            </div>
            <div className="px-6 py-5 space-y-4">
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Strategy</label>
                <select value={form.strategy_id} onChange={e => setForm({...form, strategy_id: e.target.value})}
                  className="w-full border border-slate-300 dark:border-slate-700 dark:bg-slate-800 rounded-lg px-3.5 py-2.5 text-sm">
                  <option value="">Select a strategy...</option>
                  {strategies.map((s: any) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Instrument</label>
                <select value={form.instrument} onChange={e => setForm({...form, instrument: e.target.value})}
                  className="w-full border border-slate-300 dark:border-slate-700 dark:bg-slate-800 rounded-lg px-3.5 py-2.5 text-sm">
                  {['ES', 'NQ', 'RTY', 'YM'].map(i => <option key={i}>{i}</option>)}
                </select>
                <p className="text-[10.5px] text-slate-400 dark:text-slate-500 mt-1">Timeframes are taken from the strategy's own configuration (bias / setup / entry frames) — no need to pick them here.</p>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Start</label>
                  <input type="date" value={form.start_date} onChange={e => setForm({...form, start_date: e.target.value})}
                    className="w-full border border-slate-300 dark:border-slate-700 dark:bg-slate-800 rounded-lg px-3.5 py-2.5 text-sm"/>
                </div>
                <div>
                  <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">End</label>
                  <input type="date" value={form.end_date} onChange={e => setForm({...form, end_date: e.target.value})}
                    className="w-full border border-slate-300 dark:border-slate-700 dark:bg-slate-800 rounded-lg px-3.5 py-2.5 text-sm"/>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Capital</label>
                  <input type="number" value={form.initial_capital} onChange={e => setForm({...form, initial_capital: +e.target.value})}
                    className="w-full border border-slate-300 dark:border-slate-700 dark:bg-slate-800 rounded-lg px-3.5 py-2.5 text-sm"/>
                </div>
                <div>
                  <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Risk Per Trade %</label>
                  <input type="number" step="0.1" value={form.risk_per_trade_pct} onChange={e => setForm({...form, risk_per_trade_pct: +e.target.value})}
                    className="w-full border border-slate-300 dark:border-slate-700 dark:bg-slate-800 rounded-lg px-3.5 py-2.5 text-sm"/>
                </div>
              </div>
            </div>
            <div className="flex gap-3 px-6 py-4 border-t border-slate-200 dark:border-slate-700">
              <button onClick={() => setShowNew(false)} className="flex-1 border border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-300 py-2.5 rounded-xl text-sm font-medium">Cancel</button>
              <button onClick={() => runMutation.mutate()} disabled={!form.strategy_id || runMutation.isPending}
                className="flex-1 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white py-2.5 rounded-xl text-sm font-semibold">
                {runMutation.isPending ? 'Starting…' : 'Run Backtest'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
