import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  ComposedChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  ReferenceArea,
} from 'recharts'
import { ArrowLeft, ArrowUpRight, ArrowDownRight, Minus } from 'lucide-react'
import { dashboardApi, type BiasDetail, type BiasFvg } from '../api/endpoints'
import RefreshButton from '../components/RefreshButton'

const BIAS_STYLE: Record<BiasDetail['bias'], { label: string; tone: string; icon: any }> = {
  strong_bullish: { label: 'Strong Bullish', tone: 'bg-green-600 text-white border-green-600',     icon: ArrowUpRight   },
  bullish:        { label: 'Bullish',        tone: 'bg-green-50 text-green-700 border-green-200 dark:bg-green-900/30 dark:text-green-300 dark:border-green-900/50',  icon: ArrowUpRight   },
  neutral:        { label: 'Neutral',        tone: 'bg-slate-100 text-slate-600 border-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-700',       icon: Minus          },
  bearish:        { label: 'Bearish',        tone: 'bg-red-50 text-red-700 border-red-200 dark:bg-red-900/30 dark:text-red-300 dark:border-red-900/50',              icon: ArrowDownRight },
  strong_bearish: { label: 'Strong Bearish', tone: 'bg-red-600 text-white border-red-600',         icon: ArrowDownRight },
}

