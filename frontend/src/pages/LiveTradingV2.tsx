import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  Activity, TrendingUp, TrendingDown, DollarSign, Briefcase, X,
  Wallet, Zap, AlertCircle, ArrowLeft, RefreshCw, Calculator,
  Lock, Unlock, Pause, Play
} from 'lucide-react'
import { liveTradingApi, tradesApi, strategiesApi, dashboardApi } from '../api/endpoints'
import api from '../api/client'
import SizingModal from '../components/SizingModal'
import AddBrokerInline from '../components/AddBrokerInline'

type Period = 'today' | 'week' | 'month' | 'ytd'

// ── helpers ────────────────────────────────────────────────────────────
const fmt = (n: number, digits = 2) =>
  n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
const fmtUsd = (n: number, digits = 2) => `$${fmt(Math.abs(n), digits)}`
const pnlColor = (n: number) => (n > 0 ? 'text-emerald-600 dark:text-emerald-400' : n < 0 ? 'text-rose-600 dark:text-rose-400' : 'text-slate-500')
const pnlSign  = (n: number) => (n >= 0 ? '+' : '−')

// ── sparkline ─────────────────────────────────────────────────────────
function Sparkline({ data, color = '#10b981', height = 28 }: { data: number[]; color?: string; height?: number }) {
  if (!data || data.length < 2) return <div style={{ height }} className="w-full"/>
  const min = Math.min(...data)
  const max = Math.max(...data)
  const range = max - min || 1
  const step = 100 / (data.length - 1)
  const points = data.map((v, i) => `${i * step},${100 - ((v - min) / range) * 100}`).join(' ')
  return (
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="w-full" style={{ height }}>
      <polyline points={points} fill="none" stroke={color} strokeWidth="2"/>
    </svg>
  )
}

// ── tiny stat ────────────────────────────────────────────────────────
function MiniStat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-slate-400 dark:text-slate-500 font-bold">{label}</div>
      <div className={`text-base font-extrabold tabular-nums ${color || 'text-slate-900 dark:text-slate-100'}`}>{value}</div>
    </div>
  )
}

