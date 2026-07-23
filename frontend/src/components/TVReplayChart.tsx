// ─────────────────────────────────────────────────────────────────────────────
// TVReplayChart — TradingView-style replay chart (lightweight-charts v4).
// Used ONLY by pages/Replay.tsx (LiveTrading/PaperTrading keep the shared
// CandlestickChart). FX-Replay behavior: bars stream in one at a time and the
// in-progress display bucket updates live via series.update(); setData() runs
// only on a hard reset (new day / instrument / timeframe switch), so pan and
// zoom survive playback.
//
// Times: every bar timestamp is shifted so the chart's UTC formatters render
// Eastern wall-clock time. The offset is computed PER BAR from its own instant
// (Intl in America/New_York), so DST is handled without any hardcoded offset.
// ─────────────────────────────────────────────────────────────────────────────
import { useEffect, useRef } from 'react'
import {
  createChart, ColorType, CrosshairMode, LineStyle,
  type CandlestickData, type HistogramData, type IChartApi, type IPriceLine,
  type ISeriesApi, type MouseEventParams, type SeriesMarker, type Time,
  type UTCTimestamp,
} from 'lightweight-charts'

export type TVBar = { time: number; open: number; high: number; low: number; close: number; volume?: number }
export type TVTrade = { direction: 'long' | 'short'; entryTime: number; exitTime: number; r: number }
export type TVPosition = { direction: 'long' | 'short'; qty: number; entryPrice: number; entryTime: number; stopPrice: number; targetPrice: number }

type Props = {
  instrument: string
  /** Already-revealed 1m bars, epoch SECONDS UTC, ascending. */
  bars: TVBar[]
  /** Chart-only display timeframe; the sim always steps 1m bars. */
  displayTf: 1 | 5 | 15 | 60
  /** Changing this forces a full setData reset (new day / new session). */
  resetKey: string
  /** false in blind mode: axis + crosshair labels show HH:mm only (no date leak). */
  showDate: boolean
  pdh?: number | null
  pdl?: number | null
  position?: TVPosition | null
  trades?: TVTrade[]
}

// ── colors (TradingView defaults) ────────────────────────────────────────────
const UP = '#26a69a'
const DOWN = '#ef5350'
const VOL_UP = 'rgba(38, 166, 154, 0.45)'
const VOL_DOWN = 'rgba(239, 83, 80, 0.45)'
const ENTRY_LINE = '#787b86'

const THEMES = {
  dark: {
    bg: '#131722', text: '#d1d4dc', grid: 'rgba(42, 46, 57, 0.6)',
    border: '#2a2e39', watermark: 'rgba(209, 212, 220, 0.06)',
    crosshair: '#758696', legendMuted: '#787b86',
  },
  light: {
    bg: '#ffffff', text: '#191919', grid: '#f0f3fa',
    border: '#e0e3eb', watermark: 'rgba(25, 25, 25, 0.05)',
    crosshair: '#9598a1', legendMuted: '#787b86',
  },
} as const

const tfLabel = (tf: 1 | 5 | 15 | 60) => (tf === 60 ? '1h' : `${tf}m`)

// ── ET wall-clock shift ──────────────────────────────────────────────────────
// hourCycle h23 so midnight never renders as "24".
const ET_PARTS = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York', hourCycle: 'h23',
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
})
// The offset is constant within any UTC hour (DST flips on exact hour
// boundaries), so cache per hour-bucket to keep resampling cheap at 10x.
const offsetCache = new Map<number, number>()
export function etOffsetSeconds(utcSeconds: number): number {
  const bucket = Math.floor(utcSeconds / 3600)
  const hit = offsetCache.get(bucket)
  if (hit !== undefined) return hit
  const parts = ET_PARTS.formatToParts(new Date(utcSeconds * 1000))
  const get = (t: string) => Number(parts.find((p) => p.type === t)?.value ?? 0)
  const zonedAsUtcMs = Date.UTC(get('year'), get('month') - 1, get('day'), get('hour'), get('minute'), get('second'))
  const off = Math.round(zonedAsUtcMs / 1000) - utcSeconds
  offsetCache.set(bucket, off)
  return off
}
export const etShift = (utcSeconds: number) => (utcSeconds + etOffsetSeconds(utcSeconds)) as UTCTimestamp

