/**
 * Signal review — landing target for the Email Signals "Review & approve" button.
 * URL: /app/signals/:id/review?action=approve|decline (auto-acts when action set).
 * Approve/decline a trade idea. Nothing is placed unless you approve AND your
 * plan permits placement (the backend enforces this; placed_ref shows the result).
 */
import { useEffect, useState } from 'react'
import { useParams, useSearchParams, Link } from 'react-router-dom'
import { CheckCircle2, XCircle, Clock, TrendingUp, TrendingDown, AlertCircle, ShieldCheck } from 'lucide-react'
import api from '../api/client'

type ReviewSignal = {
  id: string
  instrument: string
  direction: 'long' | 'short'
  entry_price: number
  stop_loss: number
  take_profit: number
  bias: string | null
  fired_at: string | null
  status: string
  outcome: string | null
  decision: 'approved' | 'declined' | null
  decided_at: string | null
  placed_ref: string | null
  requires_manual_approval: boolean
  can_place_on_approval: boolean
}

function Tile({ label, value, tone }: { label: string; value?: string; tone: 'blue' | 'red' | 'green' }) {
  const t = tone === 'blue' ? 'text-blue-600 dark:text-blue-400'
    : tone === 'red' ? 'text-red-600 dark:text-red-400' : 'text-green-600 dark:text-green-400'
  return (
    <div className="rounded-xl bg-white/60 dark:bg-slate-900/40 border border-slate-200 dark:border-slate-700 p-3 text-center">
      <div className="text-[11px] uppercase tracking-wide text-slate-500 dark:text-slate-400">{label}</div>
      <div className={`text-lg font-extrabold tabular-nums ${t}`}>{value ?? '—'}</div>
    </div>
  )
}

