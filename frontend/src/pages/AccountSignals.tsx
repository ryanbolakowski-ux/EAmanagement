import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useState, Fragment } from 'react'
import { Bell, Mail, Smartphone, PlayCircle, ShieldCheck, Plus, AlertTriangle, Info, ShieldAlert } from 'lucide-react'
import { useAuthStore } from '../stores/authStore'
import { strategiesApi, paperTradingApi } from '../api/endpoints'
import api from '../api/client'
import { fmtEntryTime } from '../components/TradeMetrics'

type Signal = {
  id: string
  strategy_name: string
  instrument: string
  direction: string
  entry_price: number
  stop_loss: number
  take_profit: number
  bias: string | null
  fired_at: string
  status: 'pending' | 'sent' | 'acted' | 'expired' | 'suppressed'
  outcome?: string | null
  outcome_price?: number | null
  outcome_r?: number | null
  resolved_at?: string | null
  // Suppressed-row diagnostics (only set on /suppressed endpoint)
  duplicate_suppressed_at?: string | null
  duplicate_suppressed_count?: number | null
  error_message?: string | null
  notes?: string
  // Annotated trade-chart PNG (base64), rendered inline in the expanded row.
  chart_b64?: string | null
}

// ── Outcome dot ────────────────────────────────────────────────────────────
// Tiny coloured pill that surfaces the resolved outcome of a fired signal so
// the user can scan the list at a glance: green=win, red=loss, blue-grey=
// breakeven, amber=expired, neutral grey=still pending. Falls through to
// neutral for any unknown/empty value so weird data never breaks the row.
function OutcomeDot({ outcome }: { outcome: string | null | undefined }) {
  const o = (outcome || '').toLowerCase().trim()
  let cls = 'bg-slate-300 dark:bg-slate-600'
  let title = 'Pending — outcome not yet resolved'
  if (o === 'win' || o === 'tp' || o === 'tp_hit') {
    cls = 'bg-emerald-500'; title = 'Win — hit take profit'
  } else if (o === 'loss' || o === 'sl' || o === 'sl_hit') {
    cls = 'bg-rose-500'; title = 'Loss — hit stop loss'
  } else if (o === 'breakeven' || o === 'be') {
    cls = 'bg-slate-400 dark:bg-slate-500'; title = 'Break even'
  } else if (o === 'expired') {
    cls = 'bg-amber-400'; title = 'Expired before TP or SL was hit'
  }
  return <span className={`inline-block w-2.5 h-2.5 rounded-full ${cls}`} title={title} aria-label={title} />
}

const accountSignalsApi = {
  list: () => api.get<Signal[]>('/api/v1/account-signals/'),
  listSuppressed: () => api.get<Signal[]>('/api/v1/account-signals/suppressed'),
  startWatcher: (data: { strategy_id: string; instruments: string[]; account_label: string; channels: string[] }) =>
    api.post('/api/v1/account-signals/watchers', data),
  listWatchers: () => api.get('/api/v1/account-signals/watchers'),
  stopWatcher: (id: string) => api.delete(`/api/v1/account-signals/watchers/${id}`),
}