const pad2 = (n: number) => String(n).padStart(2, '0')
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
// Shifted timestamps are "ET rendered as UTC", so format with getUTC*.
function fmtShifted(time: Time, withDate: boolean): string {
  if (typeof time !== 'number') return String(time)
  const d = new Date(time * 1000)
  const hm = `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`
  return withDate ? `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()} ${hm}` : hm
}

// ── 1m → displayTf resampling ────────────────────────────────────────────────
// Buckets are anchored to the session's first bar (09:30 ET for RTH data), so
// the 1h view shows 09:30/10:30/... candles exactly like TradingView RTH.
export function bucketStartFor(shifted: number, anchorShifted: number, tfMin: number): number {
  const secs = tfMin * 60
  return shifted - ((shifted - anchorShifted) % secs)
}

export function resample(bars: TVBar[], tfMin: number): { candles: CandlestickData<UTCTimestamp>[]; vols: HistogramData<UTCTimestamp>[] } {
  const candles: CandlestickData<UTCTimestamp>[] = []
  const vols: HistogramData<UTCTimestamp>[] = []
  if (bars.length === 0) return { candles, vols }
  const anchor = etShift(bars[0].time)
  let cur: CandlestickData<UTCTimestamp> | null = null
  let curVol = 0
  const flush = () => {
    if (!cur) return
    candles.push(cur)
    vols.push({ time: cur.time, value: curVol, color: cur.close >= cur.open ? VOL_UP : VOL_DOWN })
  }
  for (const b of bars) {
    const start = bucketStartFor(etShift(b.time), anchor, tfMin) as UTCTimestamp
    if (!cur || cur.time !== start) {
      flush()
      cur = { time: start, open: b.open, high: b.high, low: b.low, close: b.close }
      curVol = b.volume ?? 0
    } else {
      if (b.high > cur.high) cur.high = b.high
      if (b.low < cur.low) cur.low = b.low
      cur.close = b.close
      curVol += b.volume ?? 0
    }
  }
  flush()
  return { candles, vols }
}

// ─────────────────────────────────────────────────────────────────────────────

