/**
 * Pending Trades dashboard — every pre-market / intraday signal the bot has
 * generated for the current user, with status badges.
 */
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Clock, CheckCircle2, XCircle, TrendingUp, TrendingDown, AlertCircle, Activity, ArrowLeft } from 'lucide-react'
import { optionsApi, type PendingTrade } from '../api/endpoints'

const STATUS_STYLE: Record<string, { tone: string; label: string; icon: any }> = {
  pending:   { tone: 'bg-amber-100  text-amber-800  dark:bg-amber-900/40  dark:text-amber-200',  label: 'Awaiting confirm', icon: Clock },
  confirmed: { tone: 'bg-blue-100   text-blue-800   dark:bg-blue-900/40   dark:text-blue-200',   label: 'Confirmed',         icon: CheckCircle2 },
  executed:  { tone: 'bg-green-100  text-green-800  dark:bg-green-900/40  dark:text-green-200',  label: 'Executed',          icon: Activity },
  declined:  { tone: 'bg-slate-200  text-slate-600  dark:bg-slate-700    dark:text-slate-300',  label: 'Skipped',           icon: XCircle },
  expired:   { tone: 'bg-slate-100  text-slate-500  dark:bg-slate-800    dark:text-slate-400',  label: 'Expired',           icon: AlertCircle },
}

export default function PendingTrades() {
  const { data, isLoading } = useQuery({
    queryKey: ['pending-trades'],
    queryFn: () => (optionsApi as any).listPending().then((r: any) => r.data),
    refetchInterval: 10_000,
  })

  const trades: PendingTrade[] = data?.pending_trades || []

  return (
    <div className="p-6 max-w-5xl space-y-5">
      <Link to="/app/options" className="inline-flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200">
        <ArrowLeft size={12}/> Options home
      </Link>
      <div>
        <h1 className="text-2xl font-extrabold text-slate-900 dark:text-slate-100">Pending Signals</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
          Pre-market scans at 08:30 ET and intraday hits. Confirm or skip the ones marked awaiting; intraday signals execute automatically and show up here as receipts.
        </p>
      </div>

      {isLoading ? (
        <div className="space-y-2">{[1,2,3].map(i => <div key={i} className="h-16 bg-slate-100 dark:bg-slate-800 rounded-xl animate-pulse"/>)}</div>
      ) : trades.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 dark:border-slate-700 p-10 text-center">
          <Clock size={32} className="mx-auto text-slate-300 dark:text-slate-600 mb-3"/>
          <p className="font-semibold text-slate-500 dark:text-slate-400 mb-1">No pending signals yet</p>
          <p className="text-xs text-slate-400 dark:text-slate-500 max-w-sm mx-auto">
            Activate a universe-scan strategy (Strategy Builder → Options) and the next 08:30 ET scan will surface candidates here.
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {trades.map(t => {
            const style = STATUS_STYLE[t.status] || STATUS_STYLE.pending
            const Icon = style.icon
            const Arrow = t.direction === 'long' ? TrendingUp : TrendingDown
            const dirTone = t.direction === 'long' ? 'text-green-600' : 'text-red-500'
            return (
              <div key={t.id} className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-700 p-4 flex items-center justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-3 min-w-0 flex-1">
                  <Arrow size={16} className={dirTone}/>
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-bold text-slate-900 dark:text-slate-100">{t.instrument}</span>
                      <span className={`text-[10px] font-bold uppercase tracking-wider ${dirTone}`}>{t.direction}</span>
                      {t.is_intraday && <span className="text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300">Intraday</span>}
                    </div>
                    <div className="text-[11px] text-slate-500 dark:text-slate-400 mt-0.5 truncate">
                      {t.strategy_name} · {t.entry_price != null && `entry ${t.entry_price.toFixed(2)} · `}
                      stop {t.stop_loss?.toFixed(2) ?? '—'} · target {t.take_profit?.toFixed(2) ?? '—'}
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-md text-[10px] font-bold uppercase tracking-wider ${style.tone}`}>
                    <Icon size={10}/> {style.label}
                  </span>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
