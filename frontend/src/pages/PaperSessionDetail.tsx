import { useState, useEffect } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Edit2, Check, X as XIcon, StopCircle, Trash2 } from 'lucide-react'
import { paperTradingApi } from '../api/endpoints'
import { MetricsGrid, EquityCurve, TradeTable, type Metrics, type TradeRow } from '../components/TradeMetrics'
import AllocationEditor, { AllocationNote } from '../components/AllocationEditor'
import RefreshButton from '../components/RefreshButton'

type Detail = {
  session: {
    id: string
    strategy_id: string
    strategy_name: string
    is_active: boolean
    started_at: string
    instrument: string | null
    label: string | null
    total_trades: number
    wins: number
    losses: number
    net_pnl: number
    mode?: string
    starting_balance?: number | null
  }
  metrics: Metrics
  trades: TradeRow[]
}

export default function PaperSessionDetail() {
  const { id } = useParams<{ id: string }>()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')

  const { data, isLoading } = useQuery<Detail>({
    queryKey: ['paper-session-detail', id],
    queryFn: () => paperTradingApi.getSessionDetail(id!).then(r => r.data),
    enabled: !!id,
    refetchInterval: 30_000,
  })

  useEffect(() => {
    if (data && !editing) setDraft(data.session.label ?? '')
  }, [data?.session.label, editing])

  const labelMutation = useMutation({
    mutationFn: (label: string | null) => paperTradingApi.setLabel(id!, label),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['paper-session-detail', id] }); qc.invalidateQueries({ queryKey: ['paper-sessions'] }); setEditing(false) },
  })
  const stopMutation = useMutation({
    mutationFn: () => paperTradingApi.stopSession(id!),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['paper-session-detail', id] }); qc.invalidateQueries({ queryKey: ['paper-sessions'] }) },
  })
  const deleteMutation = useMutation({
    mutationFn: () => paperTradingApi.deleteSession(id!),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['paper-sessions'] }); navigate('/app/paper') },
  })

  if (isLoading || !data) return <div className="p-8 text-slate-500 dark:text-slate-400">Loading session…</div>

  const s = data.session
  const title = s.label || s.strategy_name

  return (
    <div className="p-8 max-w-6xl">
      <Link to="/app/paper" className="inline-flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-800 mb-3 transition-colors dark:text-slate-400">
        <ArrowLeft size={14}/> Back to Paper Trading
      </Link>

      <div className="flex items-start justify-between mb-6 flex-wrap gap-3">
        <div className="min-w-0">
          {editing ? (
            <div className="flex items-center gap-2">
              <input
                autoFocus
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') labelMutation.mutate(draft.trim() || null)
                  if (e.key === 'Escape') { setEditing(false); setDraft(s.label ?? '') }
                }}
                placeholder="e.g. Apex Trial #2, Topstep funded, ..."
                className="text-2xl font-extrabold text-slate-900 dark:text-slate-100 bg-transparent border-b border-blue-400 focus:outline-none px-1 max-w-md"
              />
              <button onClick={() => labelMutation.mutate(draft.trim() || null)} className="p-1.5 rounded-lg hover:bg-green-50 dark:hover:bg-green-900/30 text-green-600">
                <Check size={16}/>
              </button>
              <button onClick={() => { setEditing(false); setDraft(s.label ?? '') }} className="p-1.5 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-400 dark:text-slate-500">
                <XIcon size={16}/>
              </button>
            </div>
          ) : (
            <button
              onClick={() => setEditing(true)}
              className="group inline-flex items-center gap-2 text-2xl font-extrabold text-slate-900 dark:text-slate-100 hover:text-blue-600 transition-colors"
            >
              {title}
              <Edit2 size={14} className="text-slate-300 group-hover:text-blue-500 transition-colors dark:text-slate-600"/>
            </button>
          )}
          <div className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            {s.strategy_name}
            {s.instrument && <> · <span className="font-medium text-slate-700 dark:text-slate-300 dark:text-slate-200">{s.instrument}</span></>}
            {' · '}
            {s.is_active ? <span className="text-green-600 font-semibold">Active</span> : <span className="text-slate-400 dark:text-slate-500">Stopped</span>}
            {' · '}started {new Date(s.started_at).toLocaleString()}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <RefreshButton onClick={() => qc.invalidateQueries({ queryKey: ['paper-session-detail', id] })} />
          {s.is_active && (
            <button onClick={() => stopMutation.mutate()} disabled={stopMutation.isPending}
              className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-sm font-semibold border border-red-200 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors">
              <StopCircle size={14}/> Stop
            </button>
          )}
          <button onClick={() => { if (confirm('Delete this session and all its trades?')) deleteMutation.mutate() }} disabled={deleteMutation.isPending}
            className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-sm font-medium text-slate-500 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors dark:text-slate-400">
            <Trash2 size={14}/> Delete
          </button>
        </div>
      </div>

      {/* ALLOC-EVERYWHERE: allocation editable from the detail page too
          (same PATCH /sessions/{id}/allocation as the list cards). The
          detail endpoint serves any of the user's sessions, so gate on
          mode: 'paper' → editor; 'options_paper' → read-only note. */}
      {(s.mode ?? 'paper') === 'paper' && (
        <div className="max-w-sm rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-4 pb-3 mb-6 -mt-1">
          <AllocationEditor session={s} extraInvalidateKeys={[['paper-session-detail', id]]} />
        </div>
      )}
      {s.mode === 'options_paper' && (
        <div className="mb-6 -mt-2"><AllocationNote/></div>
      )}

      <div className="space-y-5">
        <MetricsGrid m={data.metrics}/>
        <EquityCurve trades={data.trades}/>
        <div>
          <h2 className="text-sm font-bold text-slate-700 dark:text-slate-200 mb-3">Trades ({data.trades.length})</h2>
          <TradeTable trades={data.trades}/>
        </div>
      </div>
    </div>
  )
}
