import { useState } from 'react'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, ReferenceLine } from 'recharts'
import { TradeChartModal } from './TradeChartModal'

export type Metrics = {
  total_trades: number
  wins: number
  losses: number
  win_rate: number
  net_pnl: number
  gross_profit: number
  gross_loss: number
  profit_factor: number | null
  avg_win: number
  avg_loss: number
  max_drawdown: number
  max_drawdown_pct: number
  largest_win: number
  largest_loss: number
}

export type TradeRow = {
  id: string
  instrument: string
  direction: string
  status: string
  entry_price: number | null
  exit_price: number | null
  stop_loss: number | null
  take_profit: number | null
  contracts: number
  pnl: number | null
  net_pnl: number | null
  entry_time: string | null
  exit_time: string | null
  exit_reason: string | null
}

const fmtMoney = (v: number) => `${v >= 0 ? '+' : ''}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

export function fmtEntryTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  const h24 = d.getHours()
  const ampm = h24 >= 12 ? 'PM' : 'AM'
  const h12 = h24 % 12 === 0 ? 12 : h24 % 12
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${d.getMonth() + 1}/${d.getDate()} ${h12}:${mm} ${ampm}`
}

export function fmtHold(entryIso: string | null, exitIso: string | null): string {
  if (!entryIso) return '—'
  const start = new Date(entryIso).getTime()
  const end = exitIso ? new Date(exitIso).getTime() : Date.now()
  const sec = Math.max(0, Math.floor((end - start) / 1000))
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = sec % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

function MetricCard({ label, value, color, sub }: { label: string; value: string; color?: string; sub?: string }) {
  return (
    <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 p-4">
      <div className="text-[11px] font-semibold text-slate-400 dark:text-slate-500 uppercase tracking-wider">{label}</div>
      <div className={`text-2xl font-extrabold mt-1.5 ${color ?? 'text-slate-900 dark:text-slate-100'}`}>{value}</div>
      {sub && <div className="text-[11px] text-slate-400 dark:text-slate-500 mt-0.5">{sub}</div>}
    </div>
  )
}

export function MetricsGrid({ m }: { m: Metrics }) {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
      <MetricCard label="Net P&L" value={fmtMoney(m.net_pnl)} color={m.net_pnl >= 0 ? 'text-green-600' : 'text-red-500'} sub={`${m.total_trades} closed`} />
      <MetricCard label="Win Rate" value={`${(m.win_rate * 100).toFixed(1)}%`} color={m.win_rate >= 0.5 ? 'text-green-600' : 'text-amber-600'} sub={`${m.wins}W / ${m.losses}L`} />
      <MetricCard label="Profit Factor" value={m.profit_factor != null ? m.profit_factor.toFixed(2) : '—'} color={(m.profit_factor ?? 0) >= 1.5 ? 'text-green-600' : 'text-amber-600'} />
      <MetricCard label="Max Drawdown" value={fmtMoney(-Math.abs(m.max_drawdown))} color="text-red-500" sub={`${m.max_drawdown_pct.toFixed(1)}%`} />
      <MetricCard label="Avg Win" value={fmtMoney(m.avg_win)} color="text-green-600" />
      <MetricCard label="Avg Loss" value={fmtMoney(m.avg_loss)} color="text-red-500" />
      <MetricCard label="Largest Win" value={fmtMoney(m.largest_win)} color="text-green-600" />
      <MetricCard label="Largest Loss" value={fmtMoney(m.largest_loss)} color="text-red-500" />
    </div>
  )
}

