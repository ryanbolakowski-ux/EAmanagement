/**
 * Options Session Detail — single-session view with trade list, equity curve,
 * and stop button. Refreshes every 10s so live sessions tick.
 */
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, Square, Activity, AlertCircle } from 'lucide-react'
import { optionsApi, type OptionsSessionDetail as Detail } from '../api/endpoints'

function fmtMoney(v: number | null | undefined): string {
  if (v == null) return '—'
  return `${v < 0 ? '-' : ''}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`
}

function fmtDate(s: string | null | undefined): string {
  if (!s) return '—'
  const d = new Date(s)
  return `${d.toLocaleDateString()} ${d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}`
}

export default function OptionsSessionDetail() {
  const { id } = useParams<{ id: string }>()
  const qc = useQueryClient()

  const { data, isLoading } = useQuery<Detail>({
    queryKey: ['options-session', id],
    queryFn: () => (optionsApi as any).sessionDetail(id!).then((r: any) => r.data),
    enabled: !!id,
    refetchInterval: 10_000,
  })

  const stopMutation = useMutation({
    mutationFn: () => optionsApi.stopSession(id!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['options-session', id] })
      qc.invalidateQueries({ queryKey: ['options-sessions'] })
    },
  })

  if (isLoading || !data) {
    return (
      <div className="p-6 max-w-5xl">
        <div className="h-8 w-48 bg-slate-200 dark:bg-slate-800 rounded mb-4 animate-pulse"/>
        <div className="h-32 bg-slate-100 dark:bg-slate-900 rounded-xl animate-pulse"/>
      </div>
    )
  }

  const closedTrades = data.trades.filter(t => t.status === 'closed')
  const wins = closedTrades.filter(t => (t.net_pnl || 0) > 0).length
  const losses = closedTrades.filter(t => (t.net_pnl || 0) < 0).length
  const winRate = closedTrades.length ? wins / closedTrades.length : 0
  const totalPnl = closedTrades.reduce((acc, t) => acc + (t.net_pnl || 0), 0)

  return (
    <div className="p-6 max-w-5xl space-y-5">
      <Link to="/app/options/sessions" className="inline-flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200">
        <ArrowLeft size={12}/> All options sessions
      </Link>

      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-md text-[10px] font-bold uppercase tracking-wider ${
              data.is_active
                ? 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300'
                : 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400'
            }`}>
              {data.is_active && <Activity size={10}/>}
              {data.is_active ? 'Active' : 'Stopped'}
            </span>
            <span className={`text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${
              data.mode === 'live'
                ? 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300'
                : 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
            }`}>
              {data.mode}
            </span>
          </div>
          <h1 className="text-2xl font-extrabold text-slate-900 dark:text-slate-100">{data.underlyings.join(', ')}</h1>
          <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
            {data.started_at && `Started ${fmtDate(data.started_at)}`}
            {data.ended_at && ` · Ended ${fmtDate(data.ended_at)}`}
          </p>
        </div>
        {data.is_active && (
          <button onClick={() => stopMutation.mutate()} disabled={stopMutation.isPending}
            className="inline-flex items-center gap-2 bg-red-600 hover:bg-red-700 disabled:opacity-50 text-white text-sm font-bold px-4 py-2 rounded-xl">
            <Square size={12} fill="white"/> {stopMutation.isPending ? 'Stopping…' : 'Stop session'}
          </button>
        )}
      </div>

      {/* Summary tiles */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Tile label="Closed Trades" value={String(closedTrades.length)}/>
        <Tile label="Win Rate" value={`${(winRate * 100).toFixed(1)}%`}/>
        <Tile label="Wins / Losses" value={`${wins} / ${losses}`}/>
        <Tile label="Net P&L" value={fmtMoney(totalPnl)} highlight={totalPnl >= 0 ? 'positive' : 'negative'}/>
      </div>

      {data.trades.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 dark:border-slate-700 p-8 text-center">
          <AlertCircle size={28} className="mx-auto text-slate-400 dark:text-slate-500 mb-2"/>
          <p className="text-sm font-semibold text-slate-500 dark:text-slate-400 mb-1">No trades yet</p>
          <p className="text-xs text-slate-400 dark:text-slate-500">
            The strategy is watching for signals. Live trades will appear here automatically.
          </p>
        </div>
      ) : (
        <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="bg-slate-50 dark:bg-slate-800 text-slate-500 dark:text-slate-400">
                <tr>
                  <th className="text-left py-2 px-3 font-semibold">Contract</th>
                  <th className="text-left py-2 px-3 font-semibold">Side</th>
                  <th className="text-right py-2 px-3 font-semibold">Qty</th>
                  <th className="text-right py-2 px-3 font-semibold">Entry</th>
                  <th className="text-right py-2 px-3 font-semibold">Exit</th>
                  <th className="text-right py-2 px-3 font-semibold">Net P&L</th>
                  <th className="text-left py-2 px-3 font-semibold">Exit</th>
                  <th className="text-left py-2 px-3 font-semibold">Time</th>
                </tr>
              </thead>
              <tbody>
                {data.trades.map(t => (
                  <tr key={t.id} className="border-t border-slate-100 dark:border-slate-800">
                    <td className="py-2 px-3 font-mono text-[11px] text-slate-700 dark:text-slate-200 truncate max-w-[180px]" title={t.instrument}>
                      {t.instrument}
                    </td>
                    <td className={`py-2 px-3 font-bold capitalize ${t.direction === 'call' ? 'text-green-600' : 'text-red-500'}`}>
                      {t.direction}
                    </td>
                    <td className="py-2 px-3 text-right tabular-nums">{t.contracts}</td>
                    <td className="py-2 px-3 text-right tabular-nums">{t.entry_price?.toFixed(2) ?? '—'}</td>
                    <td className="py-2 px-3 text-right tabular-nums">{t.exit_price?.toFixed(2) ?? '—'}</td>
                    <td className={`py-2 px-3 text-right tabular-nums font-bold ${(t.net_pnl ?? 0) > 0 ? 'text-green-600' : (t.net_pnl ?? 0) < 0 ? 'text-red-500' : 'text-slate-500'}`}>
                      {fmtMoney(t.net_pnl)}
                    </td>
                    <td className="py-2 px-3 text-slate-500 dark:text-slate-400 capitalize">{t.exit_reason || '—'}</td>
                    <td className="py-2 px-3 text-slate-500 dark:text-slate-400 text-[10px] whitespace-nowrap">
                      {fmtDate(t.exit_time || t.entry_time)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

function Tile({ label, value, highlight }: { label: string; value: string; highlight?: 'positive' | 'negative' }) {
  const tone = highlight === 'positive' ? 'text-green-600 dark:text-green-400'
            : highlight === 'negative' ? 'text-red-500 dark:text-red-400'
            : 'text-slate-900 dark:text-slate-100'
  return (
    <div className="bg-slate-100 dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-3">
      <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">{label}</div>
      <div className={`text-xl font-extrabold mt-1 tabular-nums ${tone}`}>{value}</div>
    </div>
  )
}
