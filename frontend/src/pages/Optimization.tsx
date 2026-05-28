import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { optimizationApi, strategiesApi, DEFAULT_OPT_GRID } from '../api/endpoints'
import { Sliders, X, Trophy, TrendingUp, Trash2 } from 'lucide-react'

export default function Optimization() {
  const qc = useQueryClient()
  const [runId, setRunId]       = useState<string | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [polling, setPolling]   = useState(false)
  const [form, setForm] = useState({
    strategy_id: '', instrument: 'ES',
    lookback: '6',
    optimization_metric: 'profit_factor',
  })

  const { data: strategies = [], isLoading: stratsLoading, isError: stratsError } = useQuery({ queryKey: ['strategies'], queryFn: () => strategiesApi.list().then(r => r.data) })
  const { data: runs = [] } = useQuery({
    queryKey: ['opt-runs'],
    queryFn: () => optimizationApi.list().then(r => r.data),
    refetchInterval: polling ? 3000 : false,
  })
  const { data: results = [] }    = useQuery({
    queryKey: ['opt-results', runId],
    queryFn: () => optimizationApi.getResults(runId!).then(r => r.data),
    enabled: !!runId,
    refetchInterval: polling ? 5000 : false,
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => optimizationApi.delete(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['opt-runs'] }); if (runId) setRunId(null); },
  })

  const runMutation = useMutation({
    mutationFn: () => optimizationApi.start({
      ...{strategy_id: form.strategy_id, instrument: form.instrument, optimization_metric: form.optimization_metric, start_date: new Date(Date.now() - parseInt(form.lookback) * 30 * 24 * 60 * 60 * 1000).toISOString(), end_date: new Date().toISOString()},
      parameter_grid: DEFAULT_OPT_GRID,
    }),
    onSuccess: (res: any) => { setRunId(res.data.id); setShowForm(false); setPolling(true) },
  })

  const applyMutation = useMutation({
    mutationFn: (params: {runId: string, rank: number}) => optimizationApi.apply(params.runId, params.rank),
    onSuccess: () => { alert('Strategy updated with optimized parameters!'); qc.invalidateQueries({ queryKey: ['strategies'] }); },
    onError: (e: any) => alert(e?.response?.data?.detail || 'Failed to apply'),
  })

  const retryMutation = useMutation({
    mutationFn: (id: string) => optimizationApi.retry(id),
    onSuccess: (res: any) => { setRunId(res.data.id); setPolling(true); qc.invalidateQueries({ queryKey: ['opt-runs'] }) },
    onError: (e: any) => alert(e?.response?.data?.detail || 'Retry failed'),
  })

  const selectedRun: any = runs.find((r: any) => r.id === runId) || null
  const activeStrategies = strategies.filter((s: any) => (s.status || 'active') === 'active')
  const draftStrategies = strategies.filter((s: any) => s.status === 'draft')
  const selectedStrategyActive = activeStrategies.some((s: any) => s.id === form.strategy_id)

  const METRICS = [
    { value: 'profit_factor', label: 'Profit Factor' },
    { value: 'net_profit',    label: 'Net Profit' },
    { value: 'win_rate',      label: 'Win Rate' },
    { value: 'sharpe_ratio',  label: 'Sharpe Ratio' },
  ]

  return (
    <div className="space-y-6 max-w-7xl mx-auto px-4 sm:px-6 py-6">
      {/* HERO */}
      <div className="rounded-3xl bg-gradient-to-br from-slate-900 via-slate-900 to-fuchsia-950 dark:from-slate-950 dark:via-slate-950 dark:to-fuchsia-950 text-white p-6 md:p-8 shadow-xl">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="min-w-0 flex-1">
            <div className="text-[10px] uppercase tracking-[0.2em] text-fuchsia-300 font-bold mb-1">Research</div>
            <h1 className="text-2xl md:text-3xl font-extrabold text-white flex items-center gap-2.5">
              <Sliders size={26}/> Optimization
            </h1>
            <p className="text-sm text-slate-400 mt-1">Auto-tune strategy parameters across hundreds of combinations · find the best Risk:Reward / Stop / FVG-size combo</p>
          </div>
          <button onClick={() => setShowForm(true)}
            className="inline-flex items-center gap-2 bg-fuchsia-500 hover:bg-fuchsia-400 text-white px-4 py-2 rounded-xl text-sm font-bold transition-colors shadow-lg shadow-fuchsia-900/30">
            <Sliders size={14}/> Run Optimization
          </button>
        </div>
      </div>

      {/* Parameter grid info */}
      {!runId && (
        <div className="grid md:grid-cols-3 gap-4 mb-8">
          {[
            { label: 'Risk:Reward', values: '1.5 · 2.0 · 2.5 · 3.0', count: 4 },
            { label: 'Stop Loss (ticks)', values: '8 · 10 · 12 · 16', count: 4 },
            { label: 'FVG Min Size', values: '2 · 4 · 6 ticks', count: 3 },
          ].map(({ label, values, count }) => (
            <div key={label} className="bg-slate-50 rounded-xl border border-slate-200 p-4 dark:bg-slate-900 dark:border-slate-700">
              <div className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-2 dark:text-slate-400">{label}</div>
              <div className="text-sm font-medium text-slate-700 dark:text-slate-200">{values}</div>
              <div className="text-xs text-slate-400 mt-1 dark:text-slate-500">{count} values</div>
            </div>
          ))}
        </div>
      )}

      {/* Past Optimization Runs */}
      {runs.length > 0 && (
        <div className="mb-6">
          <h2 className="text-base font-bold text-slate-900 mb-3 dark:text-slate-100">Optimization Runs</h2>
          <div className="space-y-2">
            {runs.map((r: any) => (
              <div key={r.id} onClick={() => { setRunId(r.id); if (r.status === 'running' || r.status === 'queued') setPolling(true); else setPolling(false); }}
                className={`flex items-center justify-between p-4 rounded-xl border cursor-pointer transition-all ${ runId === r.id ? 'border-blue-300 bg-blue-50' : 'border-slate-200 bg-slate-50 hover:border-slate-300' } dark:bg-slate-900`}>
                <div className="flex items-center gap-3">
                  <div className={`w-2.5 h-2.5 rounded-full flex-shrink-0 ${ r.status === 'completed' ? 'bg-green-500' : r.status === 'running' ? 'bg-amber-500 animate-pulse' : r.status === 'failed' ? 'bg-red-500' : 'bg-slate-300' }`}/>
                  <div className="min-w-0">
                    <div className="text-sm font-semibold text-slate-900 truncate dark:text-slate-100">{r.strategy_name || 'Strategy'}</div>
                    <div className="text-[11px] text-slate-500 mt-0.5 dark:text-slate-400">{r.instrument} · {r.total_combinations} combos</div>
                    <div className="text-xs text-slate-400 mt-0.5 dark:text-slate-500">{new Date(r.created_at).toLocaleString()} · {r.status}</div>
                    {r.status === 'failed' && (r.failure_reason || r.error_message) && (
                      <div className="text-[11px] text-red-500 mt-0.5 truncate max-w-xs" title={r.failure_reason || r.error_message}>⚠ {r.failure_reason || r.error_message}</div>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  {r.status === 'failed' && (
                    <button onClick={(e) => { e.stopPropagation(); retryMutation.mutate(r.id); }} disabled={retryMutation.isPending}
                      className="text-xs font-semibold text-blue-600 hover:underline disabled:opacity-50">Retry</button>
                  )}
                  <div className="text-right">
                    <div className="text-sm font-medium text-slate-700 dark:text-slate-200">{r.completed_combinations}/{r.total_combinations}</div>
                    <div className="text-xs text-slate-400 dark:text-slate-500">completed</div>
                  </div>
                  <button onClick={(e) => { e.stopPropagation(); deleteMutation.mutate(r.id); }}
                    className="text-slate-300 hover:text-red-500 transition-colors dark:text-slate-600">
                    <Trash2 size={14}/>
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {runs.length === 0 ? (
        <div className="bg-slate-50 rounded-2xl border border-dashed border-slate-200 p-16 text-center dark:bg-slate-900 dark:border-slate-700">
          <div className="w-14 h-14 bg-blue-50 dark:bg-blue-900/20 rounded-2xl flex items-center justify-center mx-auto mb-5">
            <Sliders size={24} className="text-blue-500"/>
          </div>
          <p className="font-semibold text-slate-700 mb-1 dark:text-slate-200">No optimization runs yet</p>
          <p className="text-sm text-slate-400 mb-5 dark:text-slate-500">Run an optimization to find the best parameter combinations for your strategy</p>
          <button onClick={() => setShowForm(true)} className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2.5 rounded-xl text-sm font-semibold transition-colors">
            <Sliders size={14}/> Start Optimization
          </button>
        </div>
      ) : selectedRun && selectedRun.status === 'failed' ? (
        <div className="bg-red-50 dark:bg-red-900/20 rounded-2xl border border-red-200 dark:border-red-800 p-8 text-center">
          <p className="font-semibold text-red-700 dark:text-red-300 mb-1">This optimization run failed</p>
          <p className="text-sm text-red-600 dark:text-red-400 mb-1">{selectedRun.failure_reason || selectedRun.error_message || 'Unknown error'}</p>
          <p className="text-xs text-slate-500 mb-4">{selectedRun.completed_combinations}/{selectedRun.total_combinations} combinations completed before failure. Partial results are not available.</p>
          <button onClick={() => retryMutation.mutate(selectedRun.id)} disabled={retryMutation.isPending}
            className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white px-4 py-2.5 rounded-xl text-sm font-semibold">
            {retryMutation.isPending ? 'Retrying…' : 'Retry run'}</button>
        </div>
      ) : selectedRun && (selectedRun.status === 'running' || selectedRun.status === 'queued') ? (
        <div className="bg-amber-50 dark:bg-amber-900/20 rounded-2xl border border-amber-200 dark:border-amber-800 p-8 text-center">
          <p className="font-semibold text-amber-700 dark:text-amber-300 mb-1">Optimization {selectedRun.status}…</p>
          <p className="text-sm text-amber-600 dark:text-amber-400">{selectedRun.completed_combinations}/{selectedRun.total_combinations} combinations · {Math.round(selectedRun.progress || 0)}%</p>
        </div>
      ) : results.length > 0 ? (
        <div>
          <div className="flex items-center gap-2 mb-4">
            <Trophy size={16} className="text-amber-500"/>
            <h2 className="text-base font-bold text-slate-900 dark:text-slate-100">Top Results</h2>
            <span className="badge badge-grey">{results.length} combinations</span>
          </div>

          <div className="bg-slate-50 rounded-2xl border border-slate-200 overflow-hidden shadow-sm dark:bg-slate-900 dark:border-slate-700">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-slate-100 border-b border-slate-200 dark:bg-slate-800 dark:border-slate-700">
                  {['Rank', 'R:R', 'SL Ticks', 'FVG Min', 'Net Profit', 'Profit Factor', 'Win Rate', 'Max DD', 'Trades', ''].map(h => (
                    <th key={h} className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider whitespace-nowrap dark:text-slate-400">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {results.map((r: any) => (
                  <tr key={r.rank} className={`hover:bg-slate-100 transition-colors ${r.rank === 1 ? 'bg-amber-50 dark:bg-amber-900/20/50' : ''} dark:hover:bg-slate-800`}>
                    <td className="px-4 py-3.5">
                      <span className={`font-bold text-sm ${r.rank === 1 ? 'text-amber-600' : r.rank <= 3 ? 'text-blue-600' : 'text-slate-400'}`}>
                        {r.rank === 1 ? '🥇' : r.rank === 2 ? '🥈' : r.rank === 3 ? '🥉' : `#${r.rank}`}
                      </span>
                    </td>
                    <td className="px-4 py-3.5 font-semibold text-slate-800 dark:text-slate-100">{r.parameters.risk_reward_ratio}:1</td>
                    <td className="px-4 py-3.5 text-slate-600 dark:text-slate-300">{r.parameters.stop_loss_ticks}</td>
                    <td className="px-4 py-3.5 text-slate-600 dark:text-slate-300">{r.parameters.fvg_min_size_ticks}</td>
                    <td className={`px-4 py-3.5 font-semibold ${r.net_profit >= 0 ? 'text-green-600' : 'text-red-500'}`}>
                      {r.net_profit >= 0 ? '+' : ''}${r.net_profit.toLocaleString()}
                    </td>
                    <td className={`px-4 py-3.5 font-semibold ${r.profit_factor >= 1.5 ? 'text-green-600' : 'text-amber-600'}`}>{r.profit_factor.toFixed(2)}</td>
                    <td className={`px-4 py-3.5 ${r.win_rate >= 0.5 ? 'text-green-600' : 'text-slate-500'}`}>{(r.win_rate * 100).toFixed(1)}%</td>
                    <td className="px-4 py-3.5 text-red-500">{r.max_drawdown.toFixed(1)}%</td>
                    <td className="px-4 py-3.5 text-slate-500 dark:text-slate-400">{r.total_trades}</td>
                    <td className="px-4 py-3.5">
                      <button onClick={() => runId && applyMutation.mutate({runId, rank: r.rank})}
                        className="px-3 py-1.5 bg-green-600 hover:bg-green-700 text-white rounded-lg text-xs font-semibold transition-colors">
                        Apply
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <div className="bg-slate-50 rounded-2xl border border-dashed border-slate-200 p-12 text-center dark:bg-slate-900 dark:border-slate-700">
          <p className="text-sm text-slate-500 dark:text-slate-400">Select a run above to view its results.</p>
        </div>
      )}

      {/* Modal */}
      {showForm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-slate-50 rounded-2xl shadow-2xl w-full max-w-md dark:bg-slate-900">
            <div className="flex items-center justify-between px-6 py-5 border-b border-slate-100 dark:border-slate-800">
              <h2 className="text-base font-bold text-slate-900 dark:text-slate-100">Configure Optimization</h2>
              <button onClick={() => setShowForm(false)} className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400 dark:text-slate-500 dark:hover:bg-slate-800"><X size={16}/></button>
            </div>
            <div className="px-6 py-5 space-y-4">
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Strategy</label>
                <select value={form.strategy_id} onChange={e => setForm({...form, strategy_id: e.target.value})}
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700">
                  <option value="">Select a strategy...</option>
                  {activeStrategies.map((s: any) => <option key={s.id} value={s.id}>{s.name}</option>)}
                  {draftStrategies.map((s: any) => <option key={s.id} value={s.id} disabled>{s.name} (draft — activate first)</option>)}
                </select>
                {stratsLoading && <p className="text-xs text-slate-400 mt-1">Loading strategies…</p>}
                {stratsError && <p className="text-xs text-red-500 mt-1">Could not load strategies. Try again.</p>}
                {!stratsLoading && !stratsError && strategies.length === 0 && <p className="text-xs text-slate-400 mt-1">No strategies yet — create one first.</p>}
                {!stratsLoading && !stratsError && strategies.length > 0 && activeStrategies.length === 0 && <p className="text-xs text-amber-600 mt-1">Draft strategies must be activated before optimization.</p>}
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Optimize For</label>
                <div className="grid grid-cols-2 gap-2">
                  {METRICS.map(m => (
                    <button key={m.value} type="button" onClick={() => setForm({...form, optimization_metric: m.value})}
                      className={`px-3 py-2.5 rounded-lg text-xs font-semibold border transition-all ${ form.optimization_metric === m.value ? 'bg-blue-600 border-blue-600 text-white' : 'bg-slate-50 border-slate-200 text-slate-600 hover:border-slate-300' } dark:text-slate-300 dark:border-slate-700`}>
                      {m.label}
                    </button>
                  ))}
                </div>
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Lookback Period</label>
                <select value={form.lookback} onChange={e => setForm({...form, lookback: e.target.value})}
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700">
                  <option value="3">3 Months</option>
                  <option value="6">6 Months</option>
                  <option value="12">1 Year</option>
                  <option value="18">18 Months</option>
                  <option value="24">2 Years</option>
                  <option value="36">3 Years</option>
                </select>
              </div>
              <div className="bg-blue-50 dark:bg-blue-900/20 rounded-xl p-3 text-xs text-blue-700">
                Will test <strong>48 combinations</strong>: RR [1.5, 2.0, 2.5, 3.0] × SL [8, 10, 12, 16] × FVG [2, 4, 6]
              </div>
            </div>
            <div className="flex gap-3 px-6 py-4 border-t border-slate-100 dark:border-slate-800">
              <button onClick={() => setShowForm(false)} className="flex-1 border border-slate-200 text-slate-600 py-2.5 rounded-xl text-sm font-medium dark:text-slate-300 dark:border-slate-700">Cancel</button>
              <button onClick={() => runMutation.mutate()} disabled={!selectedStrategyActive || runMutation.isPending}
                title={!form.strategy_id ? 'Select a strategy first' : !selectedStrategyActive ? 'Activate this strategy before optimizing' : ''}
                className="flex-1 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white py-2.5 rounded-xl text-sm font-semibold transition-colors">
                {runMutation.isPending ? 'Starting…' : 'Start Optimization'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