export function EquityCurve({ trades }: { trades: TradeRow[] }) {
  const closed = trades.filter(t => t.status === 'closed' && t.net_pnl != null)
    .slice().sort((a, b) => {
      const ta = new Date(a.exit_time || a.entry_time || '').getTime()
      const tb = new Date(b.exit_time || b.entry_time || '').getTime()
      return ta - tb
    })
  let equity = 0
  const points = closed.map((t, i) => {
    equity += t.net_pnl || 0
    return { i: i + 1, equity: parseFloat(equity.toFixed(2)) }
  })
  if (points.length === 0) {
    return (
      <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 p-8 text-center text-sm text-slate-400 dark:text-slate-500">
        No closed trades yet — equity curve will appear here once trades start closing.
      </div>
    )
  }
  return (
    <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 p-5">
      <div className="text-sm font-semibold text-slate-700 dark:text-slate-200 mb-3">Equity Curve</div>
      <ResponsiveContainer width="100%" height={200}>
        <LineChart data={points} margin={{ top: 8, right: 12, bottom: 0, left: 12 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" className="dark:opacity-20"/>
          <XAxis dataKey="i" tick={{ fontSize: 10, fill: '#94a3b8' }} tickLine={false} axisLine={false} label={{ value: 'Trade #', position: 'insideBottom', offset: -2, fontSize: 10, fill: '#94a3b8' }}/>
          <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} tickLine={false} axisLine={false} tickFormatter={v => `$${v.toLocaleString()}`} width={64}/>
          <Tooltip contentStyle={{ background: '#ffffff', border: '1px solid #e2e8f0', borderRadius: 8, fontSize: 12 }} formatter={(v: any) => [`$${Number(v).toLocaleString()}`, 'Equity']} labelFormatter={(l) => `Trade #${l}`}/>
          <ReferenceLine y={0} stroke="#94a3b8" strokeDasharray="2 2"/>
          <Line type="monotone" dataKey="equity" stroke="#2563eb" strokeWidth={2} dot={false} activeDot={{ r: 4, fill: '#2563eb' }}/>
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

export function TradeTable({ trades }: { trades: TradeRow[] }) {
  const [openChartId, setOpenChartId] = useState<string | null>(null)

  if (trades.length === 0) {
    return (
      <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 p-8 text-center text-sm text-slate-400 dark:text-slate-500">
        No trades yet.
      </div>
    )
  }
  return (
    <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-slate-100 dark:bg-slate-800 text-[11px] uppercase tracking-wider text-slate-500 dark:text-slate-400">
            <tr>
              {['Entered', 'Hold', 'Symbol', 'Side', 'Status', 'Entry', 'Exit', 'SL', 'TP', 'Qty', 'Net P&L', 'Reason', 'Chart'].map(h => (
                <th key={h} className="px-3 py-2.5 text-left whitespace-nowrap font-semibold">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
            {trades.map(t => {
              const pnlColor = t.net_pnl == null ? 'text-slate-400' : t.net_pnl >= 0 ? 'text-green-600' : 'text-red-500'
              return (
                <tr key={t.id} className="hover:bg-slate-100 dark:hover:bg-slate-800/50 dark:hover:bg-slate-800">
                  <td className="px-3 py-2 text-slate-500 dark:text-slate-400 whitespace-nowrap">{fmtEntryTime(t.entry_time)}</td>
                  <td className="px-3 py-2 text-slate-500 dark:text-slate-400 whitespace-nowrap">{fmtHold(t.entry_time, t.exit_time)}</td>
                  <td className="px-3 py-2 font-semibold text-slate-900 dark:text-slate-100">{t.instrument}</td>
                  <td className="px-3 py-2">
                    <span className={`badge ${t.direction === 'long' ? 'badge-green' : 'badge-red'}`}>{t.direction.toUpperCase()}</span>
                  </td>
                  <td className="px-3 py-2">
                    <span className={`badge ${t.status === 'closed' ? 'badge-grey' : t.status === 'open' ? 'badge-blue' : 'badge-amber'}`}>{t.status}</span>
                  </td>
                  <td className="px-3 py-2 text-slate-700 dark:text-slate-200">{t.entry_price?.toFixed(2) ?? '—'}</td>
                  <td className="px-3 py-2 text-slate-700 dark:text-slate-200">{t.exit_price?.toFixed(2) ?? 'Open'}</td>
                  <td className="px-3 py-2 text-slate-400 dark:text-slate-500">{t.stop_loss?.toFixed(2) ?? '—'}</td>
                  <td className="px-3 py-2 text-slate-400 dark:text-slate-500">{t.take_profit?.toFixed(2) ?? '—'}</td>
                  <td className="px-3 py-2 text-slate-500 dark:text-slate-400">{t.contracts}</td>
                  <td className={`px-3 py-2 font-semibold ${pnlColor}`}>{t.net_pnl != null ? fmtMoney(t.net_pnl) : '—'}</td>
                  <td className="px-3 py-2 text-slate-400 text-xs dark:text-slate-500">{t.exit_reason ?? '—'}</td>
                  <td className="px-3 py-2">
                    <button
                      onClick={() => setOpenChartId(t.id)}
                      className="text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300 text-xs font-semibold underline"
                    >
                      View
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {openChartId && <TradeChartModal tradeId={openChartId} onClose={() => setOpenChartId(null)} />}
    </div>
  )
}
