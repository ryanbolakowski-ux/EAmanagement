/**
 * Confirm page — landing target for the email's Confirm/Skip buttons.
 *
 * The URL is /app/pending/:token?action=confirm|decline. We auto-perform
 * the action on mount when the query param is set; otherwise we render a
 * preview of the pending trade with Confirm / Skip buttons.
 */
import { useEffect, useState } from 'react'
import { useParams, useSearchParams, Link } from 'react-router-dom'
import { CheckCircle2, XCircle, Clock, TrendingUp, TrendingDown, AlertCircle } from 'lucide-react'
import { optionsApi, type PendingTrade } from '../api/endpoints'

export default function PendingTradeConfirm() {
  const { token } = useParams<{ token: string }>()
  const [params] = useSearchParams()
  const initialAction = params.get('action') as 'confirm' | 'decline' | null

  const [trade, setTrade]     = useState<PendingTrade | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState<string | null>(null)
  const [outcome, setOutcome] = useState<'confirmed' | 'declined' | null>(null)
  const [acting, setActing]   = useState(false)

  useEffect(() => {
    if (!token) return
    let cancelled = false
    ;(optionsApi as any).getPendingByToken(token)
      .then((r: any) => { if (!cancelled) setTrade(r.data) })
      .catch((e: any) => { if (!cancelled) setError(e?.response?.data?.detail || 'Could not load this signal.') })
      .finally(() => !cancelled && setLoading(false))
    return () => { cancelled = true }
  }, [token])

  // Auto-perform the action when the email button is clicked
  useEffect(() => {
    if (!trade || !initialAction || outcome || trade.status !== 'pending') return
    void perform(initialAction)
  }, [trade, initialAction])

  async function perform(action: 'confirm' | 'decline') {
    if (!token || acting) return
    setActing(true)
    try {
      if (action === 'confirm') {
        await (optionsApi as any).confirmPendingByToken(token)
        setOutcome('confirmed')
      } else {
        await (optionsApi as any).declinePendingByToken(token)
        setOutcome('declined')
      }
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Could not record your decision.')
    } finally {
      setActing(false)
    }
  }

  if (loading) {
    return <div className="p-8 max-w-xl mx-auto"><div className="h-32 bg-slate-100 dark:bg-slate-800 rounded-xl animate-pulse"/></div>
  }

  if (error) {
    return (
      <div className="p-8 max-w-xl mx-auto">
        <div className="rounded-xl border border-red-200 dark:border-red-900/40 bg-red-50 dark:bg-red-900/20 p-5 flex items-start gap-3">
          <AlertCircle size={20} className="text-red-600 flex-shrink-0 mt-0.5"/>
          <div>
            <div className="font-bold text-red-800 dark:text-red-200 mb-1">Signal unavailable</div>
            <p className="text-sm text-red-700 dark:text-red-300 leading-relaxed">{error}</p>
            <Link to="/app/options/pending" className="inline-block mt-3 text-xs font-semibold text-red-700 dark:text-red-300 underline">All pending signals →</Link>
          </div>
        </div>
      </div>
    )
  }

  if (!trade) return null

  const dir = trade.direction === 'long' ? 'LONG' : 'SHORT'
  const Arrow = trade.direction === 'long' ? TrendingUp : TrendingDown
  const dirTone = trade.direction === 'long' ? 'text-green-600 dark:text-green-400' : 'text-red-500 dark:text-red-400'
  const dirBg = trade.direction === 'long' ? 'bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-900/40' : 'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-900/40'

  // Visual state outranks the actual DB state (confirmation just happened in
  // this session via `outcome`) so the success page renders immediately.
  const stateRender = outcome
    ? outcome === 'confirmed' ? 'confirmed' : 'declined'
    : trade.status

  return (
    <div className="p-6 max-w-xl mx-auto space-y-5">
      <div>
        <Link to="/app" className="text-xs text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200">← Dashboard</Link>
        <h1 className="text-2xl font-extrabold text-slate-900 dark:text-slate-100 mt-1">Pre-market signal</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-0.5">{trade.strategy_name}</p>
      </div>

      <div className={`rounded-2xl border p-5 ${dirBg}`}>
        <div className="flex items-center gap-3 mb-3">
          <Arrow size={20} className={dirTone}/>
          <span className={`text-xs font-bold uppercase tracking-widest ${dirTone}`}>{dir}</span>
          <span className="text-2xl font-extrabold text-slate-900 dark:text-slate-100">{trade.instrument}</span>
        </div>
        <div className="grid grid-cols-3 gap-3 text-sm mb-3">
          <Tile label="Entry"  value={trade.entry_price?.toFixed(2)} tone="blue"/>
          <Tile label="Stop"   value={trade.stop_loss?.toFixed(2)}   tone="red"/>
          <Tile label="Target" value={trade.take_profit?.toFixed(2)} tone="green"/>
        </div>
        {trade.bias && (
          <div className="text-xs text-slate-600 dark:text-slate-300 mb-1">
            <strong>Bias:</strong> <span className="capitalize">{trade.bias.replace(/_/g, ' ')}</span>
          </div>
        )}
        {trade.reason && (
          <div className="text-xs text-slate-600 dark:text-slate-300 italic leading-relaxed">{trade.reason}</div>
        )}
        {trade.expires_at && stateRender === 'pending' && (
          <div className="text-[11px] text-slate-500 dark:text-slate-400 mt-3 flex items-center gap-1.5">
            <Clock size={11}/> Expires {new Date(trade.expires_at).toLocaleString(undefined, { hour: '2-digit', minute: '2-digit', month: 'short', day: 'numeric' })}
          </div>
        )}
      </div>

      {stateRender === 'pending' && (
        <div className="flex gap-3 justify-end">
          <button onClick={() => perform('decline')} disabled={acting}
            className="px-5 py-2.5 rounded-xl border border-slate-300 dark:border-slate-700 text-slate-600 dark:text-slate-300 text-sm font-semibold hover:bg-slate-50 dark:hover:bg-slate-800 disabled:opacity-50">
            {acting ? '…' : 'Skip this one'}
          </button>
          <button onClick={() => perform('confirm')} disabled={acting}
            className="px-6 py-2.5 rounded-xl bg-green-600 hover:bg-green-700 disabled:opacity-50 text-white text-sm font-bold inline-flex items-center gap-2">
            <CheckCircle2 size={16}/> {acting ? 'Confirming…' : 'Confirm — execute'}
          </button>
        </div>
      )}

      {stateRender === 'confirmed' && (
        <div className="rounded-xl border border-green-200 dark:border-green-900/40 bg-green-50 dark:bg-green-900/20 p-4 flex items-start gap-3">
          <CheckCircle2 size={20} className="text-green-600 flex-shrink-0 mt-0.5"/>
          <div className="text-sm text-green-800 dark:text-green-200">
            <div className="font-bold mb-1">Confirmed</div>
            The bot will execute this trade at the next scan tick (within seconds for live, ≤60s for paper).
          </div>
        </div>
      )}

      {stateRender === 'declined' && (
        <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 p-4 flex items-start gap-3">
          <XCircle size={20} className="text-slate-500 flex-shrink-0 mt-0.5"/>
          <div className="text-sm text-slate-700 dark:text-slate-200">
            <div className="font-bold mb-1">Skipped</div>
            The bot will not enter this trade. Other strategies are unaffected.
          </div>
        </div>
      )}

      {(stateRender === 'executed' || stateRender === 'expired') && (
        <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 p-4 text-sm text-slate-600 dark:text-slate-300">
          This signal is no longer active — status: <strong>{stateRender}</strong>.
        </div>
      )}
    </div>
  )
}

function Tile({ label, value, tone }: { label: string; value?: string; tone: 'blue' | 'red' | 'green' }) {
  const t = tone === 'blue' ? 'text-blue-700 dark:text-blue-300'
        : tone === 'red'  ? 'text-red-600  dark:text-red-300'
                          : 'text-green-700 dark:text-green-300'
  return (
    <div className="bg-white/60 dark:bg-slate-800/40 rounded-lg px-3 py-2">
      <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">{label}</div>
      <div className={`text-base font-extrabold tabular-nums ${t}`}>{value || '—'}</div>
    </div>
  )
}