export default function TVReplayChart({
  instrument, bars, displayTf, resetKey, showDate, pdh, pdl, position, trades = [],
}: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const legendRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volumeRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const priceLinesRef = useRef<IPriceLine[]>([])
  // What is currently rendered: series identity + bucket count, so reveals go
  // through series.update() and only hard resets call setData().
  const renderedRef = useRef<{ key: string; count: number }>({ key: '', count: 0 })
  const lastCandleRef = useRef<CandlestickData<UTCTimestamp> | null>(null)
  const isDarkRef = useRef(document.documentElement.classList.contains('dark'))
  // Mount-scope closures (crosshair handler, theme observer) read live props
  // through this ref so legend/watermark never go stale.
  const liveRef = useRef({ instrument, displayTf, showDate })
  liveRef.current = { instrument, displayTf, showDate }

  const setLegend = (c: CandlestickData<UTCTimestamp> | null) => {
    const el = legendRef.current
    if (!el) return
    if (!c) {
      el.innerHTML = ''
      return
    }
    const t = isDarkRef.current ? THEMES.dark : THEMES.light
    const up = c.close >= c.open
    const dirColor = up ? UP : DOWN
    const chg = c.open !== 0 ? ((c.close - c.open) / c.open) * 100 : 0
    const cell = (label: string, v: number) =>
      `<span style="color:${t.legendMuted}">${label}</span> <span style="color:${dirColor}">${v.toFixed(2)}</span>`
    el.innerHTML =
      `<span style="color:${t.text};font-weight:700">${liveRef.current.instrument}</span> ` +
      `<span style="color:${t.legendMuted}">· ${tfLabel(liveRef.current.displayTf)}</span>&nbsp; ` +
      `${cell('O', c.open)} ${cell('H', c.high)} ${cell('L', c.low)} ${cell('C', c.close)} ` +
      `<span style="color:${dirColor}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span>`
  }
  const setLegendRef = useRef(setLegend)
  setLegendRef.current = setLegend

  const watermarkOptions = (dark: boolean) => ({
    visible: true,
    text: `${liveRef.current.instrument} · ${tfLabel(liveRef.current.displayTf)} replay`,
    color: (dark ? THEMES.dark : THEMES.light).watermark,
    fontSize: 40,
  })

  const themeOptions = (dark: boolean) => {
    const t = dark ? THEMES.dark : THEMES.light
    return {
      layout: { background: { type: ColorType.Solid as const, color: t.bg }, textColor: t.text },
      grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
      rightPriceScale: { borderColor: t.border },
      timeScale: { borderColor: t.border },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: t.crosshair, labelBackgroundColor: t.crosshair },
        horzLine: { color: t.crosshair, labelBackgroundColor: t.crosshair },
      },
    }
  }
  const themeOptionsRef = useRef(themeOptions)
  themeOptionsRef.current = themeOptions
  const watermarkOptionsRef = useRef(watermarkOptions)
  watermarkOptionsRef.current = watermarkOptions

  // ── mount: create chart + series + observers ──────────────────────────────
  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    const chart = createChart(el, {
      width: el.clientWidth,
      height: el.clientHeight,
      ...themeOptionsRef.current(isDarkRef.current),
      timeScale: {
        borderColor: (isDarkRef.current ? THEMES.dark : THEMES.light).border,
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 6,
        barSpacing: 8,
        // Never let a tick label print the date in blind mode.
        tickMarkFormatter: (time: Time) => fmtShifted(time, false),
      },
      localization: {
        timeFormatter: (time: Time) => fmtShifted(time, liveRef.current.showDate),
      },
    })

    const candle = chart.addCandlestickSeries({
      upColor: UP, downColor: DOWN, wickUpColor: UP, wickDownColor: DOWN,
      borderVisible: false,
      priceFormat: { type: 'price', precision: 2, minMove: 0.25 },
    })
    // Keep candles clear of the volume pane at the bottom.
    chart.priceScale('right').applyOptions({ scaleMargins: { top: 0.08, bottom: 0.22 } })

    const volume = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: '',
      lastValueVisible: false,
      priceLineVisible: false,
    })
    chart.priceScale('').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } })

    chartRef.current = chart
    candleRef.current = candle
    volumeRef.current = volume
    renderedRef.current = { key: '', count: 0 }

    // OHLC legend: hovered candle, falling back to the latest bar.
    const onCrosshair = (param: MouseEventParams) => {
      const d = param.time != null
        ? (param.seriesData.get(candle) as CandlestickData<UTCTimestamp> | undefined)
        : undefined
      setLegendRef.current(d ?? lastCandleRef.current)
    }
    chart.subscribeCrosshairMove(onCrosshair)

    // Theme: follow the app's dark class on <html> live.
    const applyTheme = () => {
      const dark = document.documentElement.classList.contains('dark')
      isDarkRef.current = dark
      chart.applyOptions({
        ...themeOptionsRef.current(dark),
        watermark: watermarkOptionsRef.current(dark),
      })
      setLegendRef.current(lastCandleRef.current)
    }
    applyTheme()
    const mo = new MutationObserver(applyTheme)
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })

    const ro = new ResizeObserver(() => {
      const c = containerRef.current
      if (c) chart.resize(c.clientWidth, c.clientHeight)
    })
    ro.observe(el)

    return () => {
      mo.disconnect()
      ro.disconnect()
      chart.unsubscribeCrosshairMove(onCrosshair)
      chart.remove()
      chartRef.current = null
      candleRef.current = null
      volumeRef.current = null
      priceLinesRef.current = []
      lastCandleRef.current = null
      renderedRef.current = { key: '', count: 0 }
    }
  }, [])

  // ── watermark + crosshair date masking on prop change ─────────────────────
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    chart.applyOptions({
      watermark: watermarkOptionsRef.current(isDarkRef.current),
      localization: { timeFormatter: (time: Time) => fmtShifted(time, liveRef.current.showDate) },
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instrument, displayTf, showDate])

  // ── data: setData only on hard reset, series.update() during playback ─────
  useEffect(() => {
    const chart = chartRef.current
    const candle = candleRef.current
    const volume = volumeRef.current
    if (!chart || !candle || !volume) return
    const key = `${resetKey}|${displayTf}`
    if (bars.length === 0) {
      if (renderedRef.current.key !== key || renderedRef.current.count > 0) {
        candle.setData([])
        volume.setData([])
        renderedRef.current = { key, count: 0 }
        lastCandleRef.current = null
        setLegend(null)
      }
      return
    }
    const { candles, vols } = resample(bars, displayTf)
    const hasVolume = bars.some((b) => (b.volume ?? 0) > 0)
    const prev = renderedRef.current
    if (prev.key !== key || candles.length < prev.count) {
      // New day / TF switch / rewind: full redraw, then snap to the right edge.
      candle.setData(candles)
      volume.setData(hasVolume ? vols : [])
      chart.timeScale().scrollToRealTime()
    } else {
      // Reveal path: update the in-progress bucket + append completed ones.
      // Same-time update replaces the forming candle — TradingView tick behavior.
      for (let i = Math.max(0, prev.count - 1); i < candles.length; i++) {
        candle.update(candles[i])
        if (hasVolume) volume.update(vols[i])
      }
    }
    renderedRef.current = { key, count: candles.length }
    lastCandleRef.current = candles[candles.length - 1] ?? null
    setLegend(lastCandleRef.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bars, displayTf, resetKey])

  // ── price lines: entry/SL/TP + PDH/PDL ────────────────────────────────────
  useEffect(() => {
    const candle = candleRef.current
    if (!candle) return
    const add = (price: number, color: string, style: LineStyle, title: string, width: 1 | 2 = 1) =>
      priceLinesRef.current.push(candle.createPriceLine({
        price, color, lineWidth: width, lineStyle: style, axisLabelVisible: true, title,
      }))
    if (pdh != null) add(pdh, 'rgba(245, 158, 11, 0.7)', LineStyle.Dotted, 'PDH')
    if (pdl != null) add(pdl, 'rgba(56, 189, 248, 0.7)', LineStyle.Dotted, 'PDL')
    if (position) {
      add(position.entryPrice, ENTRY_LINE, LineStyle.Solid, `${position.direction.toUpperCase()} ×${position.qty}`, 2)
      add(position.stopPrice, DOWN, LineStyle.Dashed, 'SL')
      add(position.targetPrice, UP, LineStyle.Dashed, 'TP')
    }
    return () => {
      // candleRef may already be nulled by the mount cleanup (StrictMode);
      // chart.remove() disposes price lines with the chart in that case.
      const c = candleRef.current
      if (c) for (const pl of priceLinesRef.current) c.removePriceLine(pl)
      priceLinesRef.current = []
    }
  }, [pdh, pdl, position, resetKey])

  // ── trade markers, snapped to the display-TF bucket start ─────────────────
  useEffect(() => {
    const candle = candleRef.current
    if (!candle) return
    if (bars.length === 0) {
      candle.setMarkers([])
      return
    }
    const anchor = etShift(bars[0].time)
    const snap = (utc: number) => bucketStartFor(etShift(utc), anchor, displayTf) as UTCTimestamp
    const entryMarker = (time: number, direction: 'long' | 'short'): SeriesMarker<UTCTimestamp> => ({
      time: snap(time),
      position: direction === 'long' ? 'belowBar' : 'aboveBar',
      shape: direction === 'long' ? 'arrowUp' : 'arrowDown',
      color: direction === 'long' ? UP : DOWN,
      text: direction === 'long' ? 'LONG' : 'SHORT',
    })
    const markers: SeriesMarker<UTCTimestamp>[] = []
    for (const tr of trades) {
      markers.push(entryMarker(tr.entryTime, tr.direction))
      markers.push({
        time: snap(tr.exitTime),
        position: 'aboveBar',
        shape: 'circle',
        color: tr.r >= 0 ? UP : DOWN,
        text: `${tr.r >= 0 ? '+' : ''}${tr.r.toFixed(1)}R`,
      })
    }
    if (position) markers.push(entryMarker(position.entryTime, position.direction))
    // setMarkers requires ascending times.
    markers.sort((a, b) => (a.time as number) - (b.time as number))
    candle.setMarkers(markers)
  }, [trades, position, displayTf, bars, resetKey])

  return (
    <div className="relative w-full h-full">
      <div ref={containerRef} className="absolute inset-0"/>
      <div
        ref={legendRef}
        className="absolute top-2 left-3 z-10 pointer-events-none select-none text-[11px] font-medium tracking-tight whitespace-nowrap"
      />
    </div>
  )
}
