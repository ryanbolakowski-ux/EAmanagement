/* Candlestick chart with TradingView-style annotations:
   - OHLC candles via Customized
   - FVG zones as colored bands
   - Long/short R:R tool (green target / red stop boxes)
   - Per-step ARROWS + CALLOUT LABELS pointing at key candles
*/
import { ResponsiveContainer, ComposedChart, Line, XAxis, YAxis, Tooltip, ReferenceLine, ReferenceArea, ReferenceDot, Customized } from 'recharts'

export type Candle = { t: string; o: number; h: number; l: number; c: number }
export type FVG = { high: number; low: number; ce: number; direction: string; is_entry: boolean }

export interface Annotation {
  candleIdx: number      // which candle to point at
  priceLevel?: number    // optional — vertical y to aim at; defaults to candle high
  label: string          // text shown next to the arrow
  color?: string         // arrow + label color (default violet)
  side?: 'above' | 'below'   // arrow comes from above (default) or below
  emphasize?: boolean    // bigger arrow + label
}

export interface CandlestickProps {
  candles: Candle[]
  entry?: number | null
  stop?: number | null
  target?: number | null
  direction?: string | null
  entryTime?: string | null
  exitTime?: string | null
  exitPrice?: number | null
  exitReason?: string | null
  fvgs?: FVG[]
  showFvgs?: boolean
  showEntry?: boolean
  showExit?: boolean
  showRRTool?: boolean
  showStopTarget?: boolean
  height?: number
  title?: string
  annotations?: Annotation[]   // NEW — arrows + labels per step
}

function fmtTime(iso: string, tfMin: number): string {
  const d = new Date(iso)
  if (tfMin >= 240) return `${d.getMonth() + 1}/${d.getDate()}`
  if (tfMin >= 60)  return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours()}:00`
  const h24 = d.getHours()
  const ampm = h24 >= 12 ? 'PM' : 'AM'
  const h12 = h24 % 12 === 0 ? 12 : h24 % 12
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${h12}:${mm}${ampm}`
}

function CandleSeries(props: any) {
  const { xAxisMap, yAxisMap, data } = props
  if (!xAxisMap || !yAxisMap || !data || data.length === 0) return null
  const yAxis = Object.values(yAxisMap)[0] as any
  const xAxis = Object.values(xAxisMap)[0] as any
  if (!yAxis || !xAxis || typeof yAxis.scale !== 'function' || typeof xAxis.scale !== 'function') return null
  const yScale = yAxis.scale
  const xScale = xAxis.scale
  const x0 = xScale(0)
  const x1 = data.length > 1 ? xScale(1) : x0 + 8
  const bandWidth = Math.max(2, Math.abs((x1 - x0) * 0.7))
  return (
    <g>
      {data.map((c: Candle, i: number) => {
        const cx = xScale(i)
        if (typeof cx !== 'number' || !isFinite(cx)) return null
        const yH = yScale(c.h); const yL = yScale(c.l)
        const yO = yScale(c.o); const yC = yScale(c.c)
        if (![yH, yL, yO, yC].every(v => typeof v === 'number' && isFinite(v))) return null
        const isBull = c.c >= c.o
        const color = isBull ? '#10b981' : '#ef4444'
        const bodyTop = Math.min(yO, yC)
        const bodyH = Math.max(1, Math.abs(yC - yO))
        return (
          <g key={i}>
            <line x1={cx} x2={cx} y1={yH} y2={yL} stroke={color} strokeWidth={1.2}/>
            <rect x={cx - bandWidth / 2} y={bodyTop} width={bandWidth} height={bodyH}
              fill={color} stroke={color} strokeWidth={1}/>
          </g>
        )
      })}
    </g>
  )
}

/** Renders arrows + callout labels via Customized. Each annotation points
    at a specific candle with an offset stem + arrowhead + text bubble. */
