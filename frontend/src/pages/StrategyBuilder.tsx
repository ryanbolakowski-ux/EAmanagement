import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { strategiesApi } from '../api/endpoints'
import type { Strategy, StrategyCreate } from '../types'
import { Plus, Edit2, Trash2, TrendingUp, X, ChevronRight } from 'lucide-react'

const TIMEFRAMES = ['1m', '5m', '15m', '30m', '1H', '4H', '1D']
const INSTRUMENTS = ['ES', 'NQ', 'RTY', 'YM']
const SESSIONS    = ['NY', 'LONDON', 'ASIA', 'NY_AM']

const DEFAULT_FORM: StrategyCreate = {
  name: '', instruments: ['ES'], primary_timeframe: '15m',
  execution_timeframe: '1m', higher_timeframes: ['1H'],
  risk_reward_ratio: 2, stop_loss_type: 'structure',
  max_contracts: 1, session_filters: ['NY'], fvg_min_size_ticks: 4,
}

const STATUS_STYLES: Record<string, string> = {
  active:   'badge badge-green',
  draft:    'badge badge-grey',
  paused:   'badge badge-amber',
  archived: 'badge badge-grey',
}

function ToggleChip({ val, active, onClick }: { val: string; active: boolean; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick}
      className={`px-3 py-1.5 rounded-lg text-xs font-semibold border transition-all ${
        active
          ? 'bg-blue-600 border-blue-600 text-white shadow-sm'
          : 'bg-white border-slate-200 text-slate-500 hover:border-slate-300 hover:text-slate-700'
      }`}>
      {val}
    </button>
  )
}

