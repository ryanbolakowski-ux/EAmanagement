import { useState } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { optimizationApi, strategiesApi } from '../api/endpoints'
import { Sliders, X, Trophy, TrendingUp } from 'lucide-react'

export default function Optimization() {
  const [runId, setRunId]       = useState<string | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState({
    strategy_id: '', instrument: 'ES',
    start_date: '2022-01-01T00:00:00Z', end_date: '2024-01-01T00:00:00Z',
    optimization_metric: 'profit_factor',
  })

  const { data: strategies = [] } = useQuery({ queryKey: ['strategies'], queryFn: () => strategiesApi.list().then(r => r.data) })
  const { data: results = [] }    = useQuery({
    queryKey: ['opt-results', runId],
    queryFn: () => optimizationApi.getResults(runId!).then(r => r.data),
    enabled: !!runId,
  })

  const runMutation = useMutation({
    mutationFn: () => optimizationApi.start({
      ...form,
      parameter_grid: {
        risk_reward_ratio: [1.5, 2.0, 2.5, 3.0],
        stop_loss_ticks: [8, 10, 12, 16],
        fvg_min_size_ticks: [2, 4, 6],
      },
    }),
    onSuccess: (res: any) => { setRunId(res.data.id); setShowForm(false) },
  })

  const METRICS = [
    { value: 'profit_factor', label: 'Profit Factor' },
    { value: 'net_profit',    label: 'Net Profit' },
    { value: 'win_rate',      label: 'Win Rate' },
    { value: 'sharpe_ratio',  label: 'Sharpe Ratio' },
  ]

  return (
    <div className="p-8 max-w-6xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-extrabold text-slate-900">Optimization</h1>
          <p className="text-slate-500 text-sm mt-1">Auto-tune strategy parameters across hundreds of combinations</p>
        </div>
        <button onClick={() => setShowForm(true)}
          className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2.5 rounded-xl text-sm font-semibold transition-colors shadow-sm shadow-blue-200">
          <Sliders size={14}/> Run Optimization
        </button>
      </div>

      {/* Parameter grid info */}
      {!runId && (
        <div className="grid md:grid-cols-3 gap-4 mb-8">
          {[
            { label: 'Risk:Reward', values: '1.5 · 2.0 · 2.5 · 3.0', count: 4 },
            { label: 'Stop Loss (ticks)', values: '8 · 10 · 12 · 16', count: 4 },
            { label: 'FVG Min Size', values: '2 · 4 · 6 ticks', count: 3 },
          ].map(({ label, values, count }) => (
            <div key={label} className="bg-white rounded-xl border border-slate-200 p-4">
              <div className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-2">{label}</div>
              <div className="text-sm font-medium text-slate-700">{values}</div>
              <div className="text-xs text-slate-400 mt-1">{count} values</div>
            </div>
          ))}
        </div>
      )}

      {results.length > 0 ? (
        <div>
          <div className="flex items-center gap-2 mb-4">
            <Trophy size={16} className="text-amber-500"/>
            <h2 className="text-base font-bold text-slate-900">Top Results</h2>
            <span className="badge badge-grey">{results.length} combinations</span>
          </div>

          <div className="bg-white rounded-2xl border border-slate-200 overflow-hidden shadow-sm">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-slate-50 border-b border-slate-200">
                  {['Rank', 'R:R', 'SL Ticks', 'FVG Min', 'Net Profit', 'Profit Factor', 'Win Rate', 'Max DD', 'Trades'].map(h => (
                    <th key={h} className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {results.map((r: any) => (
                  <tr key={r.rank} className={`hover:bg-slate-50 transition-colors ${r.rank === 1 ? 'bg-amber-50/50' : ''}`}>
                    <td className="px-4 py-3.5">
                      <span className={`font-bold text-sm ${r.rank === 1 ? 'text-amber-600' : r.rank <= 3 ? 'text-blue-600' : 'text-slate-400'}`}>
                        {r.rank === 1 ? '🥇' : r.rank === 2 ? '🥈' : r.rank === 3 ? '🥉' : `#${r.rank}`}
                      </span>
                    </td>
                    <td className="px-4 py-3.5 font-semibold text-slate-800">{r.parameters.risk_reward_ratio}:1</td>
                    <td className="px-4 py-3.5 text-slate-600">{r.parameters.stop_loss_ticks}</td>
                    <td className="px-4 py-3.5 text-slate-600">{r.parameters.fvg_min_size_ticks}</td>
                    <td className={`px-4 py-3.5 font-semibold ${r.net_profit >= 0 ? 'text-green-600' : 'text-red-500'}`}>
                      {r.net_profit >= 0 ? '+' : ''}${r.net_profit.toLocaleString()}
                    </td>
                    <td className={`px-4 py-3.5 font-semibold ${r.profit_factor >= 1.5 ? 'text-green-600' : 'text-amber-600'}`}>{r.profit_factor.toFixed(2)}</td>
                    <td className={`px-4 py-3.5 ${r.win_rate >= 0.5 ? 'text-green-600' : 'text-slate-500'}`}>{(r.win_rate * 100).toFixed(1)}%</td>
                    <td className="px-4 py-3.5 text-red-500">{r.max_drawdown.toFixed(1)}%</td>
                    <td className="px-4 py-3.5 text-slate-500">{r.total_trades}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <div className="bg-white rounded-2xl border border-dashed border-slate-200 p-16 text-center">
          <div className="w-14 h-14 bg-blue-50 rounded-2xl flex items-center justify-center mx-auto mb-5">
            <Sliders size={24} className="text-blue-500"/>
          </div>
          <p className="font-semibold text-slate-700 mb-1">No optimization runs yet</p>
          <p className="text-sm text-slate-400 mb-5">Run an optimization to find the best parameter combinations for your strategy</p>
          <button onClick={() => setShowForm(true)}
            className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2.5 rounded-xl text-sm font-semibold transition-colors">
            <Sliders size={14}/> Start Optimization
          </button>
        </div>
      )}

      {/* Modal */}
      {showForm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md">
            <div className="flex items-center justify-between px-6 py-5 border-b border-slate-100">
              <h2 className="text-base font-bold text-slate-900">Configure Optimization</h2>
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
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Optimize For</label>
                <div className="grid grid-cols-2 gap-2">
                  {METRICS.map(m => (
                    <button key={m.value} type="button" onClick={() => setForm({...form, optimization_metric: m.value})}
                      className={`px-3 py-2.5 rounded-lg text-xs font-semibold border transition-all ${
                        form.optimization_metric === m.value
                          ? 'bg-blue-600 border-blue-600 text-white' : 'bg-white border-slate-200 text-slate-600 hover:border-slate-300'
                      }`}>
                      {m.label}
                    </button>
                  ))}
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
              <div className="bg-blue-50 rounded-xl p-3 text-xs text-blue-700">
                Will test <strong>48 combinations</strong>: RR [1.5, 2.0, 2.5, 3.0] × SL [8, 10, 12, 16] × FVG [2, 4, 6]
              </div>
            </div>
            <div className="flex gap-3 px-6 py-4 border-t border-slate-100">
              <button onClick={() => setShowForm(false)} className="flex-1 border border-slate-200 text-slate-600 py-2.5 rounded-xl text-sm font-medium">Cancel</button>
              <button onClick={() => runMutation.mutate()} disabled={!form.strategy_id || runMutation.isPending}
                className="flex-1 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white py-2.5 rounded-xl text-sm font-semibold transition-colors">
                Start Optimization
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