const SESSION_LABEL: Record<string, { label: string; tone: string }> = {
  asian:     { label: 'Asian Session',  tone: 'bg-violet-100 text-violet-800 dark:bg-violet-900/40 dark:text-violet-200' },
  london:    { label: 'London Session', tone: 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-200' },
  ny:        { label: 'NY Session',     tone: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200' },
  overnight: { label: 'Overnight',      tone: 'bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-200' },
  unknown:   { label: 'Unknown',        tone: 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400' },
}

function LevelTile({ label, value, swept, sub }: { label: string; value: number | null | undefined; swept?: boolean; sub?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900/40 p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">{label}</span>
        {swept !== undefined && (
          <span className={`text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${swept ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300' : 'bg-slate-200 text-slate-500 dark:bg-slate-800 dark:text-slate-400'}`}>
            {swept ? 'swept' : 'intact'}
          </span>
        )}
      </div>
      <div className="mt-1 text-sm font-extrabold text-slate-900 dark:text-slate-100 tabular-nums">
        {value != null ? value.toLocaleString() : '—'}
      </div>
      {sub && <div className="text-[10px] text-slate-500 dark:text-slate-400 mt-0.5">{sub}</div>}
    </div>
  )
}

function ICTLevelsPanel({ d }: { d: BiasDetail }) {
  const session = d.current_session ?? 'unknown'
  const sessionStyle = SESSION_LABEL[session] ?? SESSION_LABEL.unknown
  const opening = d.opening_type ?? 'unknown'
  const position = d.position_vs_pd ?? 'unknown'

  return (
    <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900/40 p-4 mb-4">
      <div className="flex items-center justify-between gap-2 flex-wrap mb-3">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-bold uppercase tracking-widest text-slate-500 dark:text-slate-400">ICT Levels</span>
          <span className={`text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded ${sessionStyle.tone}`}>
            {sessionStyle.label}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-1.5 text-[10px] font-bold uppercase tracking-wider">
          <span className="px-2 py-0.5 rounded bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300">
            Open: {opening.replace('_', ' ')}
          </span>
          <span className="px-2 py-0.5 rounded bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300">
            Price: {position.replace('_', ' ')}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2 mb-3">
        <LevelTile label="Prior Day High" value={d.pdh} swept={d.pdh_swept} sub="PDH"/>
        <LevelTile label="Prior Day Close" value={d.pdc} sub="PDC"/>
        <LevelTile label="Prior Day Low" value={d.pdl} swept={d.pdl_swept} sub="PDL"/>
        <LevelTile label="Asian High" value={d.asian_high} swept={d.asian_swept_high}/>
        <LevelTile label="Asian Low" value={d.asian_low} swept={d.asian_swept_low}/>
      </div>

      {d.draw_target && (
        <div className="rounded-lg bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-900/40 px-3 py-2 text-xs text-slate-700 dark:text-slate-200">
          <span className="font-bold text-blue-700 dark:text-blue-300">Draw on liquidity →</span>{' '}
          {d.draw_target.label} @ <span className="font-bold tabular-nums">{d.draw_target.level.toLocaleString()}</span>{' '}
          <span className="text-slate-500 dark:text-slate-400">({d.draw_target.side})</span>
        </div>
      )}
    </div>
  )
}

function formatTime(t: number) {
  const d = new Date(t * 1000)
  return `${d.getMonth() + 1}/${d.getDate()}`
}

function InstrumentSection({ d }: { d: BiasDetail }) {
  const style = BIAS_STYLE[d.bias]
  const Icon = style.icon

  // Build the chart series: candle close + ema fast + ema slow per day
  const chartData = d.candles.map((c, i) => ({
    time: c.time,
    label: formatTime(c.time),
    close: c.close,
    ema9:  d.ema_fast_series[i] ?? null,
    ema21: d.ema_slow_series[i] ?? null,
  }))

  // Pick the price domain so the chart isn't scaled by ema lines extremes
  const closes = chartData.map(p => p.close)
  const minP = closes.length ? Math.min(...closes) : 0
  const maxP = closes.length ? Math.max(...closes) : 0
  const pad = (maxP - minP) * 0.05 || 1
  const yDomain: [number, number] = [Math.floor(minP - pad), Math.ceil(maxP + pad)]

  // Derive ranges (start/end timestamps) for each FVG so we can paint a band
  const chartStart = chartData[0]?.time ?? 0
  const chartEnd   = chartData[chartData.length - 1]?.time ?? 0
  const fvgsForChart = d.htf_fvgs
    .filter(f => !f.filled)
    .map(f => ({
      ...f,
      start: Math.max(chartStart, Math.floor(new Date(f.timestamp).getTime() / 1000)),
      end:   chartEnd,
    }))

  const respectedBull   = d.htf_fvgs.filter(f => f.direction === 'bullish' && !f.filled && f.respected).length
  const disrespectedBull = d.htf_fvgs.filter(f => f.direction === 'bullish' && (!f.respected || f.filled)).length
  const respectedBear   = d.htf_fvgs.filter(f => f.direction === 'bearish' && !f.filled && f.respected).length
  const disrespectedBear = d.htf_fvgs.filter(f => f.direction === 'bearish' && (!f.respected || f.filled)).length

  return (
    <div className="bg-white dark:bg-slate-800 rounded-2xl border border-slate-200 dark:border-slate-700 p-6 shadow-sm">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <div>
          <h2 className="text-xl font-extrabold text-slate-900 dark:text-slate-100">{d.instrument}</h2>
          <div className="text-xs text-slate-500 dark:text-slate-400">
            Last close {d.last_close?.toLocaleString()} · EMA9 {d.ema_fast?.toLocaleString()} · EMA21 {d.ema_slow?.toLocaleString()}
          </div>
        </div>
        <span className={`inline-flex items-center gap-1.5 text-xs font-semibold px-3 py-1.5 rounded-lg border ${style.tone}`}>
          <Icon size={13} strokeWidth={2.5} />
          {style.label} ({d.strength_pct >= 0 ? '+' : ''}{d.strength_pct.toFixed(2)}%)
        </span>
      </div>

      <ICTLevelsPanel d={d}/>

      <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed mb-4 dark:text-slate-200">{d.summary}</p>

      <div className="h-64 -mx-2 mb-4">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={chartData} margin={{ top: 8, right: 12, bottom: 0, left: 12 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" className="dark:opacity-20"/>
            <XAxis dataKey="label" tick={{ fontSize: 10, fill: '#64748b' }} tickLine={false} axisLine={false} interval="preserveStartEnd"/>
            <YAxis domain={yDomain} tick={{ fontSize: 10, fill: '#64748b' }} tickLine={false} axisLine={false} width={56} tickFormatter={v => v.toLocaleString()}/>
            <Tooltip
              contentStyle={{ background: '#ffffff', border: '1px solid #e2e8f0', borderRadius: 8, fontSize: 12 }}
              labelFormatter={(l) => `Day ${l}`}
              formatter={(v: any, name) => [Number(v).toLocaleString(), name]}
            />
            {fvgsForChart.map((f, i) => (
              <ReferenceArea
                key={i}
                y1={f.low} y2={f.high}
                x1={f.start} x2={f.end}
                fill={f.direction === 'bullish' ? '#10b981' : '#ef4444'}
                fillOpacity={f.respected ? 0.10 : 0.04}
                stroke={f.direction === 'bullish' ? '#10b981' : '#ef4444'}
                strokeOpacity={0.3}
                strokeDasharray="3 3"
              />
            ))}
            <Line type="monotone" dataKey="close" stroke="#2563eb" strokeWidth={2} dot={false} name="Close"/>
            <Line type="monotone" dataKey="ema9"  stroke="#10b981" strokeWidth={1.5} dot={false} name="EMA9"/>
            <Line type="monotone" dataKey="ema21" stroke="#f97316" strokeWidth={1.5} dot={false} name="EMA21"/>
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4">
        <FvgStat label="Bullish FVGs respecting"   count={respectedBull}    tone="green"/>
        <FvgStat label="Bullish FVGs broken"       count={disrespectedBull} tone="red"/>
        <FvgStat label="Bearish FVGs holding"      count={respectedBear}    tone="red"/>
        <FvgStat label="Bearish FVGs broken"       count={disrespectedBear} tone="green"/>
      </div>

      <details className="group">
        <summary className="cursor-pointer text-xs font-semibold text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200">
          1H Fair Value Gaps ({d.htf_fvgs.length})
        </summary>
        <div className="mt-2 max-h-60 overflow-y-auto">
          <table className="w-full text-xs">
            <thead className="text-slate-500 dark:text-slate-400">
              <tr className="border-b border-slate-200 dark:border-slate-700">
                <th className="text-left py-1.5 px-2 font-semibold">Time</th>
                <th className="text-left py-1.5 px-2 font-semibold">Direction</th>
                <th className="text-right py-1.5 px-2 font-semibold">Low</th>
                <th className="text-right py-1.5 px-2 font-semibold">CE</th>
                <th className="text-right py-1.5 px-2 font-semibold">High</th>
                <th className="text-right py-1.5 px-2 font-semibold">Size</th>
                <th className="text-left py-1.5 px-2 font-semibold">Status</th>
              </tr>
            </thead>
            <tbody>
              {d.htf_fvgs.slice().reverse().map((f, i) => <FvgRow key={i} f={f}/>)}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  )
}

function FvgStat({ label, count, tone }: { label: string; count: number; tone: 'green' | 'red' }) {
  const cls = tone === 'green'
    ? 'bg-green-50 text-green-700 border-green-200 dark:bg-green-900/20 dark:text-green-300 dark:border-green-900/40'
    : 'bg-red-50 text-red-700 border-red-200 dark:bg-red-900/20 dark:text-red-300 dark:border-red-900/40'
  return (
    <div className={`rounded-lg border p-3 ${cls}`}>
      <div className="text-2xl font-extrabold">{count}</div>
      <div className="text-[11px] mt-0.5 leading-snug">{label}</div>
    </div>
  )
}

function FvgRow({ f }: { f: BiasFvg }) {
  const dt = new Date(f.timestamp)
  const dateLabel = `${dt.getMonth() + 1}/${dt.getDate()} ${String(dt.getHours()).padStart(2, '0')}:${String(dt.getMinutes()).padStart(2, '0')}`
  const dirCls = f.direction === 'bullish' ? 'text-green-600' : 'text-red-500'
  const status = f.filled ? 'Filled' : f.respected ? 'Respected' : 'Disrespected'
  const statusCls = f.filled
    ? 'text-slate-400 dark:text-slate-500'
    : f.respected
      ? 'text-green-600'
      : 'text-amber-600'
  return (
    <tr className="border-b border-slate-100 dark:border-slate-800">
      <td className="py-1.5 px-2 text-slate-500 dark:text-slate-400">{dateLabel}</td>
      <td className={`py-1.5 px-2 font-semibold capitalize ${dirCls}`}>{f.direction}</td>
      <td className="py-1.5 px-2 text-right text-slate-600 dark:text-slate-300">{f.low.toLocaleString()}</td>
      <td className="py-1.5 px-2 text-right text-slate-600 dark:text-slate-300">{f.ce.toLocaleString()}</td>
      <td className="py-1.5 px-2 text-right text-slate-600 dark:text-slate-300">{f.high.toLocaleString()}</td>
      <td className="py-1.5 px-2 text-right text-slate-500 dark:text-slate-400">{f.size_ticks.toFixed(1)} ticks</td>
      <td className={`py-1.5 px-2 font-semibold ${statusCls}`}>{status}</td>
    </tr>
  )
}

export default function BiasDetailPage() {
  const { data, isLoading, refetch } = useQuery({
    queryKey: ['bias-detail'],
    queryFn: () => dashboardApi.biasDetail().then(r => r.data),
    refetchInterval: 5 * 60_000,
    staleTime: 60_000,
  })

  return (
    <div className="p-8 max-w-6xl">
      <div className="flex items-center justify-between mb-6 flex-wrap gap-3">
        <div>
          <Link to="/app" className="inline-flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-800 mb-2 transition-colors dark:text-slate-400">
            <ArrowLeft size={14}/> Back to dashboard
          </Link>
          <h1 className="text-2xl font-extrabold text-slate-900 dark:text-slate-100">Daily Bias</h1>
          <p className="text-slate-500 text-sm mt-1 dark:text-slate-400">
            ICT daily bias — 30D trend + intraday structure (PDH/PDL/PDC, Asian range, sweeps, draw on liquidity) and 1H Fair Value Gaps.
          </p>
        </div>
        <RefreshButton onClick={() => refetch()}/>
      </div>

      {isLoading ? (
        <div className="space-y-6">
          {[...Array(4)].map((_, i) => (
            <div key={i} className="h-96 bg-slate-100 dark:bg-slate-900 rounded-2xl border border-slate-200 dark:border-slate-700 animate-pulse dark:bg-slate-800"/>
          ))}
        </div>
      ) : (
        <div className="space-y-6">
          {data?.instruments.map(d => <InstrumentSection key={d.instrument} d={d}/>)}
        </div>
      )}
    </div>
  )
}