export default function StrategyBuilder() {
  const qc = useQueryClient()
  const [showForm, setShowForm] = useState(false)
  const [editId, setEditId]     = useState<string | null>(null)
  const [form, setForm]         = useState<StrategyCreate>(DEFAULT_FORM)

  const { data: strategies = [], isLoading } = useQuery({
    queryKey: ['strategies'],
    queryFn: () => strategiesApi.list().then((r) => r.data),
  })

  const createMutation = useMutation({ mutationFn: strategiesApi.create, onSuccess: () => { qc.invalidateQueries({ queryKey: ['strategies'] }); reset() } })
  const updateMutation = useMutation({ mutationFn: ({ id, d }: any) => strategiesApi.update(id, d), onSuccess: () => { qc.invalidateQueries({ queryKey: ['strategies'] }); reset() } })
  const deleteMutation = useMutation({ mutationFn: strategiesApi.delete, onSuccess: () => qc.invalidateQueries({ queryKey: ['strategies'] }) })

  const reset = () => { setShowForm(false); setEditId(null); setForm(DEFAULT_FORM) }
  const toggle = (list: string[], val: string) => list.includes(val) ? list.filter(v => v !== val) : [...list, val]

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    editId ? updateMutation.mutate({ id: editId, d: form }) : createMutation.mutate(form)
  }

  const startEdit = (s: Strategy) => {
    setEditId(s.id)
    setForm({ name: s.name, description: s.description || '', instruments: s.instruments,
      primary_timeframe: s.primary_timeframe, execution_timeframe: s.execution_timeframe,
      higher_timeframes: [], risk_reward_ratio: s.risk_reward_ratio,
      stop_loss_type: s.stop_loss_type, max_contracts: 1, session_filters: s.session_filters, fvg_min_size_ticks: 4 })
    setShowForm(true)
  }

  return (
    <div className="p-8 max-w-5xl">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-extrabold text-slate-900">Strategies</h1>
          <p className="text-slate-500 text-sm mt-1">Define your rule-based entry and exit logic</p>
        </div>
        <button onClick={() => setShowForm(true)}
          className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2.5 rounded-xl text-sm font-semibold transition-colors shadow-sm shadow-blue-200">
          <Plus size={15}/> New Strategy
        </button>
      </div>

      {/* List */}
      {isLoading ? (
        <div className="space-y-3">
          {[...Array(3)].map((_, i) => <div key={i} className="bg-white rounded-xl border border-slate-200 h-20 animate-pulse"/>)}
        </div>
      ) : strategies.length === 0 ? (
        <div className="bg-white rounded-2xl border border-slate-200 border-dashed p-14 text-center">
          <div className="w-12 h-12 bg-blue-50 rounded-2xl flex items-center justify-center mx-auto mb-4">
            <TrendingUp size={22} className="text-blue-500"/>
          </div>
          <p className="font-semibold text-slate-700 mb-1">No strategies yet</p>
          <p className="text-sm text-slate-400 mb-5">Create your first strategy to get started</p>
          <button onClick={() => setShowForm(true)}
            className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg text-sm font-semibold transition-colors">
            <Plus size={14}/> Create Strategy
          </button>
        </div>
      ) : (
        <div className="space-y-3">
          {strategies.map((s) => (
            <div key={s.id} className="bg-white rounded-xl border border-slate-200 p-5 flex items-center gap-4 hover:border-slate-300 hover:shadow-sm transition-all">
              <div className="w-10 h-10 bg-blue-50 rounded-xl flex items-center justify-center flex-shrink-0">
                <TrendingUp size={18} className="text-blue-600"/>
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2.5 mb-0.5">
                  <span className="font-semibold text-slate-900 text-sm">{s.name}</span>
                  <span className={STATUS_STYLES[s.status] ?? 'badge badge-grey'}>{s.status}</span>
                </div>
                <div className="text-xs text-slate-400 flex items-center gap-1.5">
                  <span>{s.instruments.join(', ')}</span>
                  <span>·</span>
                  <span>{s.primary_timeframe} primary</span>
                  <span>·</span>
                  <span>{s.execution_timeframe} execution</span>
                  <span>·</span>
                  <span>RR {s.risk_reward_ratio}:1</span>
                  {s.session_filters.length > 0 && <><span>·</span><span>{s.session_filters.join(', ')}</span></>}
                </div>
              </div>
              <div className="flex items-center gap-1.5 flex-shrink-0">
                <button onClick={() => startEdit(s)} className="p-2 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-700 transition-colors">
                  <Edit2 size={14}/>
                </button>
                <button onClick={() => { if (confirm(`Delete "${s.name}"?`)) deleteMutation.mutate(s.id) }}
                  className="p-2 rounded-lg hover:bg-red-50 text-slate-400 hover:text-red-500 transition-colors">
                  <Trash2 size={14}/>
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Modal */}
      {showForm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-lg max-h-[92vh] flex flex-col">
            <div className="flex items-center justify-between px-6 py-5 border-b border-slate-100">
              <h2 className="text-base font-bold text-slate-900">{editId ? 'Edit Strategy' : 'New Strategy'}</h2>
              <button onClick={reset} className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400 transition-colors"><X size={16}/></button>
            </div>

            <form onSubmit={handleSubmit} className="overflow-y-auto flex-1 px-6 py-5 space-y-5">
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Strategy Name</label>
                <input value={form.name} onChange={e => setForm({...form, name: e.target.value})} required
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                  placeholder="e.g. ES ICT Sweep + FVG"/>
              </div>

              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-2">Instruments</label>
                <div className="flex gap-2 flex-wrap">
                  {INSTRUMENTS.map(i => <ToggleChip key={i} val={i} active={form.instruments.includes(i)} onClick={() => setForm({...form, instruments: toggle(form.instruments, i)})}/>)}
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Primary Timeframe</label>
                  <select value={form.primary_timeframe} onChange={e => setForm({...form, primary_timeframe: e.target.value})}
                    className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                    {TIMEFRAMES.map(tf => <option key={tf}>{tf}</option>)}
                  </select>
                </div>
                <div>
                  <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Execution Timeframe</label>
                  <select value={form.execution_timeframe} onChange={e => setForm({...form, execution_timeframe: e.target.value})}
                    className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                    {TIMEFRAMES.map(tf => <option key={tf}>{tf}</option>)}
                  </select>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Risk : Reward</label>
                  <div className="relative">
                    <input type="number" step="0.5" min="1" value={form.risk_reward_ratio}
                      onChange={e => setForm({...form, risk_reward_ratio: parseFloat(e.target.value)})}
                      className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"/>
                    <span className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-slate-400">: 1</span>
                  </div>
                </div>
                <div>
                  <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Max Contracts</label>
                  <input type="number" min="1" value={form.max_contracts}
                    onChange={e => setForm({...form, max_contracts: parseInt(e.target.value)})}
                    className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"/>
                </div>
              </div>

              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Stop Loss Type</label>
                <div className="flex gap-2">
                  {['structure', 'ticks'].map(t => (
                    <ToggleChip key={t} val={t === 'structure' ? 'Structure-based' : 'Fixed ticks'} active={form.stop_loss_type === t} onClick={() => setForm({...form, stop_loss_type: t})}/>
                  ))}
                </div>
              </div>

              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-2">Session Filters</label>
                <div className="flex gap-2 flex-wrap">
                  {SESSIONS.map(s => <ToggleChip key={s} val={s} active={form.session_filters.includes(s)} onClick={() => setForm({...form, session_filters: toggle(form.session_filters, s)})}/>)}
                </div>
              </div>

              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Min FVG Size (ticks)</label>
                <input type="number" min="1" value={form.fvg_min_size_ticks}
                  onChange={e => setForm({...form, fvg_min_size_ticks: parseInt(e.target.value)})}
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"/>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Daily Loss Limit ($)</label>
                  <input type="number" value={form.max_daily_loss ?? ''} onChange={e => setForm({...form, max_daily_loss: e.target.value ? parseFloat(e.target.value) : undefined})}
                    className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" placeholder="Optional"/>
                </div>
                <div>
                  <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Max Trades / Day</label>
                  <input type="number" value={form.max_trades_per_day ?? ''} onChange={e => setForm({...form, max_trades_per_day: e.target.value ? parseInt(e.target.value) : undefined})}
                    className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" placeholder="Optional"/>
                </div>
              </div>
            </form>

            <div className="flex gap-3 px-6 py-4 border-t border-slate-100">
              <button type="button" onClick={reset} className="flex-1 border border-slate-200 text-slate-600 py-2.5 rounded-xl text-sm font-medium hover:bg-slate-50 transition-colors">Cancel</button>
              <button onClick={handleSubmit as any}
                className="flex-1 bg-blue-600 hover:bg-blue-700 text-white py-2.5 rounded-xl text-sm font-semibold transition-colors">
                {editId ? 'Save Changes' : 'Create Strategy'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
