import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { LineChart, Line, XAxis, YAxis, ResponsiveContainer, CartesianGrid, Tooltip } from 'recharts'
import { Play, Trash2, FlaskConical, X } from 'lucide-react'
import { strategiesApi, backtestsApi } from '../api/endpoints'

const STATUS_COLORS: Record<string, string> = {
  completed: 'badge-green',
  running: 'badge-amber',
  failed: 'badge-red',
  queued: 'badge-grey',
  cancelled: 'bg-slate-100 text-slate-500'}

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
  const { data: runs = [] }       = useQuery({ queryKey: ['backtests'], queryFn: () => backtestsApi.list().then(r => r.data), refetchInterval: (q: any) => ((q?.state?.data as any[]) || []).some((r: any) => r?.status === 'running' || r?.status === 'queued') ? 2000 : false })
  const { data: metrics }: any    = useQuery({
    queryKey: ['backtest-metrics', selected],
    queryFn: () => backtestsApi.getMetrics(selected!).then(r => r.data),
    enabled: !!selected,
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
        status: 'PENDING',
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
              {(r.status === 'RUNNING' || r.status === 'PENDING') && (
                <div className="mt-2">
                  <div className="flex items-center justify-between text-[10px] text-blue-700 dark:text-blue-300 mb-1">
                    <span className="font-bold">{r.status === 'PENDING' ? 'Queued' : 'Running'}</span>
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
                    {(selectedRun.status === 'RUNNING' || selectedRun.status === 'PENDING') && (
                      <span className="inline-block w-3 h-3 border-2 border-blue-600 dark:border-blue-400 border-r-transparent rounded-full animate-spin"/>
                    )}
                    <span className={`badge ${STATUS_COLORS[selectedRun.status] || 'badge-grey'}`}>{selectedRun.status.toLowerCase()}</span>
                    {(selectedRun.status === 'RUNNING' || selectedRun.status === 'PENDING') && (
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
                {(selectedRun.status === 'RUNNING' || selectedRun.status === 'PENDING') && (
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