function PendingOrdersCard() {
  const qc = useQueryClient()
  const { data } = useQuery({
    queryKey: ['pending-orders'],
    queryFn: () => api.get('/api/v1/scanner/pending-orders').then((r: any) => r.data),
    refetchInterval: 30_000,
  })
  const cancelOrder = useMutation({
    mutationFn: (id: string) => api.delete(`/api/v1/scanner/pending-orders/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['pending-orders'] }),
  })
  if (!data || data.count === 0) return null
  const sess = data.session || {}
  const sessLabel = sess.label === 'PRE_MARKET' ? 'Pre-market — orders will fill at 9:30 AM ET open'
                  : sess.label === 'AFTER_HOURS' ? 'After-hours — limit orders active until 8:00 PM ET'
                  : sess.label === 'CLOSED' ? 'Markets closed — orders queued for next open'
                  : 'Regular hours — orders should fill shortly'
  return (
    <div className="rounded-2xl border border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30 p-4 mb-4">
      <div className="flex items-start justify-between mb-3 gap-2">
        <div>
          <div className="text-[10px] uppercase tracking-[0.2em] font-bold text-amber-800 dark:text-amber-200">⏳ Pending close orders</div>
          <div className="text-xs text-amber-700 dark:text-amber-300 mt-0.5">{data.count} order{data.count === 1 ? '' : 's'} queued at Tradier · {sessLabel}</div>
        </div>
      </div>
      <div className="space-y-1.5">
        {data.orders.map((o: any) => (
          <div key={o.id} className="bg-white dark:bg-slate-900 rounded-lg px-3 py-2 flex items-center justify-between text-xs">
            <div className="flex items-center gap-2 min-w-0">
              <span className={`text-[10px] font-bold uppercase px-1.5 py-0.5 rounded ${o.side === 'sell' ? 'bg-rose-100 text-rose-700' : 'bg-emerald-100 text-emerald-700'}`}>{o.side}</span>
              <span className="font-bold">{o.symbol}</span>
              <span className="text-slate-500">{o.quantity} sh</span>
              <span className="text-slate-400 uppercase text-[10px]">{o.type}{o.price ? ` @ $${o.price}` : ''}</span>
              <span className="text-slate-400 uppercase text-[10px]">duration={o.duration}</span>
            </div>
            <button onClick={() => cancelOrder.mutate(o.id)} disabled={cancelOrder.isPending}
              className="ml-2 text-[10px] font-bold text-rose-600 hover:underline whitespace-nowrap">Cancel</button>
          </div>
        ))}
      </div>
    </div>
  )
}

function OpenPositionsCard() {
  const qc = useQueryClient()
  const { data } = useQuery({
    queryKey: ['scanner-open-positions'],
    queryFn: () => api.get('/api/v1/scanner/open-positions').then((r: any) => r.data),
    refetchInterval: 30_000,
  })
  const closeAll = useMutation({
    mutationFn: () => api.post('/api/v1/scanner/close-all'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['scanner-open-positions'] }),
  })
  const forceCloseAll = useMutation({
    mutationFn: () => api.post('/api/v1/scanner/force-close-all'),
    onSuccess: (r: any) => {
      qc.invalidateQueries({ queryKey: ['scanner-open-positions'] })
      alert(`Force-closed ${r.data?.count || 0} positions at Tradier.`)
    },
  })
  if (!data) return null
  // No open positions but maybe realized P&L from earlier today
  if (data.count === 0) {
    const realized = data.realized_today || 0
    const closedToday = data.closed_today || []
    if (realized === 0 && closedToday.length === 0) {
      return (
        <div className="rounded-2xl border border-dashed border-slate-300 dark:border-slate-700 bg-slate-50 dark:bg-slate-900/40 p-5 text-center">
          <div className="text-sm font-extrabold text-slate-600 dark:text-slate-300">No open Theta Scanner positions</div>
          <div className="text-xs text-slate-500 dark:text-slate-400 mt-1">Positions opened by the daily scanner appear here with live P&L + trailing-stop tracking.</div>
        </div>
      )
    }
    const rClass = realized > 0 ? 'text-emerald-400' : realized < 0 ? 'text-rose-400' : 'text-slate-200'
    return (
      <div className="rounded-2xl bg-gradient-to-br from-slate-900 to-slate-950 text-white p-5 shadow-xl">
        <div className="flex items-start justify-between mb-4">
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] font-bold opacity-70">Today's realized P&L</div>
            <div className="text-xs opacity-60 mt-0.5">{closedToday.length} position{closedToday.length === 1 ? '' : 's'} closed today · no positions currently open</div>
          </div>
          <div className={`text-3xl font-extrabold tabular-nums ${rClass}`}>
            {realized >= 0 ? '+' : ''}${Math.abs(realized).toLocaleString(undefined,{maximumFractionDigits:2})}
          </div>
        </div>
        <div className="space-y-2">
          {closedToday.map((c: any, i: number) => {
            const cls = c.realized_pnl >= 0 ? 'text-emerald-300' : 'text-rose-300'
            return (
              <div key={i} className="bg-white/5 rounded-lg px-3 py-2.5 flex items-center justify-between gap-3 text-xs">
                <div className="font-extrabold text-base">{c.ticker}</div>
                <div className="opacity-70">{c.qty} sh</div>
                <div className="opacity-70">entry ${c.entry_price.toFixed(2)} → exit ${c.exit_price.toFixed(2)}</div>
                <div className="opacity-60 text-[10px] uppercase tracking-wider">{c.exit_reason}</div>
                <div className={`font-bold tabular-nums ${cls}`}>{c.realized_pct >= 0 ? '+' : ''}{c.realized_pct.toFixed(1)}%</div>
                <div className={`font-bold tabular-nums ${cls}`}>{c.realized_pnl >= 0 ? '+' : ''}${Math.abs(c.realized_pnl).toFixed(2)}</div>
              </div>
            )
          })}
        </div>
      </div>
    )
  }
  const pnl = data.total_unrealized
  const pnlClass = pnl > 0 ? 'text-emerald-300' : pnl < 0 ? 'text-rose-300' : 'text-slate-200'
  return (
    <div className="rounded-2xl bg-gradient-to-br from-slate-900 to-slate-950 text-white p-5 shadow-xl">
      <div className="flex items-start justify-between mb-4 gap-3">
        <div>
          <div className="text-[10px] uppercase tracking-[0.2em] font-bold opacity-70">Open Theta Scanner positions</div>
          <div className="text-xs opacity-60 mt-0.5">{data.count} position{data.count === 1 ? '' : 's'} · 3% trailing stop active</div>
          {data.tradier_equity !== null && data.tradier_equity !== undefined && (
            <div className="mt-2 text-xs opacity-80">
              <span className="opacity-60">Tradier sandbox balance:</span> <span className="font-bold">${data.tradier_equity.toLocaleString(undefined,{maximumFractionDigits:2})}</span>
              {data.tradier_open_pl !== undefined && data.tradier_open_pl !== null && (
                <span className={`ml-2 font-bold ${data.tradier_open_pl >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
                  ({data.tradier_open_pl >= 0 ? '+' : ''}${Math.abs(data.tradier_open_pl).toLocaleString(undefined,{maximumFractionDigits:2})} open)
                </span>
              )}
            </div>
          )}
        </div>
        <div className="flex gap-2">
          <button onClick={() => { if (confirm(`Close all ${data.count} Theta Scanner positions at market?`)) closeAll.mutate() }}
            disabled={closeAll.isPending}
            className="bg-rose-600 hover:bg-rose-700 disabled:opacity-50 text-white text-xs font-bold px-3 py-1.5 rounded-lg whitespace-nowrap">
            {closeAll.isPending ? 'Closing...' : 'Close all'}
          </button>
          <button onClick={() => { if (confirm('FORCE CLOSE everything at Tradier — including positions outside Theta Scanner. Continue?')) forceCloseAll.mutate() }}
            disabled={forceCloseAll.isPending}
            className="bg-rose-900 hover:bg-rose-950 disabled:opacity-50 text-white text-xs font-bold px-3 py-1.5 rounded-lg whitespace-nowrap border border-rose-700"
            title="Closes EVERY open position on the Tradier account, not just Theta Scanner picks">
            {forceCloseAll.isPending ? 'Force closing...' : '⚠ Force close all'}
          </button>
        </div>
      </div>
      <div className="grid grid-cols-3 gap-3 mb-4">
        <div><div className="text-[10px] uppercase opacity-60">Cost basis</div><div className="text-lg font-bold tabular-nums">${data.total_cost.toLocaleString(undefined,{maximumFractionDigits:2})}</div></div>
        <div><div className="text-[10px] uppercase opacity-60">Market value</div><div className="text-lg font-bold tabular-nums">${data.total_value.toLocaleString(undefined,{maximumFractionDigits:2})}</div></div>
        <div><div className="text-[10px] uppercase opacity-60">Open P&L</div><div className={`text-xl font-extrabold tabular-nums ${pnlClass}`}>{pnl >= 0 ? '+' : ''}${Math.abs(pnl).toLocaleString(undefined,{maximumFractionDigits:2})} ({data.total_unrealized_pct >= 0 ? '+' : ''}{data.total_unrealized_pct.toFixed(1)}%)</div></div>
      </div>
      <div className="space-y-2">
        {data.positions.map((p: any) => {
          const c = p.unrealized_pnl >= 0 ? 'text-emerald-300' : 'text-rose-300'
          return (
            <div key={p.id} className="bg-white/5 rounded-lg px-3 py-2.5 flex items-center justify-between gap-3 text-xs">
              <div className="font-extrabold text-base">{p.ticker}</div>
              <div className="opacity-70">{p.qty} sh · entry ${p.entry_price.toFixed(2)}</div>
              <div className="opacity-70">live ${p.live_price ? p.live_price.toFixed(2) : '--'}</div>
              <div className="opacity-70">high ${p.trail_high.toFixed(2)}</div>
              <div className={`font-bold tabular-nums ${c}`}>{p.unrealized_pct >= 0 ? '+' : ''}{p.unrealized_pct.toFixed(1)}%</div>
              <div className={`font-bold tabular-nums ${c}`}>{p.unrealized_pnl >= 0 ? '+' : ''}${Math.abs(p.unrealized_pnl).toFixed(2)}</div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function ScannerTodayCard() {
  const { data } = useQuery({
    queryKey: ['theta-today-pick-emailsig'],
    queryFn: () => api.get('/api/v1/scanner/today-pick').then(r => r.data),
    refetchInterval: 60_000,
  })
  const pick = data?.pick
  const ms = data?.market_status
  if (ms?.status === 'holiday') {
    return (
      <div className="rounded-2xl border border-rose-300 dark:border-rose-800 bg-rose-50 dark:bg-rose-950/30 p-5 text-center">
        <div className="text-3xl mb-2">🏛️</div>
        <div className="font-extrabold text-rose-800 dark:text-rose-200 mb-1">Markets closed — {ms.holiday_name}</div>
        <div className="text-xs text-rose-700/80 dark:text-rose-300/80 mt-1">Theta Scanner resumes <strong>{ms.next_open} at 9:25 AM ET</strong>.</div>
      </div>
    )
  }
  if (ms?.status === 'weekend') {
    return (
      <div className="rounded-2xl border border-slate-300 dark:border-slate-700 bg-slate-100 dark:bg-slate-900/60 p-5 text-center">
        <div className="text-3xl mb-2">🏖️</div>
        <div className="font-extrabold text-slate-700 dark:text-slate-200 mb-1">Markets are closed — it's {ms.today}</div>
        <div className="text-xs text-slate-500 dark:text-slate-400">Theta Scanner resumes <strong>{ms.next_open} at 9:25 AM ET</strong>.</div>
      </div>
    )
  }
  if (ms?.status === 'news_blackout') {
    return (
      <div className="rounded-2xl border border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30 p-5 text-center">
        <div className="text-3xl mb-2">📰</div>
        <div className="font-extrabold text-amber-800 dark:text-amber-200 mb-1">Scanner paused — high-impact news</div>
        <div className="text-sm text-amber-700 dark:text-amber-300 font-semibold mt-1">{ms.event_name} at {ms.event_time_et}</div>
        <div className="text-xs text-amber-700/80 dark:text-amber-400/80 mt-2">We don't trade through CPI / FOMC / NFP / PPI — too much slippage and gap risk. Scanner will resume after the event.</div>
      </div>
    )
  }
  if (!pick) {
    return (
      <div className="rounded-2xl border border-dashed border-violet-300 dark:border-violet-800 bg-violet-50/40 dark:bg-violet-950/30 p-5 text-center">
        <div className="text-2xl mb-1">🎯</div>
        <div className="font-extrabold text-slate-700 dark:text-slate-200 mb-1">No pick yet today</div>
        <div className="text-xs text-slate-500 dark:text-slate-400">{data?.message || 'Scanner runs daily at 9:25 ET.'}</div>
      </div>
    )
  }
  const rr = ((pick.target - pick.entry) / (pick.entry - pick.stop)).toFixed(1)
  return (
    <div className="rounded-2xl bg-gradient-to-br from-violet-600 to-indigo-700 text-white p-5 shadow-xl shadow-violet-900/20">
      <div className="flex items-start justify-between mb-4">
        <div>
          <div className="text-[10px] uppercase tracking-[0.2em] font-bold opacity-80">🎯 Theta Scanner · Today's Pick</div>
          <div className="flex items-baseline gap-3 mt-1 flex-wrap">
            <div className="text-3xl font-extrabold tabular-nums">{pick.ticker}</div>
            {pick.live_price !== undefined && pick.live_price !== null && (
              <div className="text-3xl font-extrabold tabular-nums opacity-95">
                ${pick.live_price.toFixed(2)}
              </div>
            )}
            {pick.live_pct !== undefined && pick.live_pct !== null && (
              <div className={`text-xl font-extrabold tabular-nums ${pick.live_pct >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
                {pick.live_pct >= 0 ? '+' : ''}{pick.live_pct.toFixed(2)}%
              </div>
            )}
            {(pick.live_price === undefined || pick.live_price === null) && (
              <div className="text-xs opacity-60 italic">live price unavailable</div>
            )}
          </div>
          <div className="text-xs opacity-80 mt-1">
            {pick.catalyst_reason}
            <span className="ml-2 opacity-60">· entry ${pick.entry?.toFixed(2)}</span>
          </div>
        </div>
        <div className="text-right">
          <div className="text-[10px] uppercase tracking-wider opacity-70">Score</div>
          <div className="text-2xl font-extrabold tabular-nums">{pick.score}</div>
        </div>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-3">
        <div><div className="text-[10px] uppercase tracking-wider opacity-70">Entry</div><div className="text-lg font-bold tabular-nums">${pick.entry?.toFixed(2)}</div></div>
        <div><div className="text-[10px] uppercase tracking-wider opacity-70">Stop</div><div className="text-lg font-bold tabular-nums text-rose-200">${pick.stop?.toFixed(2)}</div></div>
        <div><div className="text-[10px] uppercase tracking-wider opacity-70">Target</div><div className="text-lg font-bold tabular-nums text-emerald-200">${pick.target?.toFixed(2)}</div></div>
        <div><div className="text-[10px] uppercase tracking-wider opacity-70">R:R</div><div className="text-lg font-bold tabular-nums">{rr}×</div></div>
      </div>
      <div className="flex flex-wrap gap-2 text-[11px]">
        <span className="bg-white/20 px-2 py-1 rounded">Gap +{pick.gap_pct?.toFixed(1)}%</span>
        <span className="bg-white/20 px-2 py-1 rounded">{pick.rel_vol}× rel-vol</span>
        <span className="bg-white/20 px-2 py-1 rounded">Vol {(pick.today_vol/1e6).toFixed(1)}M</span>
      </div>
    </div>
  )
}

function ScannerHistory() {
  const [assetType, setAssetType] = useState<'options' | 'futures'>('options')
  const { data } = useQuery({
    queryKey: ['scanner-history', assetType],
    queryFn: () => api.get(`/api/v1/scanner/history?days=30&asset_type=${assetType}`).then(r => r.data),
    refetchInterval: 5 * 60_000,
  })
  const picks = data?.picks || []
  return (
    <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl overflow-hidden">
      <div className="px-5 py-4 border-b border-slate-200 dark:border-slate-800 flex items-center justify-between gap-3">
        <div>
          <h3 className="text-base font-extrabold text-slate-900 dark:text-slate-100">Last 30 days · Email signals sent</h3>
          <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5">Every pick the scanner emailed you, with outcome when resolved.</p>
        </div>
        <div className="flex gap-1 p-1 bg-slate-100 dark:bg-slate-800 rounded-lg">
          {(['options', 'futures'] as const).map(t => (
            <button key={t} onClick={() => setAssetType(t)}
              className={`px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider rounded-md transition-colors ${assetType === t ? 'bg-white dark:bg-slate-900 text-violet-700 dark:text-violet-300 shadow' : 'text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200'}`}>
              {t === 'options' ? '🎯 Options' : '⚡ Futures (prop)'}
            </button>
          ))}
        </div>
      </div>
      {picks.length === 0 ? (
        <div className="text-center py-12 text-sm text-slate-400 italic">
          {assetType === 'futures' ? 'No futures signals yet — coming when a prop-firm futures scanner ships.' : 'No picks yet. The first one lands at 9:25 ET tomorrow.'}
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="bg-slate-50 dark:bg-slate-900/50 text-[10px] uppercase tracking-wider text-slate-500">
              <tr>
                <th className="px-4 py-2.5 text-left">Date</th>
                <th className="px-4 py-2.5 text-left">Ticker</th>
                <th className="px-4 py-2.5 text-left">Entry</th>
                <th className="px-4 py-2.5 text-left">Stop</th>
                <th className="px-4 py-2.5 text-left">Target</th>
                <th className="px-4 py-2.5 text-left">Gap</th>
                <th className="px-4 py-2.5 text-left">Score</th>
                <th className="px-4 py-2.5 text-left">Catalyst</th>
                <th className="px-4 py-2.5 text-left">Outcome</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {picks.map((p: any) => {
                const out = p.outcome
                const outClass = out === 'win' ? 'bg-emerald-100 text-emerald-700' : out === 'loss' ? 'bg-rose-100 text-rose-700' : 'bg-slate-100 text-slate-500'
                return (
                  <tr key={p.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                    <td className="px-4 py-2 whitespace-nowrap text-slate-500">{new Date(p.picked_at).toLocaleDateString()}</td>
                    <td className="px-4 py-2 font-bold text-slate-900 dark:text-slate-100">{p.ticker}</td>
                    <td className="px-4 py-2 tabular-nums">${parseFloat(p.entry).toFixed(2)}</td>
                    <td className="px-4 py-2 tabular-nums text-rose-500">${parseFloat(p.stop).toFixed(2)}</td>
                    <td className="px-4 py-2 tabular-nums text-emerald-600">${parseFloat(p.target).toFixed(2)}</td>
                    <td className="px-4 py-2 tabular-nums">+{parseFloat(p.gap_pct || 0).toFixed(1)}%</td>
                    <td className="px-4 py-2 tabular-nums">{p.score}</td>
                    <td className="px-4 py-2 text-slate-500 max-w-[180px] truncate" title={p.catalyst_reason || ''}>{p.catalyst_reason || '—'}</td>
                    <td className="px-4 py-2">
                      <span className={`text-[10px] font-bold uppercase px-1.5 py-0.5 rounded ${outClass}`}>
                        {out || 'pending'}
                      </span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export default function AccountSignals() {
  const qc = useQueryClient()
  const { user } = useAuthStore()
  const isAdmin = !!(user as any)?.is_admin
  const [showSetup, setShowSetup] = useState(false)
  const [showSuppressed, setShowSuppressed] = useState(false)
  const [expandedSignalId, setExpandedSignalId] = useState<string | null>(null)
  const [form, setForm] = useState({
    account_label: '',
    strategy_id: '',
    instruments: ['ES'] as string[],
    channels: ['email'] as string[],
  })

  // Backend defaults to status='sent', so this is already a clean list.
  // Belt-and-suspenders filter below in case stale clients/caches sneak in
  // anything else.
  const { data: rawSignals = [] } = useQuery({ queryKey: ['account-signals'],   queryFn: () => accountSignalsApi.list().then(r => r.data),   refetchInterval: 30000 })
  const signals = (rawSignals as Signal[]).filter(s => s.status === 'sent')
  const { data: suppressedRows = [] } = useQuery({
    queryKey: ['account-signals-suppressed'],
    queryFn: () => accountSignalsApi.listSuppressed().then(r => r.data),
    enabled: isAdmin && showSuppressed,
    refetchInterval: 60000,
  })
  const { data: watchers = [] }  = useQuery({ queryKey: ['signal-watchers'],   queryFn: () => accountSignalsApi.listWatchers().then(r => r.data) })
  const { data: strategies = [] }= useQuery({ queryKey: ['strategies'],         queryFn: () => strategiesApi.list().then(r => r.data) })
  const { data: stats } = useQuery<{ total: number; wins: number; losses: number; expired: number; pending: number; resolved: number; win_rate: number; total_r: number; avg_r: number; excluded_outliers: number }>({ queryKey: ['signals-stats'], queryFn: () => api.get('/api/v1/account-signals/stats').then(r => r.data), refetchInterval: 60000 })

  const startMutation = useMutation({
    mutationFn: () => accountSignalsApi.startWatcher({
      strategy_id: form.strategy_id,
      instruments: form.instruments,
      account_label: form.account_label,
      channels: form.channels,
    }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['signal-watchers'] }); setShowSetup(false) },
  })
  const stopMutation = useMutation({
    mutationFn: (id: string) => accountSignalsApi.stopWatcher(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['signal-watchers'] }),
  })

  return (
    <div className="space-y-6 max-w-5xl mx-auto px-4 sm:px-6 py-6">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-extrabold text-slate-900 dark:text-slate-100 flex items-center gap-2">
            <Bell size={22}/> Email Signals
          </h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Email-only signals for futures, options, and stocks. Use this if you trade a prop-firm funded account (where automated trading is prohibited), an IRA, or simply want to confirm every trade by hand. The bot identifies the setup, emails you entry/stop/target, and you place the order yourself.
          </p>
        </div>
        <button onClick={() => setShowSetup(true)}
          className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2.5 rounded-xl text-sm font-semibold shadow-sm shadow-blue-200">
          <Plus size={14}/> New Email Signal
        </button>
      </div>

      {/* Compliance banner */}
      <div className="rounded-xl border border-amber-200 dark:border-amber-800/50 bg-amber-50 dark:bg-amber-900/20 p-4 flex items-start gap-3 text-sm">
        <ShieldCheck size={18} className="text-amber-600 dark:text-amber-400 flex-shrink-0 mt-0.5"/>
        <div className="text-amber-900 dark:text-amber-200">
          <strong>Three reasons to use Email Signals:</strong> (1) prop-firm accounts (Apex, Topstep, Take Profit Trader) prohibit automation; (2) IRAs and retirement accounts disallow algorithmic execution; (3) you want a human in the loop on every entry. You stay 100% compliant — <strong>a human places every order</strong>.
        </div>
      </div>

      {/* Active watchers */}
      <section>
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 mb-4" data-id="winrate-strip">
        <div className="bg-gradient-to-br from-violet-500 to-indigo-600 text-white rounded-2xl p-4">
          <div className="text-[10px] font-bold uppercase tracking-widest opacity-80">Win Rate</div>
          <div className="text-3xl font-extrabold tabular-nums mt-1">{stats?.win_rate ?? 0}%</div>
          <div className="text-[11px] opacity-80 mt-0.5">{stats?.resolved ?? 0} resolved</div>
        </div>
        <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-4">
          <div className="text-[10px] font-bold uppercase tracking-widest text-slate-400">Wins</div>
          <div className="text-2xl font-extrabold text-emerald-600 tabular-nums mt-1">{stats?.wins ?? 0}</div>
        </div>
        <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-4">
          <div className="text-[10px] font-bold uppercase tracking-widest text-slate-400">Losses</div>
          <div className="text-2xl font-extrabold text-red-500 tabular-nums mt-1">{stats?.losses ?? 0}</div>
        </div>
        <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-4">
          <div className="text-[10px] font-bold uppercase tracking-widest text-slate-400" title="Sum of R-multiples (outcome / planned risk) across ALL resolved trades — accumulates over time, not an average">Total R-multiple <span className="text-slate-300">(sum)</span></div>
          <div className={`text-2xl font-extrabold tabular-nums mt-1 ${(stats?.total_r ?? 0) >= 0 ? 'text-emerald-600' : 'text-red-500'}`}>
            {(stats?.total_r ?? 0) >= 0 ? '+' : ''}{stats?.total_r?.toFixed(2) ?? '0.00'}R
          </div>
          <div className="text-[10px] text-slate-400 mt-0.5" title="Average R per resolved trade. Healthy range: 0.5–3.0. Above 5 may indicate an outlier signal — see exclusions.">Average R: <span className="font-semibold text-slate-700 dark:text-slate-300">{stats?.avg_r?.toFixed(2) ?? '0.00'}</span> per resolved trade</div>
          {(stats?.excluded_outliers ?? 0) > 0 && (
            <div className="text-[10px] text-amber-600 dark:text-amber-400 mt-0.5" title="Signals with |outcome_r| > 20 were excluded from these aggregates as likely data errors (near-zero risk or wrong stop/target).">⚠ {stats?.excluded_outliers} outlier{stats?.excluded_outliers === 1 ? '' : 's'} excluded</div>
          )}
        </div>
        <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-4">
          <div className="text-[10px] font-bold uppercase tracking-widest text-slate-400">Pending</div>
          <div className="text-2xl font-extrabold text-slate-900 dark:text-slate-100 tabular-nums mt-1">{stats?.pending ?? 0}</div>
          <div className="text-[10px] text-slate-400 mt-0.5">{stats?.expired ?? 0} expired</div>
        </div>
      </div>
      <ScannerTodayCard/>

      <PendingOrdersCard/>

      <OpenPositionsCard/>

      <h2 className="text-sm font-bold uppercase tracking-widest text-slate-500 dark:text-slate-400 mb-3">Active Watchers</h2>
        {watchers.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-300 dark:border-slate-700 p-8 text-center text-sm text-slate-400 dark:text-slate-500">
            No watchers running. Click "New Email Signal" to subscribe a strategy to email alerts.
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {watchers.map((w: any) => (
              <div key={w.id} className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="font-bold text-slate-900 dark:text-slate-100 truncate">{w.account_label}</div>
                    <div className="text-xs text-slate-500 dark:text-slate-400 mt-0.5 truncate">{w.strategy_name} · {(w.instruments || []).join(', ')}</div>
                    <div className="text-[10px] text-slate-400 dark:text-slate-500 mt-1 flex items-center gap-2 flex-wrap">
                      {(w.channels || []).includes('email') && <span className="inline-flex items-center gap-1"><Mail size={10}/> email</span>}
                      {(w.channels || []).includes('push')  && <span className="inline-flex items-center gap-1"><Smartphone size={10}/> Push</span>}
                    </div>
                  </div>
                  <button onClick={() => stopMutation.mutate(w.id)}
                    className="text-xs font-semibold text-red-500 hover:text-red-600">Stop</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Signal feed */}
      <section>
        <div className="flex items-end justify-between mb-3 gap-3 flex-wrap">
          <h2 className="text-sm font-bold uppercase tracking-widest text-slate-500 dark:text-slate-400">Recent Signals Sent</h2>
          <div className="text-[11px] text-slate-400 dark:text-slate-500">
            Showing <span className="font-semibold text-slate-600 dark:text-slate-300">{signals.length}</span> sent signal{signals.length === 1 ? '' : 's'}
            {' '}— internal suppressed records (dead-zone, session cap, duplicates) are hidden
            {isAdmin && (
              <>
                {' · '}
                <button
                  type="button"
                  onClick={() => setShowSuppressed(v => !v)}
                  className="inline-flex items-center gap-1 font-semibold text-blue-600 hover:text-blue-700 underline-offset-2 hover:underline">
                  <ShieldAlert size={11}/> {showSuppressed ? 'Hide' : 'Show'} suppressed (admin)
                </button>
              </>
            )}
          </div>
        </div>
        {/* Outcome legend so the colour dots are self-explanatory */}
        <div className="flex items-center flex-wrap gap-x-4 gap-y-1 text-[10px] text-slate-500 dark:text-slate-400 mb-3">
          <span className="inline-flex items-center gap-1.5"><span className="inline-block w-2 h-2 rounded-full bg-emerald-500"/>Win (TP)</span>
          <span className="inline-flex items-center gap-1.5"><span className="inline-block w-2 h-2 rounded-full bg-rose-500"/>Loss (SL)</span>
          <span className="inline-flex items-center gap-1.5"><span className="inline-block w-2 h-2 rounded-full bg-slate-400 dark:bg-slate-500"/>Break even</span>
          <span className="inline-flex items-center gap-1.5"><span className="inline-block w-2 h-2 rounded-full bg-amber-400"/>Expired</span>
          <span className="inline-flex items-center gap-1.5"><span className="inline-block w-2 h-2 rounded-full bg-slate-300 dark:bg-slate-600"/>Pending</span>
        </div>
        {signals.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-300 dark:border-slate-700 p-8 text-center text-sm text-slate-400 dark:text-slate-500">
            No signals yet. When a watched strategy fires a setup it'll show up here and an email goes out.
          </div>
        ) : (
          <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-slate-100 dark:bg-slate-800 text-[11px] uppercase tracking-wider text-slate-500 dark:text-slate-400">
                <tr>
                  <th className="px-3 py-2.5 text-left font-semibold w-6"></th>
                  <th className="px-3 py-2.5 text-left font-semibold">Fired</th>
                  <th className="px-3 py-2.5 text-left font-semibold">Strategy</th>
                  <th className="px-3 py-2.5 text-left font-semibold">Symbol</th>
                  <th className="px-3 py-2.5 text-left font-semibold">Side</th>
                  <th className="px-3 py-2.5 text-left font-semibold">Entry</th>
                  <th className="px-3 py-2.5 text-left font-semibold">Stop</th>
                  <th className="px-3 py-2.5 text-left font-semibold">Target</th>
                  <th className="px-3 py-2.5 text-left font-semibold">Outcome</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {signals.map((s: Signal) => {
                  const isOpen = expandedSignalId === s.id
                  return (
                  <Fragment key={s.id}>
                  <tr
                    className="cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-800/40"
                    onClick={() => setExpandedSignalId(isOpen ? null : s.id)}
                  >
                    <td className="px-3 py-2.5 align-middle"><OutcomeDot outcome={s.outcome}/></td>
                    <td className="px-3 py-2.5 text-slate-700 dark:text-slate-200 whitespace-nowrap">{fmtEntryTime(s.fired_at)}</td>
                    <td className="px-3 py-2.5 text-slate-600 dark:text-slate-300 truncate max-w-[180px]">{s.strategy_name}</td>
                    <td className="px-3 py-2.5 font-bold text-slate-900 dark:text-slate-100">{s.instrument}</td>
                    <td className="px-3 py-2.5"><span className={`badge ${s.direction === 'long' ? 'badge-green' : 'badge-red'}`}>{s.direction.toUpperCase()}</span></td>
                    <td className="px-3 py-2.5 text-blue-600 font-semibold">{s.entry_price.toFixed(2)}</td>
                    <td className="px-3 py-2.5 text-red-500">{s.stop_loss.toFixed(2)}</td>
                    <td className="px-3 py-2.5 text-green-600">{s.take_profit.toFixed(2)}</td>
                    <td className="px-3 py-2.5 text-xs">
                      {s.outcome ? (
                        <span className="font-semibold capitalize text-slate-700 dark:text-slate-200">
                          {s.outcome}{typeof s.outcome_r === 'number' ? ` (${s.outcome_r >= 0 ? '+' : ''}${s.outcome_r.toFixed(2)}R)` : ''}
                        </span>
                      ) : (
                        <span className="text-slate-400 dark:text-slate-500">pending</span>
                      )}
                    </td>
                  </tr>
                  {isOpen && (
                    <tr className="bg-slate-50 dark:bg-slate-900/60">
                      <td colSpan={9} className="px-4 pb-4 pt-1">
                        {s.chart_b64 ? (
                          <img
                            src={`data:image/png;base64,${s.chart_b64}`}
                            className="rounded-lg mt-2 max-w-full"
                            alt="trade setup"
                          />
                        ) : (
                          <div className="text-xs text-slate-400 dark:text-slate-500 mt-2">No chart was captured for this signal.</div>
                        )}
                      </td>
                    </tr>
                  )}
                  </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        {/* Admin-only suppressed view */}
        {isAdmin && showSuppressed && (
          <div className="mt-4 rounded-xl border border-amber-300 dark:border-amber-800 bg-amber-50/40 dark:bg-amber-950/20 p-4">
            <div className="flex items-center gap-2 mb-2 text-amber-800 dark:text-amber-200">
              <ShieldAlert size={14}/>
              <span className="text-[11px] font-bold uppercase tracking-widest">Suppressed signals (admin diagnostics)</span>
              <span className="text-[10px] text-amber-700 dark:text-amber-300">{(suppressedRows as Signal[]).length} shown</span>
            </div>
            {(suppressedRows as Signal[]).length === 0 ? (
              <div className="text-[11px] text-amber-700 dark:text-amber-300">No suppressed rows in the current window.</div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead className="text-amber-900 dark:text-amber-200 text-[10px] uppercase tracking-wider">
                    <tr>
                      <th className="px-2 py-1 text-left">Fired</th>
                      <th className="px-2 py-1 text-left">Strategy</th>
                      <th className="px-2 py-1 text-left">Symbol</th>
                      <th className="px-2 py-1 text-left">Side</th>
                      <th className="px-2 py-1 text-left">Entry</th>
                      <th className="px-2 py-1 text-left">Dup. count</th>
                      <th className="px-2 py-1 text-left">Error / reason</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-amber-200/60 dark:divide-amber-800/40">
                    {(suppressedRows as Signal[]).map(s => (
                      <tr key={s.id}>
                        <td className="px-2 py-1 whitespace-nowrap text-slate-700 dark:text-slate-200">{fmtEntryTime(s.fired_at)}</td>
                        <td className="px-2 py-1 truncate max-w-[160px]">{s.strategy_name}</td>
                        <td className="px-2 py-1 font-semibold">{s.instrument}</td>
                        <td className="px-2 py-1 uppercase">{s.direction}</td>
                        <td className="px-2 py-1 text-blue-600">{s.entry_price.toFixed(2)}</td>
                        <td className="px-2 py-1">{s.duplicate_suppressed_count ?? '—'}</td>
                        <td className="px-2 py-1 text-slate-600 dark:text-slate-300 truncate max-w-[280px]" title={s.error_message || ''}>{s.error_message || (s.duplicate_suppressed_at ? 'duplicate' : '—')}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </section>

      {/* Quick how-it-works */}
      <section className="rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 p-5">
        <div className="flex items-center gap-2 mb-3">
          <Info size={16} className="text-blue-600"/>
          <h2 className="text-sm font-bold uppercase tracking-widest text-slate-700 dark:text-slate-200">How it works</h2>
        </div>
        <ol className="text-sm text-slate-700 dark:text-slate-300 list-decimal list-inside space-y-2 leading-relaxed">
          <li>Pick the strategy and instrument(s) you want to monitor.</li>
          <li>Label the funded account this is for (e.g. "Apex 50K Eval"). The bot tags every signal with this label so you know which account to enter on.</li>
          <li>Choose how to be notified — email always, push notification on the mobile app optional.</li>
          <li>The bot watches the market with the same strategy that runs in Paper Trading. When a setup fires, it sends you the entry / SL / TP within seconds. You place the order manually in your broker.</li>
          <li>Every signal is logged in the table above so you can review acted vs. missed.</li>
        </ol>
      </section>

      {/* Setup modal */}
      <ScannerHistory/>

      {showSetup && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-white dark:bg-slate-900 rounded-2xl shadow-2xl w-full max-w-md max-h-[90vh] flex flex-col">
            <div className="flex items-center justify-between px-6 py-5 border-b border-slate-200 dark:border-slate-800">
              <h2 className="font-bold text-slate-900 dark:text-slate-100">New Email Signal</h2>
              <button onClick={() => setShowSetup(false)} className="text-slate-400 hover:text-slate-700 text-lg">×</button>
            </div>
            <div className="px-6 py-5 space-y-4 overflow-y-auto flex-1">
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Account Label</label>
                <input value={form.account_label} onChange={e => setForm({ ...form, account_label: e.target.value })}
                  placeholder="e.g. Apex 50K Eval"
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"/>
                <p className="text-[10.5px] text-slate-400 mt-1">Used in the email subject so you know which account to enter on.</p>
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Strategy</label>
                <select value={form.strategy_id} onChange={e => setForm({ ...form, strategy_id: e.target.value })}
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100">
                  <option value="">Select a strategy...</option>
                  {strategies.map((s: any) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Instruments</label>
                <div className="flex gap-2 flex-wrap">
                  {['ES', 'NQ', 'RTY', 'YM'].map(inst => (
                    <button key={inst} type="button"
                      onClick={() => setForm(f => ({ ...f, instruments: f.instruments.includes(inst) ? f.instruments.filter(i => i !== inst) : [...f.instruments, inst] }))}
                      className={`px-3 py-1.5 rounded-lg text-xs font-semibold border transition ${form.instruments.includes(inst) ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-slate-600 border-slate-300 dark:bg-slate-800 dark:border-slate-700 dark:text-slate-300'}`}>
                      {inst}
                    </button>
                  ))}
                </div>
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Notify Me</label>
                <div className="space-y-2">
                  {[
                    { key: 'email', label: 'Email (always on)', icon: Mail, locked: true },
                    { key: 'push',  label: 'Push notification — Theta Algos mobile app', icon: Smartphone },
                  ].map(({ key, label, icon: Icon, locked }) => (
                    <label key={key} className="flex items-center gap-3 px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-800">
                      <input type="checkbox" disabled={locked}
                        checked={form.channels.includes(key)}
                        onChange={() => setForm(f => ({ ...f, channels: f.channels.includes(key) ? f.channels.filter(c => c !== key) : [...f.channels, key] }))}/>
                      <Icon size={14} className="text-slate-500"/>
                      <span className="text-sm text-slate-700 dark:text-slate-200">{label}</span>
                    </label>
                  ))}
                </div>
              </div>
            </div>
            <div className="flex gap-3 px-6 py-4 border-t border-slate-200 dark:border-slate-800">
              <button onClick={() => setShowSetup(false)} className="flex-1 border border-slate-200 text-slate-600 py-2.5 rounded-xl text-sm font-medium dark:text-slate-300 dark:border-slate-700">Cancel</button>
              <button onClick={() => startMutation.mutate()} disabled={!form.strategy_id || !form.account_label || form.instruments.length === 0 || startMutation.isPending}
                className="flex-1 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white py-2.5 rounded-xl text-sm font-semibold">
                {startMutation.isPending ? 'Starting…' : 'Start Watching'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