// ── portfolio header ─────────────────────────────────────────────────



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
  const { data, isError, isLoading } = useQuery({
    queryKey: ['scanner-open-positions'],
    queryFn: () => api.get('/api/v1/scanner/open-positions').then((r: any) => r.data),
    refetchInterval: 30_000,
    retry: 1,
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
  if (isError) {
    return (
      <div className="rounded-2xl border border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30 p-4 text-sm text-amber-800 dark:text-amber-200">
        Couldn't load open positions right now — we'll retry automatically. This does not mean you have no positions; check your broker if unsure.
      </div>
    )
  }
  if (!data) return isLoading ? (
    <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4 text-xs text-slate-400 animate-pulse">Loading open positions…</div>
  ) : null
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

function TodayPickCard() {
  const { data } = useQuery({
    queryKey: ['theta-today-pick'],
    queryFn: () => api.get('/api/v1/scanner/today-pick').then((r: any) => r.data),
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
        <div className="text-xs text-amber-700/80 dark:text-amber-400/80 mt-2">No trades during CPI / FOMC / NFP / PPI. Scanner resumes after the event.</div>
      </div>
    )
  }
  if (!pick) {
    return (
      <div className="rounded-2xl border border-dashed border-violet-300 dark:border-violet-800 bg-violet-50/30 dark:bg-violet-950/30 p-5 text-center">
        <div className="text-2xl mb-2">🎯</div>
        <div className="font-extrabold text-slate-700 dark:text-slate-200 mb-1">Theta Scanner — no pick today</div>
        <div className="text-xs text-slate-500 dark:text-slate-400">{data?.message || 'Scanner runs daily at 9:25 ET. No setup met the quality bar.'}</div>
      </div>
    )
  }
  const rrMult = ((pick.target - pick.entry) / (pick.entry - pick.stop)).toFixed(1)
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
        <div><div className="text-[10px] uppercase tracking-wider opacity-70">R:R</div><div className="text-lg font-bold tabular-nums">{rrMult}×</div></div>
      </div>
      <div className="flex flex-wrap gap-2 text-[11px]">
        <span className="bg-white/20 px-2 py-1 rounded">Gap +{pick.gap_pct?.toFixed(1)}%</span>
        <span className="bg-white/20 px-2 py-1 rounded">{pick.rel_vol}× rel-vol</span>
        <span className="bg-white/20 px-2 py-1 rounded">Vol {(pick.today_vol/1e6).toFixed(1)}M</span>
      </div>
      {pick.alternatives && pick.alternatives.length > 0 && (
        <div className="mt-3 pt-3 border-t border-white/20 text-[11px] opacity-80">
          Runners-up: {pick.alternatives.slice(0,4).map((a: any) => `${a.ticker} (${a.gap_pct}%)`).join(', ')}
        </div>
      )}
    </div>
  )
}

function PortfolioHeader({ data }: { data: any }) {
  if (!data) return null
  const sparkData = (data.equity_curve_14d || []).map((p: any) => p.pnl).reduce((acc: number[], v: number) => {
    acc.push((acc[acc.length - 1] || 0) + v); return acc
  }, [] as number[])
  const todayClass = pnlColor(data.today_pnl)
  return (
    <div className="rounded-3xl bg-gradient-to-br from-slate-900 via-slate-900 to-violet-950 dark:from-slate-950 dark:via-slate-950 dark:to-violet-950 text-white p-6 md:p-8 shadow-xl">
      <div className="flex items-center justify-between mb-5">
        <div>
          <div className="text-[10px] uppercase tracking-[0.2em] text-violet-300 font-bold mb-1">Equity · Net Liquidation Value</div>
          <div className="text-3xl md:text-4xl font-extrabold tabular-nums" title="Cash + market value of open positions. Pulled from your broker (cached).">{fmtUsd(data.total_equity)}</div>
          <div className="text-xs text-slate-400 mt-1">{data.accounts_count} broker {data.accounts_count === 1 ? 'account' : 'accounts'} linked · {data.healthy_accounts} healthy</div>
          {data.reconciliation && (
            <div className="text-[10px] text-slate-400 mt-2 leading-snug" title={data.reconciliation.notes}>
              start <span className="font-semibold text-slate-300">{fmtUsd(data.reconciliation.starting_equity, 0)}</span>
              {' · realized YTD '}<span className={`font-semibold ${pnlColor(data.reconciliation.realized_ytd_net)}`}>{pnlSign(data.reconciliation.realized_ytd_net)}{fmtUsd(Math.abs(data.reconciliation.realized_ytd_net), 0)}</span>
              {' · open '}<span className={`font-semibold ${pnlColor(data.reconciliation.unrealized_open)}`}>{pnlSign(data.reconciliation.unrealized_open)}{fmtUsd(Math.abs(data.reconciliation.unrealized_open), 0)}</span>
              {Math.abs(data.reconciliation.unexplained_gap) >= 0.5 && (
                <>{' · '}<span className="font-semibold text-amber-400" title="Equity change not explained by realized + open. Usually broker-side closes not in our trades table (e.g. flatten_all), un-recorded fees, or slippage vs recorded fills.">gap {pnlSign(data.reconciliation.unexplained_gap)}{fmtUsd(Math.abs(data.reconciliation.unexplained_gap), 0)}</span></>
              )}
            </div>
          )}
        </div>
        <div className="text-right">
          <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold mb-1">Today P&L (realized + open)</div>
          <div className={`text-2xl md:text-3xl font-extrabold tabular-nums ${todayClass}`}>
            {pnlSign((data.today_pnl || 0) + (data.today_unrealized_pnl || 0))}{fmtUsd((data.today_pnl || 0) + (data.today_unrealized_pnl || 0))}
          </div>
          <div className="text-[10px] text-slate-400 mt-1 leading-snug">
            <span>realized {pnlSign(data.today_pnl)}{fmtUsd(data.today_pnl, 0)}</span>
            {data.today_unrealized_pnl !== undefined && data.today_unrealized_pnl !== 0 && (
              <span className="ml-2">· open {pnlSign(data.today_unrealized_pnl)}{fmtUsd(data.today_unrealized_pnl, 0)}</span>
            )}
          </div>
        </div>
      </div>

      {/* Sparkline */}
      {sparkData.length > 1 && (
        <div className="mt-4 -mx-2 opacity-80">
          <Sparkline data={sparkData} color="#a78bfa" height={48}/>
          <div className="flex justify-between text-[10px] text-slate-400 mt-1 px-2">
            <span>14 days ago</span><span>now</span>
          </div>
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mt-5 pt-5 border-t border-white/10">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Buying power</div>
          <div className="text-lg font-bold tabular-nums">{fmtUsd(data.total_buying_power, 0)}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Open positions</div>
          <div className="text-lg font-bold tabular-nums">{data.open_positions_count}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Week</div>
          <div className={`text-lg font-bold tabular-nums ${pnlColor(data.week_pnl)}`}>{pnlSign(data.week_pnl)}{fmtUsd(data.week_pnl, 0)}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Month</div>
          <div className={`text-lg font-bold tabular-nums ${pnlColor(data.month_pnl)}`}>{pnlSign(data.month_pnl)}{fmtUsd(data.month_pnl, 0)}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">YTD (realized + open)</div>
          <div className={`text-lg font-bold tabular-nums ${pnlColor((data.ytd_pnl || 0) + (data.total_unrealized_pnl || 0))}`}>{pnlSign((data.ytd_pnl || 0) + (data.total_unrealized_pnl || 0))}{fmtUsd(Math.abs((data.ytd_pnl || 0) + (data.total_unrealized_pnl || 0)), 0)}</div>
          <div className="text-[9px] text-slate-500 mt-0.5 leading-tight">
            <span>realized {pnlSign(data.ytd_pnl)}{fmtUsd(Math.abs(data.ytd_pnl || 0), 0)}</span>
            {data.total_unrealized_pnl !== undefined && data.total_unrealized_pnl !== 0 && (
              <span className="ml-1">· open {pnlSign(data.total_unrealized_pnl)}{fmtUsd(Math.abs(data.total_unrealized_pnl), 0)}</span>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

// ── broker card ──────────────────────────────────────────────────────
function BrokerCard({ acct, onSizing }: { acct: any; onSizing: () => void }) {
  const qc = useQueryClient()
  const toggleTrading = useMutation({
    mutationFn: (enabled: boolean) => api.put(`/api/v1/live-trading/broker-accounts/${acct.id}/trading-enabled`, { enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['portfolio-summary'] }),
  })
  const tradingOn = acct.trading_enabled !== false

  const stale = acct.is_stale
  const dot = stale ? 'bg-amber-500' : 'bg-emerald-500'
  const dotLabel = stale ? 'cached' : 'live'
  return (
    <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-5 hover:shadow-lg transition-shadow">
      <div className="flex items-start justify-between mb-3">
        <div className="flex items-center gap-3 min-w-0">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-violet-100 to-violet-200 dark:from-violet-900/40 dark:to-violet-800/40 text-violet-700 dark:text-violet-300 flex items-center justify-center font-extrabold text-sm flex-shrink-0">
            {(acct.broker || '?')[0].toUpperCase()}
          </div>
          <div className="min-w-0">
            <div className="font-bold text-sm text-slate-900 dark:text-slate-100 truncate">{acct.account_name}
      <button data-id="trading-toggle" onClick={(e) => { e.stopPropagation(); toggleTrading.mutate(!tradingOn) }} disabled={toggleTrading.isPending}
        className={`ml-2 px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider ${tradingOn ? "bg-emerald-500 text-white" : "bg-slate-300 dark:bg-slate-700 text-slate-700 dark:text-slate-300"}`}
        title={tradingOn ? "Trading enabled — click to pause" : "Trading paused — click to enable"}>
        {tradingOn ? "● Trading" : "○ Paused"}
      </button></div>
            <div className="text-[11px] text-slate-500 dark:text-slate-400 truncate flex items-center gap-1.5">
              {acct.broker} · <span className="inline-flex items-center gap-0.5">{acct.account_type === 'margin' ? <Unlock size={9}/> : <Lock size={9}/>}{acct.account_type}</span>
              {acct.sandbox_mode && <span className="px-1 py-0.5 bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300 rounded text-[9px] font-bold">SANDBOX</span>}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          <span className={`w-2 h-2 rounded-full ${dot}`}/>
          <span className="text-[10px] text-slate-500 dark:text-slate-400">{dotLabel}</span>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2 mb-4">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-slate-400 dark:text-slate-500 font-bold">Equity</div>
          <div className="text-lg font-extrabold text-slate-900 dark:text-slate-100 tabular-nums">{fmtUsd(acct.equity, 0)}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-slate-400 dark:text-slate-500 font-bold">Buying power</div>
          <div className="text-lg font-extrabold text-slate-900 dark:text-slate-100 tabular-nums">{fmtUsd(acct.buying_power, 0)}</div>
        </div>
      </div>

      <div className="flex gap-2 pt-3 border-t border-slate-100 dark:border-slate-800">
        <button onClick={onSizing} className="flex-1 inline-flex items-center justify-center gap-1.5 px-2 py-1.5 rounded-lg text-[11px] font-bold text-violet-700 dark:text-violet-300 bg-violet-50 dark:bg-violet-900/30 hover:bg-violet-100 dark:hover:bg-violet-900/50">
          <Calculator size={11}/> Sizing
        </button>
        <Link to={`/app/live/${acct.id}`} className="flex-1 inline-flex items-center justify-center gap-1.5 px-2 py-1.5 rounded-lg text-[11px] font-bold text-slate-700 dark:text-slate-200 bg-slate-100 dark:bg-slate-800 hover:bg-slate-200 dark:hover:bg-slate-700">
          <Activity size={11}/> Details
        </Link>
      </div>
    </div>
  )
}

// ── trade stats strip ────────────────────────────────────────────────
function StatsStrip({ trades }: { trades: any[] }) {
  if (!trades || trades.length === 0) {
    return <div className="text-center py-10 text-sm text-slate-400 dark:text-slate-500">No trades yet for this period.</div>
  }
  const wins = trades.filter(t => (t.pnl || 0) > 0)
  const losses = trades.filter(t => (t.pnl || 0) < 0)
  const winRate = trades.length > 0 ? (wins.length / trades.length) * 100 : 0
  const avgWin = wins.length > 0 ? wins.reduce((s, t) => s + (t.pnl || 0), 0) / wins.length : 0
  const avgLoss = losses.length > 0 ? Math.abs(losses.reduce((s, t) => s + (t.pnl || 0), 0) / losses.length) : 0
  const totalWin = wins.reduce((s, t) => s + (t.pnl || 0), 0)
  const totalLoss = Math.abs(losses.reduce((s, t) => s + (t.pnl || 0), 0))
  const pf = totalLoss > 0 ? totalWin / totalLoss : (totalWin > 0 ? 99 : 0)
  const expectancy = (winRate / 100) * avgWin - ((100 - winRate) / 100) * avgLoss
  const totalPnl = trades.reduce((s, t) => s + (t.pnl || 0), 0)
  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-3 mb-4 p-4 rounded-2xl bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-800">
      <MiniStat label="Total trades" value={String(trades.length)}/>
      <MiniStat label="Win rate" value={`${winRate.toFixed(0)}%`} color={winRate >= 50 ? 'text-emerald-600 dark:text-emerald-400' : 'text-slate-900 dark:text-slate-100'}/>
      <MiniStat label="Profit factor" value={pf >= 99 ? '∞' : pf.toFixed(2)} color={pf >= 1.5 ? 'text-emerald-600 dark:text-emerald-400' : 'text-slate-900 dark:text-slate-100'}/>
      <MiniStat label="Avg win" value={`+${fmtUsd(avgWin, 0)}`} color="text-emerald-600 dark:text-emerald-400"/>
      <MiniStat label="Avg loss" value={`−${fmtUsd(avgLoss, 0)}`} color="text-rose-600 dark:text-rose-400"/>
      <MiniStat label="Expectancy / trade" value={`${expectancy >= 0 ? '+' : '−'}${fmtUsd(expectancy, 2)}`} color={pnlColor(expectancy)}/>
      <MiniStat label="Total P&L" value={`${totalPnl >= 0 ? '+' : '−'}${fmtUsd(totalPnl, 0)}`} color={pnlColor(totalPnl)}/>
    </div>
  )
}

function filterByPeriod(t: any, period: Period): boolean {
  const ref = t.exit_time || t.entry_time
  if (!ref) return false
  const d = new Date(ref)
  const now = new Date()
  if (period === 'today') return d.toDateString() === now.toDateString()
  if (period === 'week') {
    const wStart = new Date(now); wStart.setDate(now.getDate() - now.getDay())
    return d >= wStart
  }
  if (period === 'month') return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth()
  return d.getFullYear() === now.getFullYear()
}

// ── main ─────────────────────────────────────────────────────────────
export default function LiveTradingV2() {
  const [tab, setTab] = useState<'positions' | 'history'>('history')
  const [period, setPeriod] = useState<Period>('today')
  const [sizingAccount, setSizingAccount] = useState<any | null>(null)
  const [showDeploy, setShowDeploy] = useState(false)
  const [assetType, setAssetType] = useState<'futures' | 'options' | 'stocks'>('futures')
  const [deployForm, setDeployForm] = useState({ strategy_id: '', broker_account_id: '', instrument: 'ES' })
  const [deployError, setDeployError] = useState<string | null>(null)
  const [showConnect, setShowConnect] = useState(false)

  const { data: portfolio, refetch: refetchPortfolio, isFetching: pFetching } = useQuery({
    queryKey: ['portfolio-summary'],
    queryFn: () => liveTradingApi.portfolioSummary().then((r: any) => r.data),
    refetchInterval: 30000,
  })
  const { data: liveTrades = [] } = useQuery({
    queryKey: ['live-trades'],
    queryFn: () => tradesApi.list({ mode: 'live', limit: 1000 }).then((r: any) => r.data),
    refetchInterval: 30000,
  })
  const { data: strategies = [] } = useQuery({
    queryKey: ['strategies'],
    queryFn: () => strategiesApi.list().then((r: any) => r.data),
  })
  const { data: liveSessions = [] } = useQuery({
    queryKey: ['live-sessions'],
    queryFn: () => (liveTradingApi as any).listSessions().then((r: any) => r.data),
    refetchInterval: 30000,
  })
  const { data: unrealized } = useQuery({
    queryKey: ['unrealized-pnl'],
    queryFn: () => (liveTradingApi as any).unrealizedPnl().then((r: any) => r.data),
    refetchInterval: 15000,
  })
  const { data: biasData } = useQuery({
    queryKey: ['daily-bias-live'],
    queryFn: () => dashboardApi.bias().then((r: any) => r.data),
    refetchInterval: 5 * 60 * 1000,
  })
  const qc = useQueryClient()
  const killSwitchMutation = useMutation({
    mutationFn: (id: string) => liveTradingApi.killSwitch(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['live-sessions'] }),
  })
  const pauseMutation = useMutation({
    mutationFn: (id: string) => (liveTradingApi as any).pauseSession(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['live-sessions'] }),
  })
  const resumeMutation = useMutation({
    mutationFn: (id: string) => (liveTradingApi as any).resumeSession(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['live-sessions'] }),
  })
  const startSessionMutation = useMutation({
    mutationFn: () => liveTradingApi.startSession(deployForm),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['live-sessions'] })
      qc.invalidateQueries({ queryKey: ['portfolio-summary'] })
      setShowDeploy(false)
      setDeployForm({ strategy_id: '', broker_account_id: '', instrument: 'ES' })
      setDeployError(null)
    },
    onError: (e: any) => setDeployError(e?.response?.data?.detail || 'Failed to start session.'),
  })

  const closeOneTrade = useMutation({
    mutationFn: (id: string) => api.post(`/api/v1/scanner/close-trade/${id}`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['live-trades'] }); qc.invalidateQueries({ queryKey: ['unrealized-pnl'] }); qc.invalidateQueries({ queryKey: ['scanner-open-positions'] }) },
  })
  const closeAllOpenTrades = useMutation({
    mutationFn: () => api.post('/api/v1/scanner/force-close-all'),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['live-trades'] }); qc.invalidateQueries({ queryKey: ['unrealized-pnl'] }) },
  })
  const openPositions = liveTrades.filter((t: any) => t.status === 'open' || t.status === 'pending')
  const closedTrades = liveTrades.filter((t: any) => t.status === 'closed')
  const filteredTrades = closedTrades.filter((t: any) => filterByPeriod(t, period))

  return (
    <div className="space-y-6 max-w-7xl mx-auto px-4 sm:px-6 py-6">
      {/* Top nav row */}
      <div className="flex items-center justify-end gap-2">
        <button onClick={() => setShowDeploy(true)} className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold text-white bg-violet-600 hover:bg-violet-700 shadow-lg shadow-violet-900/20">
          <Zap size={11}/> Deploy strategy
        </button>
        <button onClick={() => refetchPortfolio()} disabled={pFetching}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold text-violet-700 dark:text-violet-300 bg-violet-50 dark:bg-violet-900/30 hover:bg-violet-100 dark:hover:bg-violet-900/50 disabled:opacity-50">
          <RefreshCw size={11} className={pFetching ? 'animate-spin' : ''}/> Refresh
        </button>
      </div>

      <TodayPickCard/>

      <PendingOrdersCard/>

      <OpenPositionsCard/>

      <PortfolioHeader data={portfolio}/>

      {/* Daily Bias — futures bias for the live trader's quick context */}
      {biasData?.biases && biasData.biases.length > 0 && (
        <div>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-extrabold uppercase tracking-wider text-slate-700 dark:text-slate-200">Daily Bias · Futures</h2>
            <Link to="/app/bias" className="text-xs text-violet-600 dark:text-violet-400 hover:underline font-bold">Full detail →</Link>
          </div>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            {biasData.biases.map((b: any) => {
              const isBull = b.bias?.includes('bullish')
              const isBear = b.bias?.includes('bearish')
              const dot = isBull ? 'bg-emerald-500' : isBear ? 'bg-rose-500' : 'bg-slate-400'
              const accent = isBull ? 'text-emerald-600 dark:text-emerald-400' : isBear ? 'text-rose-600 dark:text-rose-400' : 'text-slate-500'
              return (
                <Link key={b.instrument} to="/app/bias" className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4 hover:border-violet-300 dark:hover:border-violet-700 hover:shadow-lg transition-all">
                  <div className="flex items-center justify-between mb-2">
                    <span className="font-extrabold text-base text-slate-900 dark:text-slate-100">{b.instrument}</span>
                    <span className="flex items-center gap-1.5">
                      <span className={`w-2 h-2 rounded-full ${dot}`}/>
                      <span className={`text-[11px] font-bold uppercase tracking-wider ${accent}`}>{(b.bias || '').replace('_', ' ')}</span>
                    </span>
                  </div>
                  <div className="text-xs text-slate-500 dark:text-slate-400 mb-2 tabular-nums">
                    {b.last_close ? `$${b.last_close.toLocaleString()}` : '—'}
                    <span className="ml-2 text-[10px] uppercase tracking-wider text-slate-400">{b.current_session || ''}</span>
                  </div>
                  {b.narrative && (
                    <p className="text-[11px] text-slate-600 dark:text-slate-300 leading-snug line-clamp-2">{b.narrative}</p>
                  )}
                  {b.draw_target && (
                    <div className="text-[10px] text-violet-600 dark:text-violet-400 font-bold mt-1.5">
                      → {b.draw_target.label} ${typeof b.draw_target.level === 'number' ? b.draw_target.level.toLocaleString() : '—'}
                    </div>
                  )}
                </Link>
              )
            })}
          </div>
        </div>
      )}

      {/* Broker accounts grid */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-extrabold uppercase tracking-wider text-slate-700 dark:text-slate-200">Broker accounts</h2>
          <button onClick={() => setShowConnect(true)} className="text-xs text-violet-600 dark:text-violet-400 hover:underline font-bold">+ Connect new</button>
        </div>
        {portfolio?.per_account?.length ? (
          <div className="grid sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
            {portfolio.per_account.map((a: any) => (
              <BrokerCard key={a.id} acct={a} onSizing={() => setSizingAccount(a)}/>
            ))}
          </div>
        ) : (
          <div className="rounded-2xl border border-dashed border-slate-300 dark:border-slate-700 p-10 text-center">
            <Briefcase size={32} className="mx-auto text-slate-300 dark:text-slate-600 mb-3"/>
            <p className="font-bold text-slate-600 dark:text-slate-300 mb-1">No broker accounts linked</p>
            <p className="text-xs text-slate-500 dark:text-slate-400 mb-3">Link a Tradier, Tradovate, or Webull account to start trading live.</p>
            <button onClick={() => setShowConnect(true)} className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-xs font-bold bg-violet-600 hover:bg-violet-700 text-white">
              Connect broker
            </button>
          </div>
        )}
      </div>

      {/* Unrealized PnL across open positions */}
      {unrealized && unrealized.open_count > 0 && (
        <div className="rounded-2xl bg-gradient-to-r from-slate-900 to-violet-950 dark:from-slate-950 dark:to-violet-950 text-white p-5">
          <div className="flex items-baseline justify-between mb-2">
            <div>
              <div className="text-[10px] uppercase tracking-[0.2em] text-violet-300 font-bold mb-1">Open positions · live mark</div>
              <div className="text-sm text-slate-400">{unrealized.open_count} open · refreshed every 15s</div>
            </div>
            <div className="text-right">
              <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Total unrealized</div>
              <div className={`text-3xl font-extrabold tabular-nums ${unrealized.total_unrealized > 0 ? 'text-emerald-400' : unrealized.total_unrealized < 0 ? 'text-rose-400' : 'text-white'}`}>
                {unrealized.total_unrealized >= 0 ? '+' : '−'}${Math.abs(unrealized.total_unrealized).toLocaleString(undefined, {maximumFractionDigits: 2})}
              </div>
            </div>
          </div>
          <div className="grid sm:grid-cols-2 md:grid-cols-3 gap-2 mt-3">
            {unrealized.positions.map((p: any) => (
              <div key={p.trade_id} className="bg-white/10 rounded-xl p-3">
                <div className="flex items-center justify-between mb-1">
                  <span className="font-bold text-sm">{p.instrument}</span>
                  <span className={`text-[10px] font-bold uppercase px-1.5 py-0.5 rounded ${p.direction === 'long' ? 'bg-emerald-500/30 text-emerald-300' : 'bg-rose-500/30 text-rose-300'}`}>{p.direction}</span>
                </div>
                <div className="text-[11px] text-slate-300 tabular-nums">{p.contracts}× @ ${p.entry_price?.toFixed(2)}</div>
                <div className={`text-base font-extrabold tabular-nums mt-1 ${p.unrealized_pnl > 0 ? 'text-emerald-400' : p.unrealized_pnl < 0 ? 'text-rose-400' : 'text-white'}`}>
                  {p.unrealized_pnl >= 0 ? '+' : '−'}${Math.abs(p.unrealized_pnl).toFixed(2)}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Active sessions */}
      {liveSessions.length > 0 && (
        <div>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-extrabold uppercase tracking-wider text-slate-700 dark:text-slate-200">Active sessions</h2>
            <span className="text-[11px] text-slate-400 dark:text-slate-500">{liveSessions.filter((x: any) => x.is_active).length} running</span>
          </div>
          <div className="space-y-2">
            {liveSessions.filter((sess: any) => sess.is_active).map((sess: any) => (
              <div key={sess.id} className="flex items-center justify-between gap-3 rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4 hover:shadow-md transition-shadow">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"/>
                    <span className="font-bold text-sm text-slate-900 dark:text-slate-100 truncate">{sess.strategy_name}</span>
                    <span className="text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300">{sess.instrument}</span>
                  </div>
                  <div className="text-[11px] text-slate-500 dark:text-slate-400 mt-0.5 truncate">
                    {sess.broker} · {sess.broker_account_name} · {sess.total_trades} trades · P&L {sess.net_pnl >= 0 ? '+' : '−'}${Math.abs(sess.net_pnl).toFixed(0)}
                  </div>
                </div>
                <button onClick={() => killSwitchMutation.mutate(sess.id)} disabled={killSwitchMutation.isPending}
                  className="px-3 py-1.5 rounded-lg text-[11px] font-bold text-rose-600 dark:text-rose-400 hover:bg-rose-50 dark:hover:bg-rose-900/20 border border-rose-200 dark:border-rose-900 disabled:opacity-50">
                  Kill switch
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Tabs: Open positions / History */}
      <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 overflow-hidden">
        <div className="flex border-b border-slate-200 dark:border-slate-700">
          <button onClick={() => setTab('positions')} className={`flex-1 px-4 py-3 text-sm font-bold transition-colors ${tab === 'positions' ? 'text-violet-700 dark:text-violet-300 border-b-2 border-violet-500 bg-violet-50/40 dark:bg-violet-900/20' : 'text-slate-500 dark:text-slate-400 hover:bg-slate-50 dark:hover:bg-slate-800'}`}>
            Open positions <span className="ml-1 text-[10px] bg-slate-100 dark:bg-slate-800 px-1.5 py-0.5 rounded">{openPositions.length}</span>
          </button>
          <button onClick={() => setTab('history')} className={`flex-1 px-4 py-3 text-sm font-bold transition-colors ${tab === 'history' ? 'text-violet-700 dark:text-violet-300 border-b-2 border-violet-500 bg-violet-50/40 dark:bg-violet-900/20' : 'text-slate-500 dark:text-slate-400 hover:bg-slate-50 dark:hover:bg-slate-800'}`}>
            Trade history <span className="ml-1 text-[10px] bg-slate-100 dark:bg-slate-800 px-1.5 py-0.5 rounded">{closedTrades.length}</span>
          </button>
        </div>

        <div className="p-4">
          {tab === 'positions' ? (
            openPositions.length === 0 ? (
              <div className="py-10 text-center text-sm text-slate-400 dark:text-slate-500">No open positions.</div>
            ) : (
              <>
              <div className="flex justify-end mb-3">
                <button onClick={() => { if (confirm(`Close ALL ${openPositions.length} open positions at market?`)) closeAllOpenTrades.mutate() }}
                  disabled={closeAllOpenTrades.isPending}
                  className="bg-rose-600 hover:bg-rose-700 disabled:opacity-50 text-white text-xs font-bold px-3 py-1.5 rounded-lg">
                  {closeAllOpenTrades.isPending ? 'Closing...' : `Close all ${openPositions.length}`}
                </button>
              </div>
              <table className="w-full text-xs">
                <thead className="text-[10px] uppercase tracking-wider text-slate-400 dark:text-slate-500 font-bold">
                  <tr>
                    <th className="px-2 py-2 text-left">Ticker</th>
                    <th className="px-2 py-2 text-left">Side</th>
                    <th className="px-2 py-2 text-right">Qty</th>
                    <th className="px-2 py-2 text-right">Entry</th>
                    <th className="px-2 py-2 text-right">Stop</th>
                    <th className="px-2 py-2 text-right">Target</th>
                    <th className="px-2 py-2 text-left">Strategy</th>
                    <th className="px-2 py-2 text-right">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                  {openPositions.map((t: any) => {
                    const sName = strategies.find((s: any) => s.id === t.strategy_id)?.name || '—'
                    return (
                      <tr key={t.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                        <td className="px-2 py-2 font-bold text-slate-900 dark:text-slate-100">{t.instrument}</td>
                        <td className="px-2 py-2"><span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${t.direction === 'long' ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300' : 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-300'}`}>{t.direction?.toUpperCase()}</span></td>
                        <td className="px-2 py-2 text-right tabular-nums">{t.contracts}</td>
                        <td className="px-2 py-2 text-right tabular-nums">{fmtUsd(t.entry_price || 0)}</td>
                        <td className="px-2 py-2 text-right tabular-nums text-rose-600 dark:text-rose-400">{fmtUsd(t.stop_loss || 0)}</td>
                        <td className="px-2 py-2 text-right tabular-nums text-emerald-600 dark:text-emerald-400">{fmtUsd(t.take_profit || 0)}</td>
                        <td className="px-2 py-2 text-slate-500 dark:text-slate-400 truncate max-w-[160px]">{sName}</td>
                        <td className="px-2 py-2 text-right">
                          <button onClick={() => { if (confirm(`Close ${t.instrument} (${t.contracts} sh) at market?`)) closeOneTrade.mutate(t.id) }}
                            disabled={closeOneTrade.isPending}
                            className="bg-rose-600 hover:bg-rose-700 disabled:opacity-50 text-white text-[10px] font-bold px-2 py-1 rounded">
                            Close
                          </button>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
              </>
            )
          ) : (
            <>
              <div className="flex items-center gap-1 mb-3 inline-flex rounded-lg bg-slate-100 dark:bg-slate-800 p-0.5">
                {(['today', 'week', 'month', 'ytd'] as Period[]).map(p => (
                  <button key={p} onClick={() => setPeriod(p)} className={`px-3 py-1 text-[11px] font-bold rounded ${period === p ? 'bg-white dark:bg-slate-900 text-violet-700 dark:text-violet-300 shadow-sm' : 'text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200'}`}>
                    {p.toUpperCase()}
                  </button>
                ))}
              </div>
              <StatsStrip trades={filteredTrades}/>
              {filteredTrades.length > 0 && (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead className="text-[10px] uppercase tracking-wider text-slate-400 dark:text-slate-500 font-bold">
                      <tr>
                        <th className="px-2 py-2 text-left">When</th>
                        <th className="px-2 py-2 text-left">Ticker</th>
                        <th className="px-2 py-2 text-left">Side</th>
                        <th className="px-2 py-2 text-right">Entry</th>
                        <th className="px-2 py-2 text-right">Exit</th>
                        <th className="px-2 py-2 text-right">Qty</th>
                        <th className="px-2 py-2 text-right">P&L</th>
                        <th className="px-2 py-2 text-left">Reason</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                      {filteredTrades.slice().sort((a: any, b: any) => new Date(b.exit_time || b.entry_time).getTime() - new Date(a.exit_time || a.entry_time).getTime()).map((t: any) => (
                        <tr key={t.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                          <td className="px-2 py-2 text-slate-500 dark:text-slate-400 whitespace-nowrap">{new Date(t.exit_time || t.entry_time).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}</td>
                          <td className="px-2 py-2 font-bold text-slate-900 dark:text-slate-100">{t.instrument}</td>
                          <td className="px-2 py-2"><span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${t.direction === 'long' ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300' : 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-300'}`}>{(t.direction || '').toUpperCase()}</span></td>
                          <td className="px-2 py-2 text-right tabular-nums">{fmtUsd(t.entry_price || 0)}</td>
                          <td className="px-2 py-2 text-right tabular-nums">{fmtUsd(t.exit_price || 0)}</td>
                          <td className="px-2 py-2 text-right tabular-nums">{t.contracts}</td>
                          <td className={`px-2 py-2 text-right tabular-nums font-bold ${pnlColor(t.pnl || 0)}`}>{pnlSign(t.pnl || 0)}{fmtUsd(t.pnl || 0)}</td>
                          <td className="px-2 py-2 text-slate-500 dark:text-slate-400 truncate max-w-[140px]">{t.exit_reason || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>
      </div>

      <AddBrokerInline open={showConnect} onClose={() => setShowConnect(false)}/>

      {sizingAccount && (
        <SizingModal account={sizingAccount} onClose={() => setSizingAccount(null)}/>
      )}

      {/* Deploy Strategy modal */}
      {showDeploy && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4" onClick={() => setShowDeploy(false)}>
          <div onClick={(e) => e.stopPropagation()} className="bg-white dark:bg-slate-900 rounded-2xl shadow-2xl w-full max-w-md">
            <div className="flex items-center justify-between px-6 py-5 border-b border-slate-100 dark:border-slate-800">
              <div>
                <div className="text-[10px] uppercase tracking-[0.2em] text-violet-500 dark:text-violet-400 font-bold mb-0.5">New session</div>
                <h2 className="text-lg font-extrabold text-slate-900 dark:text-slate-100">Deploy strategy live</h2>
              </div>
              <button onClick={() => setShowDeploy(false)} className="p-1.5 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-400 dark:text-slate-500"><X size={18}/></button>
            </div>
            <div className="px-6 py-5 space-y-4">
              {deployError && (
                <div className="bg-rose-50 dark:bg-rose-900/20 border border-rose-200 dark:border-rose-900 text-rose-600 dark:text-rose-300 text-xs px-3 py-2 rounded-lg flex items-center gap-2">
                  <AlertCircle size={13}/> {deployError}
                </div>
              )}
              {/* Asset type — pick what kind of instrument to trade */}
              <div>
                <label className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 block mb-1.5">Asset class</label>
                <div className="grid grid-cols-3 gap-1 p-1 bg-slate-100 dark:bg-slate-800 rounded-lg">
                  {(['futures', 'options', 'stocks'] as const).map(t => (
                    <button key={t} type="button" onClick={() => setAssetType(t)}
                      className={`px-2 py-1.5 text-[11px] font-bold uppercase tracking-wider rounded-md transition-colors ${assetType === t ? 'bg-white dark:bg-slate-900 text-violet-700 dark:text-violet-300 shadow' : 'text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200'}`}>
                      {t === 'futures' ? '⚡ Futures' : t === 'options' ? '🎯 Options' : '📈 Stocks'}
                    </button>
                  ))}
                </div>
                <p className="text-[10px] text-slate-400 dark:text-slate-500 mt-1.5">
                  {assetType === 'futures' ? 'ES/NQ/RTY/YM and their micros (MES/MNQ/M2K/MYM). Min $5,000 recommended.' :
                   assetType === 'options' ? 'Call/put contracts on SPY, QQQ, NVDA, AAPL, etc. Min $2,000 recommended.' :
                   'Outright stock shares — any liquid US ticker. No minimum.'}
                </p>
              </div>

              <div>
                <label className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 block mb-1.5">Strategy</label>
                <select value={deployForm.strategy_id} onChange={(e) => setDeployForm({...deployForm, strategy_id: e.target.value})}
                  className="w-full border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-2 text-sm bg-white dark:bg-slate-800">
                  <option value="">{strategies.length ? `Select a strategy...` : 'Loading...'}</option>
                  {(() => {
                    const active = strategies.filter((st: any) => (st.status || '').toLowerCase() === 'active')
                    const isFutures = (st: any) => (st.instruments || []).some((i: string) => ['ES','NQ','RTY','YM','MES','MNQ','M2K','MYM'].includes(i))
                    const isOptions = (st: any) => (st.name || '').toLowerCase().includes('option') || (st.options_mode)
                    let filtered = active
                    if (assetType === 'futures') filtered = active.filter(isFutures)
                    else if (assetType === 'options') filtered = active.filter(isOptions)
                    else filtered = active.filter((st: any) => !isFutures(st) && !isOptions(st))
                    return filtered.map((st: any) => <option key={st.id} value={st.id}>{st.name}</option>)
                  })()}
                </select>
                {strategies.length === 0 && (
                  <p className="text-[11px] text-amber-600 dark:text-amber-400 mt-1.5">
                    No strategies — <Link to="/app/strategies" className="underline font-semibold">create one</Link>.
                  </p>
                )}
              </div>
              <div>
                <label className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 block mb-1.5">Broker account</label>
                <select value={deployForm.broker_account_id} onChange={(e) => setDeployForm({...deployForm, broker_account_id: e.target.value})}
                  className="w-full border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-2 text-sm bg-white dark:bg-slate-800">
                  <option value="">Select an account...</option>
                  {(portfolio?.per_account || []).map((a: any) => <option key={a.id} value={a.id}>{a.account_name} · {a.broker}{a.sandbox_mode ? ' (sandbox)' : ''}</option>)}
                </select>
                {(!portfolio?.per_account || portfolio.per_account.length === 0) && (
                  <p className="text-[11px] text-amber-600 dark:text-amber-400 mt-1.5">
                    No broker linked — <button type="button" onClick={() => { setShowDeploy(false); setShowConnect(true) }} className="underline font-semibold">connect one first</button>.
                  </p>
                )}
              </div>
              <div>
                <label className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 block mb-1.5">Instrument</label>
                <select value={deployForm.instrument} onChange={(e) => setDeployForm({...deployForm, instrument: e.target.value})}
                  className="w-full border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-2 text-sm bg-white dark:bg-slate-800">
                  {(assetType === 'futures' ? ['ES','NQ','RTY','YM','MES','MNQ','M2K','MYM'] :
                    assetType === 'options'  ? ['SPY','QQQ','NVDA','AAPL','MSFT','TSLA','AMD','META','AMZN','GOOGL'] :
                    ['SPY','QQQ','AAPL','MSFT','NVDA','TSLA','AMD','META','AMZN','GOOGL','JPM','KO']).map(i => <option key={i}>{i}</option>)}
                </select>
              </div>

              {/* Capital check — warn if account equity is too low for chosen asset type */}
              {(() => {
                const acct = (portfolio?.per_account || []).find((a: any) => a.id === deployForm.broker_account_id)
                const eq = acct?.cached_equity || 0
                const minRequired = assetType === 'futures' ? 5000 : assetType === 'options' ? 2000 : 500
                if (!acct || eq >= minRequired) return null
                return (
                  <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-xl px-3 py-3 text-[11px] text-amber-800 dark:text-amber-200 leading-snug space-y-2">
                    <div className="flex items-start gap-2">
                      <AlertCircle size={14} className="flex-shrink-0 mt-0.5"/>
                      <div>
                        <strong className="block mb-1">Account equity too low for {assetType}.</strong>
                        Selected account has <strong>${eq.toLocaleString()}</strong>. Recommended minimum for {assetType}: <strong>${minRequired.toLocaleString()}</strong>.
                        {assetType === 'futures' && (
                          <>
                            <br/><br/>
                            <strong>Alternatives:</strong>
                            <ul className="list-disc pl-4 mt-1 space-y-0.5">
                              <li>Trade <strong>QQQ shares or options</strong> instead — they track NQ closely with no margin requirement.</li>
                              <li>Use <Link to="/app/email-signals" className="underline font-bold">Email Signals</Link> to subscribe a prop-firm funded account instead of trading from your own.</li>
                            </ul>
                          </>
                        )}
                      </div>
                    </div>
                  </div>
                )
              })()}
              <div className="bg-rose-50 dark:bg-rose-900/20 border border-rose-200 dark:border-rose-900 rounded-xl px-3 py-2.5 text-[11px] text-rose-700 dark:text-rose-300 leading-snug">
                <strong>Heads up:</strong> this places real broker orders. Make sure your sizing settings are correct and the broker account is in sandbox mode if you're testing.
              </div>
            </div>
            <div className="flex gap-2 px-6 py-4 border-t border-slate-100 dark:border-slate-800">
              <button onClick={() => setShowDeploy(false)} className="flex-1 px-4 py-2 rounded-lg text-sm font-bold text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800">Cancel</button>
              <button onClick={() => startSessionMutation.mutate()}
                disabled={!deployForm.strategy_id || !deployForm.broker_account_id || startSessionMutation.isPending}
                className="flex-1 px-4 py-2 rounded-lg text-sm font-bold bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white">
                {startSessionMutation.isPending ? 'Starting…' : 'Deploy live'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