function AnnotationLayer(props: any) {
  const { xAxisMap, yAxisMap, annotations, candles, offset } = props
  if (!xAxisMap || !yAxisMap || !annotations || annotations.length === 0) return null
  const yAxis = Object.values(yAxisMap)[0] as any
  const xAxis = Object.values(xAxisMap)[0] as any
  if (!yAxis || !xAxis || typeof yAxis.scale !== 'function') return null
  const yScale = yAxis.scale; const xScale = xAxis.scale

  // Use the chart drawing area to clamp label positions
  const chartLeft = (offset && offset.left) || 10
  const chartRight = (offset && (offset.left + offset.width)) || 800
  const chartTop = (offset && offset.top) || 10

  return (
    <g>
      {annotations.map((a: Annotation, i: number) => {
        const c = candles[a.candleIdx]
        if (!c) return null
        const tx = xScale(a.candleIdx)
        if (typeof tx !== 'number' || !isFinite(tx)) return null
        const above = a.side !== 'below'
        const targetY = yScale(a.priceLevel ?? (above ? c.h : c.l))
        if (typeof targetY !== 'number' || !isFinite(targetY)) return null

        const arrowSize = a.emphasize ? 8 : 6
        const textH = a.emphasize ? 26 : 22
        const textW = Math.max(90, a.label.length * 6.8 + 18)
        // Stem length depends on whether we have room
        let stemLen = a.emphasize ? 70 : 54
        // Center the box on the candle by default
        let boxX = tx - textW / 2
        // Clamp box inside chart bounds (with 4px padding)
        const minX = chartLeft + 4
        const maxX = chartRight - textW - 4
        if (boxX < minX) boxX = minX
        if (boxX > maxX) boxX = maxX
        // Where the box vertically ends up
        let boxY = above ? (targetY - stemLen - textH / 2) : (targetY + stemLen - textH / 2)
        // Clamp box inside chart top (if above stem pushes off)
        if (boxY < chartTop + 2) {
          boxY = chartTop + 2
          stemLen = Math.max(20, above ? (targetY - boxY - textH / 2) : stemLen)
        }
        const labelCenterX = boxX + textW / 2
        const labelCenterY = boxY + textH / 2
        const color = a.color || '#7c3aed'

        // Stem endpoint near the arrowhead
        const stemEndY = above ? (targetY - arrowSize) : (targetY + arrowSize)

        return (
          <g key={i}>
            {/* Stem: from label center to candle */}
            <line x1={labelCenterX} y1={labelCenterY} x2={tx} y2={stemEndY}
              stroke={color} strokeWidth={1.5}/>
            {/* Arrowhead */}
            <polygon
              points={above
                ? `${tx},${targetY} ${tx - arrowSize},${targetY - arrowSize * 1.5} ${tx + arrowSize},${targetY - arrowSize * 1.5}`
                : `${tx},${targetY} ${tx - arrowSize},${targetY + arrowSize * 1.5} ${tx + arrowSize},${targetY + arrowSize * 1.5}`}
              fill={color}/>
            {/* Label bubble */}
            <rect x={boxX} y={boxY} width={textW} height={textH} rx={6}
              fill={color} fillOpacity={0.96} stroke="#fff" strokeWidth={1.5}/>
            <text x={labelCenterX} y={labelCenterY + 4}
              fill="#fff" textAnchor="middle"
              fontSize={a.emphasize ? 12 : 11} fontWeight="700"
              fontFamily="-apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif">
              {a.label}
            </text>
          </g>
        )
      })}
    </g>
  )
}