export default function SignalReview() {
  const { id } = useParams<{ id: string }>()
  const [params] = useSearchParams()
  const initialAction = params.get('action') as 'approve' | 'decline' | null

  const [sig, setSig] = useState<ReviewSignal | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [outcome, setOutcome] = useState<'approved' | 'declined' | null>(null)
  const [placedRef, setPlacedRef] = useState<string | null>(null)
  const [acting, setActing] = useState(false)

  useEffect(() => {
    if (!id) return
    let cancelled = false
    api.get<ReviewSignal>(`/api/v1/account-signals/${id}/review`)
      .then((r) => { if (!cancelled) setSig(r.data) })
      .catch((e: any) => { if (!cancelled) setError(e?.response?.data?.detail || 'Could not load this signal.') })
      .finally(() => !cancelled && setLoading(false))
    return () => { cancelled = true }
  }, [id])

  useEffect(() => {
    if (!sig || !initialAction || outcome || sig.decision || sig.status !== 'sent') return
    void perform(initialAction)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig, initialAction])

  async function perform(action: 'approve' | 'decline') {
    if (!id || acting) return
    setActing(true)
    try {
      const r = await api.post(`/api/v1/account-signals/${id}/${action}`)
      setOutcome(action === 'approve' ? 'approved' : 'declined')
      setPlacedRef((r.data as any)?.placed_ref ?? null)
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Could not record your decision.')
    } finally {
      setActing(false)
    }
  }

  if (loading) return <div className="p-8 max-w-xl mx-auto"><div className="h-32 bg-slate-100 dark:bg-slate-800 rounded-xl animate-pulse" /></div>

  if (error) return (
    <div className="p-8 max-w-xl mx-auto">
      <div className="rounded-xl border border-red-200 dark:border-red-900/40 bg-red-50 dark:bg-red-900/20 p-5 flex items-start gap-3">
        <AlertCircle size={20} className="text-red-600 flex-shrink-0 mt-0.5" />
        <div>
          <div className="font-bold text-red-800 dark:text-red-200 mb-1">Signal unavailable</div>
          <p className="text-sm text-red-700 dark:text-red-300 leading-relaxed">{error}</p>
          <Link to="/app/email-signals" className="inline-block mt-3 text-xs font-semibold text-red-700 dark:text-red-300 underline">All Email Signals →</Link>
        </div>
      </div>
    </div>
  )

  if (!sig) return null

  const dir = sig.direction === 'long' ? 'LONG' : 'SHORT'
  const Arrow = sig.direction === 'long' ? TrendingUp : TrendingDown
  const dirTone = sig.direction === 'long' ? 'text-green-600 dark:text-green-400' : 'text-red-500 dark:text-red-400'
  const dirBg = sig.direction === 'long' ? 'bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-900/40' : 'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-900/40'
  const state = outcome ?? sig.decision ?? 'pending'

  return (
    <div className="p-6 max-w-xl mx-auto space-y-5">
      <div>
        <Link to="/app/email-signals" className="text-xs text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200">← Email Signals</Link>
        <h1 className="text-2xl font-extrabold text-slate-900 dark:text-slate-100 mt-1">Review trade idea</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-0.5">{sig.fired_at ? new Date(sig.fired_at).toLocaleString() : ''}</p>
      </div>

      <div className={`rounded-2xl border p-5 ${dirBg}`}>
        <div className="flex items-center gap-3 mb-3">
          <Arrow size={20} className={dirTone} />
          <span className={`text-xs font-bold uppercase tracking-widest ${dirTone}`}>{dir}</span>
          <span className="text-2xl font-extrabold text-slate-900 dark:text-slate-100">{sig.instrument}</span>
        </div>
        <div className="grid grid-cols-3 gap-3 text-sm mb-3">
          <Tile label="Entry" value={sig.entry_price?.toFixed(2)} tone="blue" />
          <Tile label="Stop" value={sig.stop_loss?.toFixed(2)} tone="red" />
          <Tile label="Target" value={sig.take_profit?.toFixed(2)} tone="green" />
        </div>
        {sig.bias && <div className="text-xs text-slate-600 dark:text-slate-300"><strong>Bias:</strong> <span className="capitalize">{sig.bias.replace(/_/g, ' ')}</span></div>}
      </div>

      <div className="text-[11px] text-slate-500 dark:text-slate-400 leading-relaxed flex items-start gap-1.5">
        <ShieldCheck size={13} className="flex-shrink-0 mt-0.5" />
        {sig.can_place_on_approval
          ? 'Approving may place this trade in your connected account if your broker, permissions, risk settings and confirmations allow it. Otherwise it is recorded as approved but not placed.'
          : 'This is a trade idea for your decision. Approving records your decision; your current plan does not place trades automatically.'}
      </div>

      {state === 'pending' && sig.status === 'sent' && (
        <div className="flex gap-3 justify-end">
          <button onClick={() => perform('decline')} disabled={acting}
            className="px-5 py-2.5 rounded-xl border border-slate-300 dark:border-slate-700 text-slate-600 dark:text-slate-300 text-sm font-semibold hover:bg-slate-50 dark:hover:bg-slate-800 disabled:opacity-50">
            {acting ? '…' : 'Decline'}
          </button>
          <button onClick={() => perform('approve')} disabled={acting}
            className="px-6 py-2.5 rounded-xl bg-green-600 hover:bg-green-700 disabled:opacity-50 text-white text-sm font-bold inline-flex items-center gap-2">
            <CheckCircle2 size={16} /> {acting ? 'Approving…' : 'Approve'}
          </button>
        </div>
      )}

      {state === 'approved' && (
        <div className="rounded-xl border border-green-200 dark:border-green-900/40 bg-green-50 dark:bg-green-900/20 p-4 flex items-start gap-3">
          <CheckCircle2 size={20} className="text-green-600 flex-shrink-0 mt-0.5" />
          <div className="text-sm text-green-800 dark:text-green-200">
            <div className="font-bold mb-1">Approved</div>
            {(placedRef ?? sig.placed_ref)
              ? <>Placement result: <span className="font-mono text-xs">{placedRef ?? sig.placed_ref}</span></>
              : 'Your approval has been recorded.'}
          </div>
        </div>
      )}

      {state === 'declined' && (
        <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 p-4 flex items-start gap-3">
          <XCircle size={20} className="text-slate-500 flex-shrink-0 mt-0.5" />
          <div className="text-sm text-slate-700 dark:text-slate-200">
            <div className="font-bold mb-1">Declined</div>
            No trade will be placed for this idea.
          </div>
        </div>
      )}

      {sig.status !== 'sent' && state === 'pending' && (
        <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 p-4 text-sm text-slate-600 dark:text-slate-300 flex items-center gap-2">
          <Clock size={14} /> This signal is no longer actionable (status: <strong>{sig.status}</strong>).
        </div>
      )}
    </div>
  )
}
