import { useState, useEffect } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Edit2, Check, X as XIcon, ShieldAlert } from 'lucide-react'
import { liveTradingApi } from '../api/endpoints'
import { MetricsGrid, EquityCurve, TradeTable, type Metrics, type TradeRow } from '../components/TradeMetrics'
import ToggleSwitch from '../components/ToggleSwitch'
import RefreshButton from '../components/RefreshButton'

type Detail = {
  account: {
    id: string
    account_name: string
    broker: string
    is_demo: boolean
    is_active: boolean
    trading_enabled: boolean
    created_at: string
  }
  metrics: Metrics
  trades: TradeRow[]
}

export default function LiveAccountDetail() {
  const { id } = useParams<{ id: string }>()
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')

  const { data, isLoading } = useQuery<Detail>({
    queryKey: ['live-account-detail', id],
    queryFn: () => liveTradingApi.getAccountDetail(id!).then(r => r.data),
    enabled: !!id,
    refetchInterval: 30_000,
  })

  useEffect(() => {
    if (data && !editing) setDraft(data.account.account_name)
  }, [data?.account.account_name, editing])

  const labelMutation = useMutation({
    mutationFn: (label: string) => liveTradingApi.setAccountLabel(id!, label),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['live-account-detail', id] }); qc.invalidateQueries({ queryKey: ['broker-accounts'] }); setEditing(false) },
  })
  const tradingMutation = useMutation({
    mutationFn: (next: boolean) => liveTradingApi.setTradingEnabled(id!, next),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['live-account-detail', id] }); qc.invalidateQueries({ queryKey: ['broker-accounts'] }) },
  })

  if (isLoading || !data) return <div className="p-8 text-slate-500 dark:text-slate-400">Loading account…</div>

  const a = data.account

  return (
    <div className="p-8 max-w-6xl">
      <Link to="/app/live" className="inline-flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-800 mb-3 transition-colors dark:text-slate-400">
        <ArrowLeft size={14}/> Back to Live Trading
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
                  if (e.key === 'Enter' && draft.trim()) labelMutation.mutate(draft.trim())
                  if (e.key === 'Escape') { setEditing(false); setDraft(a.account_name) }
                }}
                placeholder="e.g. Apex 50K, Topstep funded #3, ..."
                className="text-2xl font-extrabold text-slate-900 dark:text-slate-100 bg-transparent border-b border-blue-400 focus:outline-none px-1 max-w-md"
              />
              <button onClick={() => draft.trim() && labelMutation.mutate(draft.trim())} className="p-1.5 rounded-lg hover:bg-green-50 dark:hover:bg-green-900/30 text-green-600">
                <Check size={16}/>
              </button>
              <button onClick={() => { setEditing(false); setDraft(a.account_name) }} className="p-1.5 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-400 dark:text-slate-500">
                <XIcon size={16}/>
              </button>
            </div>
          ) : (
            <button
              onClick={() => setEditing(true)}
              className="group inline-flex items-center gap-2 text-2xl font-extrabold text-slate-900 dark:text-slate-100 hover:text-blue-600 transition-colors"
            >
              {a.account_name}
              <Edit2 size={14} className="text-slate-300 group-hover:text-blue-500 transition-colors dark:text-slate-600"/>
            </button>
          )}
          <div className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            {a.broker} · {a.is_demo ? 'Demo' : 'Live'}
            {' · '}
            {a.is_active ? <span className="text-green-600 font-semibold">Connected</span> : <span className="text-slate-400 dark:text-slate-500">Inactive</span>}
            {' · '}connected {new Date(a.created_at).toLocaleString()}
          </div>
        </div>
        <div className="flex items-center gap-3">
          <RefreshButton onClick={() => qc.invalidateQueries({ queryKey: ['live-account-detail', id] })} />
          <ToggleSwitch
            label={a.trading_enabled ? 'Trading' : 'Paused'}
            checked={a.trading_enabled}
            disabled={tradingMutation.isPending}
            onChange={(next) => tradingMutation.mutate(next)}
          />
        </div>
      </div>

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