export default function Candlestick(p: CandlestickProps) {
  const { candles = [], entry, stop, target, direction, entryTime, exitTime,
          exitPrice, exitReason, fvgs = [], showFvgs = true, showEntry = true,
          showExit = true, showRRTool = true, showStopTarget = true,
          height = 380, title, annotations = [] } = p
  if (!candles || candles.length === 0) {
    return <div className="text-center text-sm text-slate-400 py-12">No bars on this timeframe.</div>
  }
  const tfMin = candles.length >= 2
    ? Math.max(1, Math.round((new Date(candles[1].t).getTime() - new Date(candles[0].t).getTime()) / 60000))
    : 5
  const pts = candles.map((c, i) => ({ i, t: c.t, h: c.h, l: c.l, mid: (c.h + c.l) / 2 }))

  let entryIdx: number | null = null
  if (entryTime) {
    const tt = new Date(entryTime).getTime()
    let best = Infinity
    candles.forEach((c, i) => {
      const d = Math.abs(new Date(c.t).getTime() - tt)
      if (d < best) { best = d; entryIdx = i }
    })
  }
  let exitIdx: number | null = null
  if (exitTime) {
    const tt = new Date(exitTime).getTime()
    let best = Infinity
    candles.forEach((c, i) => {
      const d = Math.abs(new Date(c.t).getTime() - tt)
      if (d < best) { best = d; exitIdx = i }
    })
  }
  const rrStart = entryIdx ?? 0
  const rrEnd   = exitIdx ?? candles.length - 1

  return (
    <div>
      {title && <div className="text-[10px] uppercase tracking-[0.18em] text-slate-400 dark:text-slate-500 font-bold mb-1">{title}</div>}
      <ResponsiveContainer width="100%" height={height}>
        <ComposedChart data={pts} margin={{ top: 30, right: 80, bottom: 20, left: 10 }}>
          <XAxis dataKey="i" type="number" domain={[-0.5, candles.length - 0.5]}
            tick={{ fontSize: 10, fill: '#94a3b8' }}
            tickFormatter={(i) => { const c = candles[i as number]; return c ? fmtTime(c.t, tfMin) : '' }}
            interval={Math.max(1, Math.floor(candles.length / 8))}/>
          <YAxis domain={['dataMin - 3', 'dataMax + 3']} tick={{ fontSize: 10, fill: '#94a3b8' }}
            tickFormatter={(v) => Number(v).toFixed(2)} width={70} orientation="right"/>
          <Tooltip content={(props: any) => {
            const idx = props?.payload?.[0]?.payload?.i
            const c = typeof idx === 'number' ? candles[idx] : null
            if (!c) return null
            const isBull = c.c >= c.o
            return (
              <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-lg p-2 text-[11px] shadow-lg">
                <div className="text-slate-500 mb-1">{fmtTime(c.t, tfMin)}</div>
                <div>O <span className="tabular-nums font-semibold">{c.o.toFixed(2)}</span></div>
                <div>H <span className="tabular-nums font-semibold text-emerald-600">{c.h.toFixed(2)}</span></div>
                <div>L <span className="tabular-nums font-semibold text-rose-600">{c.l.toFixed(2)}</span></div>
                <div>C <span className={`tabular-nums font-semibold ${isBull ? 'text-emerald-600' : 'text-rose-600'}`}>{c.c.toFixed(2)}</span></div>
              </div>
            )
          }}/>

          {showFvgs && fvgs.map((fvg, i) => (
            <ReferenceArea key={`fvg-${i}`} y1={fvg.low} y2={fvg.high}
              fill={fvg.is_entry ? '#a78bfa' : (fvg.direction === 'bullish' ? '#10b981' : '#f43f5e')}
              fillOpacity={fvg.is_entry ? 0.32 : 0.10}
              stroke={fvg.is_entry ? '#7c3aed' : 'transparent'}
              strokeDasharray="3 3"/>
          ))}

          {showRRTool && entry && stop && target && entryIdx !== null && (
            <>
              <ReferenceArea x1={rrStart} x2={rrEnd}
                y1={direction === 'short' ? target : entry}
                y2={direction === 'short' ? entry : target}
                fill="#10b981" fillOpacity={0.16} stroke="#10b981" strokeOpacity={0.5}/>
              <ReferenceArea x1={rrStart} x2={rrEnd}
                y1={direction === 'short' ? entry : stop}
                y2={direction === 'short' ? stop : entry}
                fill="#ef4444" fillOpacity={0.16} stroke="#ef4444" strokeOpacity={0.5}/>
            </>
          )}

          {!showRRTool && showStopTarget && stop && (
            <ReferenceLine y={stop} stroke="#dc2626" strokeWidth={1.5} strokeDasharray="2 4"
              label={{ value: `Stop ${stop.toFixed(2)}`, position: 'right', fill: '#dc2626', fontSize: 10 }}/>
          )}
          {!showRRTool && showStopTarget && target && (
            <ReferenceLine y={target} stroke="#16a34a" strokeWidth={1.5} strokeDasharray="2 4"
              label={{ value: `Target ${target.toFixed(2)}`, position: 'right', fill: '#16a34a', fontSize: 10 }}/>
          )}

          {showEntry && entryIdx !== null && entry && (
            <>
              <ReferenceLine x={entryIdx} stroke="#a78bfa" strokeWidth={2} strokeDasharray="4 2"/>
              <ReferenceDot x={entryIdx} y={entry} r={5} fill="#7c3aed" stroke="#fff" strokeWidth={2}/>
            </>
          )}

          {showExit && exitIdx !== null && exitPrice && (
            <>
              <ReferenceLine x={exitIdx} stroke="#64748b" strokeWidth={2} strokeDasharray="4 2"/>
              <ReferenceDot x={exitIdx} y={exitPrice} r={6}
                fill={exitReason === 'tp_hit' ? '#16a34a' : '#dc2626'}
                stroke="#fff" strokeWidth={2}/>
            </>
          )}

          {/* Invisible Lines anchor recharts axes so Customized can read scales */}
          <Line dataKey="mid" stroke="transparent" dot={false} isAnimationActive={false}/>
          <Line dataKey="h" stroke="transparent" dot={false} isAnimationActive={false}/>
          <Line dataKey="l" stroke="transparent" dot={false} isAnimationActive={false}/>

          {/* Real OHLC candles */}
          <Customized component={(args: any) => <CandleSeries {...args} data={candles}/>} />

          {/* Arrows + callout labels */}
          <Customized component={(args: any) => <AnnotationLayer {...args} annotations={annotations} candles={candles}/>} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}
