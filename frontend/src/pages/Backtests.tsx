import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { backtestsApi, strategiesApi } from '../api/endpoints'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, ReferenceLine } from 'recharts'
import { FlaskConical, Play, X, TrendingUp, TrendingDown, AlertTriangle, Activity } from 'lucide-react'

const STATUS_STYLE: Record<string, string> = {
  completed: 'badge badge-green',
  running:   'badge badge-blue',
  queued:    'badge badge-grey',
  failed:    'badge badge-red',
}

function MetricCard({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-4">
      <div className="text-xs text-slate-400 font-medium uppercase tracking-wider mb-2">{label}</div>
      <div className={`text-xl font-extrabold ${color ?? 'text-slate-900'}`}>{value}</div>
      {sub && <div className="text-xs text-slate-400 mt-0.5">{sub}</div>}
    </div>
  )
}

const CustomTooltip = ({ active, payload, label }: any) => {
  if (active && payload?.length) {
    return (
      <div className="bg-white border border-slate-200 rounded-lg shadow-lg px-3 py-2 text-xs">
        <div className="text-slate-400 mb-1">{label}</div>
        <div className="font-bold text-slate-900">${payload[0].value?.toLocaleString()}</div>
      </div>
    )
  }
  return null
}

export default function Backtests() {
  const qc = useQueryClient()
  const [showForm, setShowForm]     = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [form, setForm] = useState({
    strategy_id: '', instrument: 'ES',
    start_date: '2022-01-01T00:00:00Z', end_date: '2024-01-01T00:00:00Z',
    timeframe: '15m', initial_capital: 100000,
    commission_per_side: 2.25, slippage_ticks: 1,
  })

  const { data: runs = [], isLoading }  = useQuery({ queryKey: ['backtests'], queryFn: () => backtestsApi.list().then(r => r.data) })
  const { data: strategies = [] }       = useQuery({ queryKey: ['strategies'], queryFn: () => strategiesApi.list().then(r => r.data) })
  const { data: metrics, isLoading: mLoading } = useQuery({
    queryKey: ['backtest-metrics', selectedId],
    queryFn: () => backtestsApi.getMetrics(selectedId!).then(r => r.data),
    enabled: !!selectedId,
  })

  const runMutation = useMutation({
    mutationFn: () => backtestsApi.run(form),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['backtests'] }); setShowForm(false) },
  })

  const selectedRun = runs.find(r => r.id === selectedId)

  return (
    <div className="p-8 max-w-7xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-extrabold text-slate-900">Backtests</h1>
          <p className="text-slate-500 text-sm mt-1">Test strategies against historical ES & NQ data</p>
        </div>
        <button onClick={() => setShowForm(true)}
          className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2.5 rounded-xl text-sm font-semibold transition-colors shadow-sm shadow-blue-200">
          <Play size={14}/> New Backtest
        </button>
      </div>

      <div className="grid lg:grid-cols-3 gap-6">
        {/* Run list */}
        <div className="lg:col-span-1 space-y-2">
          {isLoading ? (
            [...Array(4)].map((_, i) => <div key={i} className="bg-white rounded-xl border border-slate-200 h-16 animate-pulse"/>)
          ) : runs.length === 0 ? (
            <div className="bg-white rounded-2xl border border-dashed border-slate-200 p-10 text-center">
              <FlaskConical size={30} className="mx-auto text-slate-300 mb-3"/>
              <p className="text-sm text-slate-400">No backtests yet</p>
            </div>
          ) : (
            runs.map(r => (
              <button key={r.id} onClick={() => setSelectedId(r.id)}
                className={`w-full text-left bg-white rounded-xl border px-4 py-3.5 transition-all hover:shadow-sm ${
                  selectedId === r.id ? 'border-blue-500 shadow-sm shadow-blue-100' : 'border-slate-200'
                }`}>
                <div className="flex items-center justify-between mb-1">
                  <span className="font-semibold text-slate-900 text-sm">{r.instrument}</span>
                  <span className={STATUS_STYLE[r.status] ?? 'badge badge-grey'}>{r.status}</span>
                </div>
                <div className="text-xs text-slate-400">
                  {r.start_date.slice(0, 10)} → {r.end_date.slice(0, 10)}
                </div>
              </button>
            ))
          )}
        </div>

        {/* Metrics panel */}
        <div className="lg:col-span-2">
          {!selectedId ? (
            <div className="bg-white rounded-2xl border border-slate-200 p-14 text-center h-full flex flex-col items-center justify-center">
              <Activity size={36} className="text-slate-200 mb-4"/>
              <p className="font-semibold text-slate-400">Select a completed backtest</p>
              <p className="text-sm text-slate-300 mt-1">Results and equity curve will appear here</p>
            </div>
          ) : mLoading ? (
            <div className="space-y-4">
              <div className="grid grid-cols-3 gap-3">
                {[...Array(6)].map((_, i) => <div key={i} className="bg-white rounded-xl border border-slate-200 h-20 animate-pulse"/>)}
              </div>
            </div>
          ) : metrics ? (
            <div className="space-y-4">
              {/* Info strip */}
              <div className="bg-slate-50 rounded-xl border border-slate-200 px-4 py-3 flex items-center gap-4 text-xs text-slate-500">
                <span className="font-semibold text-slate-700">{selectedRun?.instrument}</span>
                <span>·</span>
                <span>{selectedRun?.start_date.slice(0, 10)} to {selectedRun?.end_date.slice(0, 10)}</span>
                <span className={`${STATUS_STYLE[selectedRun?.status ?? '']} ml-auto`}>{selectedRun?.status}</span>
              </div>

              {/* Metric grid */}
              <div className="grid grid-cols-3 gap-3">
                <MetricCard label="Win Rate" value={`${(metrics.win_rate * 100).toFixed(1)}%`} color={(metrics.win_rate >= 0.5) ? 'text-green-600' : 'text-red-500'}/>
                <MetricCard label="Profit Factor" value={metrics.profit_factor.toFixed(2)} color={metrics.profit_factor >= 1.5 ? 'text-green-600' : 'text-amber-600'}/>
                <MetricCard label="Net Profit" value={`$${metrics.net_profit.toLocaleString()}`} color={metrics.net_profit >= 0 ? 'text-green-600' : 'text-red-500'}/>
                <MetricCard label="Max Drawdown" value={`${metrics.max_drawdown_pct.toFixed(1)}%`} sub="Peak-to-trough" color="text-red-500"/>
                <MetricCard label="Total Trades" value={String(metrics.total_trades)}/>
                <MetricCard label="Avg R:R" value={metrics.avg_rr.toFixed(2)} color="text-blue-600"/>
                {metrics.sharpe_ratio != null && <MetricCard label="Sharpe Ratio" value={metrics.sharpe_ratio.toFixed(2)} color={metrics.sharpe_ratio >= 1 ? 'text-green-600' : 'text-slate-500'}/>}
              </div>

              {/* Equity curve */}
              {metrics.equity_curve.length > 0 && (
                <div className="bg-white rounded-xl border border-slate-200 p-5">
                  <div className="text-sm font-semibold text-slate-700 mb-4">Equity Curve</div>
                  <ResponsiveContainer width="100%" height={200}>
                    <LineChart data={metrics.equity_curve}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" vertical={false}/>
                      <XAxis dataKey="timestamp" tick={{ fill: '#94a3b8', fontSize: 10 }} tickLine={false} axisLine={false} interval="preserveStartEnd"/>
                      <YAxis tick={{ fill: '#94a3b8', fontSize: 10 }} tickLine={false} axisLine={false} tickFormatter={v => `$${(v/1000).toFixed(0)}k`}/>
                      <Tooltip content={<CustomTooltip/>}/>
                      <Line type="monotone" dataKey="equity" stroke="#2563eb" strokeWidth={2} dot={false} activeDot={{ r: 4, fill: '#2563eb' }}/>
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              )}

              {/* Monthly returns */}
              {Object.keys(metrics.monthly_returns).length > 0 && (
                <div className="bg-white rounded-xl border border-slate-200 p-5">
                  <div className="text-sm font-semibold text-slate-700 mb-3">Monthly Returns</div>
                  <div className="flex flex-wrap gap-2">
                    {Object.entries(metrics.monthly_returns).slice(-12).map(([month, pnl]) => (
                      <div key={month} className={`text-center px-3 py-2 rounded-lg text-xs ${(pnl as number) >= 0 ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-600'}`}>
                        <div className="font-semibold">{(pnl as number) >= 0 ? '+' : ''}${(pnl as number).toFixed(0)}</div>
                        <div className="text-[10px] opacity-70">{month}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className="bg-amber-50 border border-amber-200 rounded-xl p-6 flex items-center gap-3">
              <AlertTriangle size={18} className="text-amber-500 flex-shrink-0"/>
              <p className="text-sm text-amber-700">This backtest hasn't completed yet or has no metrics available.</p>
            </div>
          )}
        </div>
      </div>

      {/* Modal */}
      {showForm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md">
            <div className="flex items-center justify-between px-6 py-5 border-b border-slate-100">
              <h2 className="text-base font-bold text-slate-900">Configure Backtest</h2>
              <button onClick={() => setShowForm(false)} className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400"><X size={16}/></button>
            </div>
            <div className="px-6 py-5 space-y-4">
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Strategy</label>
                <select value={form.strategy_id} onChange={e => setForm({...form, strategy_id: e.target.value})}
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                  <option value="">Select a strategy...</option>
                  {strategies.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Instrument</label>
                  <select value={form.instrument} onChange={e => setForm({...form, instrument: e.target.value})}
                    className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                    {['ES', 'NQ'].map(i => <option key={i}>{i}</option>)}
                  </select>
                </div>
                <div>
                  <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Timeframe</label>
                  <select value={form.timeframe} onChange={e => setForm({...form, timeframe: e.target.value})}
                    className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                    {['1m', '5m', '15m', '1H', '4H'].map(tf => <option key={tf}>{tf}</option>)}
                  </select>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Start Date</label>
                  <input type="date" value={form.start_date.slice(0, 10)} onChange={e => setForm({...form, start_date: e.target.value + 'T00:00:00Z'})}
                    className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"/>
                </div>
                <div>
                  <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">End Date</label>
                  <input type="date" value={form.end_date.slice(0, 10)} onChange={e => setForm({...form, end_date: e.target.value + 'T00:00:00Z'})}
                    className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"/>
                </div>
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Initial Capital ($)</label>
                <input type="number" value={form.initial_capital} onChange={e => setForm({...form, initial_capital: parseFloat(e.target.value)})}
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"/>
              </div>
            </div>
            <div className="flex gap-3 px-6 py-4 border-t border-slate-100">
              <button onClick={() => setShowForm(false)} className="flex-1 border border-slate-200 text-slate-600 py-2.5 rounded-xl text-sm font-medium">Cancel</button>
              <button onClick={() => runMutation.mutate()} disabled={!form.strategy_id || runMutation.isPending}
                className="flex-1 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white py-2.5 rounded-xl text-sm font-semibold transition-colors">
                {runMutation.isPending ? 'Submitting...' : 'Run Backtest'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
