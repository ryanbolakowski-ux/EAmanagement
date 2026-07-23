// ─────────────────────────────────────────────────────────────────────────────
// TVReplayChart — TradingView-style replay chart (lightweight-charts v4).
// Used ONLY by pages/Replay.tsx (LiveTrading/PaperTrading keep the shared
// CandlestickChart). FX-Replay behavior: bars stream in one at a time and the
// in-progress display bucket updates live via series.update(); setData() runs
// only on a hard reset (new day / instrument / timeframe switch), so pan and
// zoom survive playback.
//
// Times: every bar timestamp is shifted so the chart's UTC formatters render
// Eastern wall-clock time by default. The offset is computed PER BAR from its
// own instant (Intl in America/New_York), so DST is handled without any
// hardcoded offset. The DISPLAY timezone/hour-format only change the label
// TEXT — the shift used to place candles/buckets/markers/bands never changes,
// so nothing on the chart ever moves when the timezone or format changes.
// ─────────────────────────────────────────────────────────────────────────────
import { useEffect, useRef } from 'react'
import {
  createChart, ColorType, CrosshairMode, LineStyle,
  type CandlestickData, type HistogramData, type IChartApi, type IPriceLine,
  type ISeriesApi, type ISeriesPrimitive, type ISeriesPrimitivePaneRenderer,
  type ISeriesPrimitivePaneView, type Logical, type MouseEventParams,
  type SeriesAttachedParameter, type SeriesMarker, type SeriesPrimitivePaneViewZOrder,
  type Time, type UTCTimestamp,
} from 'lightweight-charts'

export type TVBar = { time: number; open: number; high: number; low: number; close: number; volume?: number }
// r is null for trades opened without an initial stop (GOAL G): risk is undefined.
export type TVTrade = { direction: 'long' | 'short'; entryTime: number; exitTime: number; r: number | null }
// stopPrice / targetPrice are null when the position has no SL / TP (GOAL G).
export type TVPosition = {
  direction: 'long' | 'short'; qty: number; entryPrice: number; entryTime: number
  stopPrice: number | null; targetPrice: number | null
}

// Master toggles for the session-shading bands (GOAL B). Any omitted key
// defaults to visible; the whole overlay is gated by `sessionsEnabled`.
export type SessionVisibility = {
  asia?: boolean; london?: boolean; nyAm?: boolean; nyLunch?: boolean; nyPm?: boolean
}

type Props = {
  instrument: string
  /** Already-revealed 1m bars, epoch SECONDS UTC, ascending. */
  bars: TVBar[]
  /** Chart-only display timeframe in MINUTES (any 1–240); the sim always steps 1m bars. */
  displayTf: number
  /** Changing this forces a full setData reset (new day / new session). */
  resetKey: string
  /** false in blind mode: axis + crosshair labels show time only (no date leak). */
  showDate: boolean
  pdh?: number | null
  pdl?: number | null
  position?: TVPosition | null
  trades?: TVTrade[]
  // ── GOAL A: live color customization ─────────────────────────────────────
  /** Up candle+wick color. Default TradingView green #26a69a. */
  upColor?: string
  /** Down candle+wick color. Default TradingView red #ef5350. */
  downColor?: string
  /** Chart background override (applies in both themes). null/undefined = theme default. */
  background?: string | null
  // ── GOAL B: session-time indicator bands ─────────────────────────────────
  /** Master on/off for the translucent session bands. Default false. */
  sessionsEnabled?: boolean
  /** Per-session visibility; omitted keys default to true. */
  sessionVisibility?: SessionVisibility
  // ── GOAL I: timezone + time format (labels only — never moves anything) ──
  /** IANA timezone for axis/crosshair/legend LABELS. Default 'America/New_York'. */
  timezone?: string
  /** 12-hour clock for labels when true, else 24-hour. Default false. */
  hour12?: boolean
  // ── GOAL G: place-on-chart SL/TP arming ──────────────────────────────────
  /** Fired on a chart click with the price under the cursor (null-safe). */
  onChartClick?: (price: number) => void
  // ── GOAL H: right-click context menu (page renders the menu UI) ──────────
  /** Fired on right-click over the chart surface with VIEWPORT coords (clientX, clientY). */
  onContextMenu?: (x: number, y: number) => void
}

// ── colors (TradingView defaults) ────────────────────────────────────────────
// Per-instrument price steps — RTY ticks 0.10, YM 1.00; a wrong minMove makes
// lightweight-charts quantize every axis/crosshair/price-line label onto the
// wrong grid (e.g. RTY 2245.10 rendered as 2245.00).
const PRICE_FORMAT: Record<string, { precision: number; minMove: number }> = {
  ES: { precision: 2, minMove: 0.25 },
  NQ: { precision: 2, minMove: 0.25 },
  YM: { precision: 0, minMove: 1 },
  RTY: { precision: 1, minMove: 0.1 },
}

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

// tf label: '3m', '45m', '1h', '2h 30m'.
const tfLabel = (tf: number) => {
  if (tf < 60) return `${tf}m`
  const h = Math.floor(tf / 60)
  const m = tf % 60
  return m === 0 ? `${h}h` : `${h}h ${m}m`
}

// Convert a hex color (#rgb / #rrggbb) to an rgba() string; passes any other
// color format through unchanged (best-effort — presets/pickers are hex).
function withAlpha(color: string, alpha: number): string {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color.trim())
  if (!m) return color
  let h = m[1]
  if (h.length === 3) h = h.split('').map((c) => c + c).join('')
  const n = parseInt(h, 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`
}

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

// ── time-label formatting (timezone + 12/24h aware) ──────────────────────────
const pad2 = (n: number) => String(n).padStart(2, '0')
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function fmt12or24(h24: number, m: number, hour12: boolean): string {
  if (!hour12) return `${pad2(h24)}:${pad2(m)}`
  const ap = h24 < 12 ? 'AM' : 'PM'
  let h = h24 % 12
  if (h === 0) h = 12
  return `${h}:${pad2(m)} ${ap}`
}

// Cached Intl formatters for non-ET timezones (keyed by zone + hour format).
const zonedDtfCache = new Map<string, Intl.DateTimeFormat>()
function zonedParts(realUtcSeconds: number, timezone: string, hour12: boolean) {
  const key = `${timezone}|${hour12}`
  let dtf = zonedDtfCache.get(key)
  if (!dtf) {
    dtf = new Intl.DateTimeFormat('en-US', {
      timeZone: timezone,
      ...(hour12 ? { hour12: true, hour: 'numeric' } : { hourCycle: 'h23', hour: '2-digit' }),
      minute: '2-digit', month: 'short', day: 'numeric', year: 'numeric',
    })
    zonedDtfCache.set(key, dtf)
  }
  const parts = dtf.formatToParts(new Date(realUtcSeconds * 1000))
  const g = (t: string) => parts.find((p) => p.type === t)?.value ?? ''
  return { month: g('month'), day: g('day'), hour: g('hour'), minute: g('minute'), dayPeriod: g('dayPeriod') }
}

// Chart time values are ET-shifted ("ET rendered as UTC"). Render that instant
// in the chosen `timezone`/`hour12`. ET is the fast path (getUTC*, no Intl);
// other zones recover the true UTC instant (exact for intraday replay data:
// the ET offset is stable over a session, DST flips only near 02:00 Sun) then
// format with Intl. When `withDate` is false the label is time-only — the sole
// path used for the axis and for the crosshair in blind mode, so no date leaks.
function fmtZoned(time: Time, timezone: string, hour12: boolean, withDate: boolean): string {
  if (typeof time !== 'number') return String(time)
  if (timezone === 'America/New_York') {
    const d = new Date(time * 1000)
    const hm = fmt12or24(d.getUTCHours(), d.getUTCMinutes(), hour12)
    return withDate ? `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()} ${hm}` : hm
  }
  const realUtc = time - etOffsetSeconds(time)
  const p = zonedParts(realUtc, timezone, hour12)
  const hm = hour12 ? `${p.hour}:${p.minute} ${p.dayPeriod}` : `${p.hour}:${p.minute}`
  return withDate ? `${p.month} ${p.day} ${hm}` : hm
}

// ── 1m → displayTf resampling ────────────────────────────────────────────────
// Buckets are anchored to the session's first bar (09:30 ET for RTH data, or
// 18:00 ET for ETH), so the 1h view shows 09:30/10:30/... candles like
// TradingView. Works for arbitrary minute timeframes.
export function bucketStartFor(shifted: number, anchorShifted: number, tfMin: number): number {
  const secs = tfMin * 60
  return shifted - ((shifted - anchorShifted) % secs)
}

export function resample(
  bars: TVBar[], tfMin: number, volUpColor: string = VOL_UP, volDownColor: string = VOL_DOWN,
): { candles: CandlestickData<UTCTimestamp>[]; vols: HistogramData<UTCTimestamp>[] } {
  const candles: CandlestickData<UTCTimestamp>[] = []
  const vols: HistogramData<UTCTimestamp>[] = []
  if (bars.length === 0) return { candles, vols }
  const anchor = etShift(bars[0].time)
  let cur: CandlestickData<UTCTimestamp> | null = null
  let curVol = 0
  const flush = () => {
    if (!cur) return
    candles.push(cur)
    vols.push({ time: cur.time, value: curVol, color: cur.close >= cur.open ? volUpColor : volDownColor })
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

// ── session-time indicator bands (GOAL B) ────────────────────────────────────
// A lightweight-charts v4 series primitive that shades full-height translucent
// bands per ICT session. Boundaries are ET wall-clock (candle times are already
// ET-shifted, so getUTC* == ET). Non-overlapping display windows, handed off at
// the shared boundaries the backend uses (London 02:00–05:00, NY lunch
// 11:00–14:00):
//   Asia    18:00 → 02:00     London  02:00 → 05:00
//   NY AM   09:30 → 11:00     NY Lunch 11:00 → 14:00     NY PM 14:00 → 16:00
//   (05:00–09:30 pre-market and 16:00–18:00 post-close are left unshaded)
// In RTH-only data the overnight bands simply have no bars and are skipped.
export type SessionKey = 'asia' | 'london' | 'nyAm' | 'nyLunch' | 'nyPm'

const SESSION_COLORS: Record<SessionKey, { dark: string; light: string; label: string }> = {
  asia: { dark: 'rgba(139, 92, 246, 0.12)', light: 'rgba(139, 92, 246, 0.09)', label: 'Asia' },
  london: { dark: 'rgba(59, 130, 246, 0.12)', light: 'rgba(59, 130, 246, 0.09)', label: 'London' },
  nyAm: { dark: 'rgba(34, 197, 94, 0.11)', light: 'rgba(34, 197, 94, 0.08)', label: 'NY AM' },
  nyLunch: { dark: 'rgba(245, 158, 11, 0.12)', light: 'rgba(245, 158, 11, 0.10)', label: 'NY Lunch' },
  nyPm: { dark: 'rgba(20, 184, 166, 0.11)', light: 'rgba(20, 184, 166, 0.08)', label: 'NY PM' },
}
const SESSION_LABEL_DARK = 'rgba(209, 212, 220, 0.38)'
const SESSION_LABEL_LIGHT = 'rgba(25, 25, 25, 0.32)'

export function classifyEtSession(etMinutes: number): SessionKey | null {
  if (etMinutes >= 1080 || etMinutes < 120) return 'asia'     // 18:00–02:00
  if (etMinutes < 300) return 'london'                        // 02:00–05:00
  if (etMinutes >= 570 && etMinutes < 660) return 'nyAm'      // 09:30–11:00
  if (etMinutes >= 660 && etMinutes < 840) return 'nyLunch'   // 11:00–14:00
  if (etMinutes >= 840 && etMinutes < 960) return 'nyPm'      // 14:00–16:00
  return null
}

export type BandRange = { session: SessionKey; i0: number; i1: number }

// Group contiguous same-session runs of display buckets. Band index i maps 1:1
// to the candle's logical index on the time scale, so bands are placed via
// logicalToCoordinate — timezone/label changes never move them.
export function computeBandRanges(candles: CandlestickData<UTCTimestamp>[]): BandRange[] {
  const ranges: BandRange[] = []
  let cur: SessionKey | null = null
  let start = 0
  for (let i = 0; i < candles.length; i++) {
    const d = new Date((candles[i].time as number) * 1000)
    const s = classifyEtSession(d.getUTCHours() * 60 + d.getUTCMinutes())
    if (s !== cur) {
      if (cur) ranges.push({ session: cur, i0: start, i1: i - 1 })
      cur = s
      start = i
    }
  }
  if (cur) ranges.push({ session: cur, i0: start, i1: candles.length - 1 })
  return ranges
}

type DrawTarget = Parameters<ISeriesPrimitivePaneRenderer['draw']>[0]

class SessionBandsRenderer implements ISeriesPrimitivePaneRenderer {
  constructor(private _src: SessionBands) {}
  draw(target: DrawTarget) {
    const src = this._src
    const chart = src.chart
    if (!chart) return
    const ts = chart.timeScale()
    // Media-space x extents from logical indices (bucket i == logical index i).
    const spans: { session: SessionKey; left: number; right: number }[] = []
    for (const b of src.ranges) {
      if (!src.visibility[b.session]) continue
      const x1 = ts.logicalToCoordinate((b.i0 - 0.5) as unknown as Logical)
      const x2 = ts.logicalToCoordinate((b.i1 + 0.5) as unknown as Logical)
      if (x1 == null || x2 == null) continue
      spans.push({ session: b.session, left: Math.min(x1, x2), right: Math.max(x1, x2) })
    }
    if (spans.length === 0) return
    target.useBitmapCoordinateSpace((scope) => {
      const ctx = scope.context
      const hr = scope.horizontalPixelRatio
      const H = scope.bitmapSize.height
      const W = scope.bitmapSize.width
      for (const s of spans) {
        let l = Math.round(s.left * hr)
        let r = Math.round(s.right * hr)
        if (r <= 0 || l >= W) continue
        l = Math.max(l, 0)
        r = Math.min(r, W)
        if (r <= l) continue
        ctx.fillStyle = src.dark ? SESSION_COLORS[s.session].dark : SESSION_COLORS[s.session].light
        ctx.fillRect(l, 0, r - l, H)
      }
    })
    target.useMediaCoordinateSpace((scope) => {
      const ctx = scope.context
      const W = scope.mediaSize.width
      ctx.font = '10px -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif'
      ctx.textBaseline = 'top'
      ctx.fillStyle = src.dark ? SESSION_LABEL_DARK : SESSION_LABEL_LIGHT
      for (const s of spans) {
        if (s.right - s.left < 46) continue
        const x = Math.max(s.left, 2) + 4
        if (x > W) continue
        ctx.fillText(SESSION_COLORS[s.session].label, x, 4)
      }
    })
  }
}

class SessionBandsPaneView implements ISeriesPrimitivePaneView {
  private _renderer: SessionBandsRenderer
  constructor(src: SessionBands) {
    this._renderer = new SessionBandsRenderer(src)
    this._src = src
  }
  private _src: SessionBands
  zOrder(): SeriesPrimitivePaneViewZOrder {
    return 'bottom'
  }
  renderer(): ISeriesPrimitivePaneRenderer | null {
    const src = this._src
    if (!src.enabled || !src.chart || src.ranges.length === 0) return null
    return this._renderer
  }
}

class SessionBands implements ISeriesPrimitive<Time> {
  private _chart: IChartApi | null = null
  private _requestUpdate: (() => void) | null = null
  private _paneViews: SessionBandsPaneView[]
  ranges: BandRange[] = []
  enabled = false
  dark = true
  visibility: Record<SessionKey, boolean> = { asia: true, london: true, nyAm: true, nyLunch: true, nyPm: true }

  constructor() {
    this._paneViews = [new SessionBandsPaneView(this)]
  }
  get chart(): IChartApi | null {
    return this._chart
  }
  attached(p: SeriesAttachedParameter<Time>) {
    this._chart = p.chart
    this._requestUpdate = p.requestUpdate
  }
  detached() {
    this._chart = null
    this._requestUpdate = null
  }
  updateAllViews() {
    // Coordinates are read live in the renderer; nothing to precompute here.
  }
  paneViews(): readonly ISeriesPrimitivePaneView[] {
    return this._paneViews
  }
  setBands(r: BandRange[]) {
    this.ranges = r
    this._requestUpdate?.()
  }
  setOptions(enabled: boolean, visibility: Record<SessionKey, boolean>, dark: boolean) {
    this.enabled = enabled
    this.visibility = visibility
    this.dark = dark
    this._requestUpdate?.()
  }
}

// ─────────────────────────────────────────────────────────────────────────────

export default function TVReplayChart({
  instrument, bars, displayTf, resetKey, showDate, pdh, pdl, position, trades = [],
  upColor, downColor, background, sessionsEnabled, sessionVisibility,
  timezone = 'America/New_York', hour12 = false, onChartClick, onContextMenu,
}: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const rootRef = useRef<HTMLDivElement | null>(null)
  const legendRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volumeRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const priceLinesRef = useRef<IPriceLine[]>([])
  const sessionBandsRef = useRef<SessionBands | null>(null)
  // What is currently rendered: series identity + bucket count, so reveals go
  // through series.update() and only hard resets call setData().
  const renderedRef = useRef<{ key: string; count: number; vol: boolean }>({ key: '', count: 0, vol: false })
  const lastCandleRef = useRef<CandlestickData<UTCTimestamp> | null>(null)
  const isDarkRef = useRef(document.documentElement.classList.contains('dark'))
  const applyThemeRef = useRef<(() => void) | null>(null)

  const upC = upColor ?? UP
  const downC = downColor ?? DOWN
  const resolvedVisibility: Record<SessionKey, boolean> = {
    asia: sessionVisibility?.asia ?? true,
    london: sessionVisibility?.london ?? true,
    nyAm: sessionVisibility?.nyAm ?? true,
    nyLunch: sessionVisibility?.nyLunch ?? true,
    nyPm: sessionVisibility?.nyPm ?? true,
  }
  // Mount-scope closures (crosshair/click/context handlers, theme observer,
  // formatters) read live props through refs so nothing goes stale.
  const liveRef = useRef({ instrument, displayTf, showDate, timezone, hour12, onChartClick, onContextMenu })
  liveRef.current = { instrument, displayTf, showDate, timezone, hour12, onChartClick, onContextMenu }
  const backgroundRef = useRef<string | null>(background ?? null)
  backgroundRef.current = background ?? null
  const sessionsEnabledRef = useRef<boolean>(sessionsEnabled ?? false)
  sessionsEnabledRef.current = sessionsEnabled ?? false
  const sessionVisibilityRef = useRef<Record<SessionKey, boolean>>(resolvedVisibility)
  sessionVisibilityRef.current = resolvedVisibility

  const setLegend = (c: CandlestickData<UTCTimestamp> | null) => {
    const el = legendRef.current
    if (!el) return
    if (!c) {
      el.innerHTML = ''
      return
    }
    const t = isDarkRef.current ? THEMES.dark : THEMES.light
    const up = c.close >= c.open
    const dirColor = up ? upC : downC
    const chg = c.open !== 0 ? ((c.close - c.open) / c.open) * 100 : 0
    const cell = (label: string, v: number) =>
      `<span style="color:${t.legendMuted}">${label}</span> <span style="color:${dirColor}">${v.toFixed(2)}</span>`
    const clock = fmtZoned(c.time, liveRef.current.timezone, liveRef.current.hour12, false)
    el.innerHTML =
      `<span style="color:${t.text};font-weight:700">${liveRef.current.instrument}</span> ` +
      `<span style="color:${t.legendMuted}">· ${tfLabel(liveRef.current.displayTf)} · ${clock}</span>&nbsp; ` +
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

  const themeOptions = (dark: boolean, bgOverride?: string | null) => {
    const t = dark ? THEMES.dark : THEMES.light
    return {
      layout: { background: { type: ColorType.Solid as const, color: bgOverride ?? t.bg }, textColor: t.text },
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

  // ── mount: create chart + series + primitive + observers ──────────────────
  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    const chart = createChart(el, {
      width: el.clientWidth,
      height: el.clientHeight,
      ...themeOptionsRef.current(isDarkRef.current, backgroundRef.current),
      timeScale: {
        borderColor: (isDarkRef.current ? THEMES.dark : THEMES.light).border,
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 6,
        barSpacing: 8,
        // Axis is always time-only (never prints the date), so blind mode can
        // never leak the date through a tick label, in any timezone/format.
        tickMarkFormatter: (time: Time) =>
          fmtZoned(time, liveRef.current.timezone, liveRef.current.hour12, false),
      },
      localization: {
        timeFormatter: (time: Time) =>
          fmtZoned(time, liveRef.current.timezone, liveRef.current.hour12, liveRef.current.showDate),
      },
    })

    const candle = chart.addCandlestickSeries({
      upColor: upC, downColor: downC, wickUpColor: upC, wickDownColor: downC,
      borderVisible: false,
      priceFormat: { type: 'price', ...(PRICE_FORMAT[instrument] ?? { precision: 2, minMove: 0.25 }) },
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

    // Session bands drawn behind the candles (zOrder 'bottom').
    const sessionBands = new SessionBands()
    candle.attachPrimitive(sessionBands)
    sessionBands.setOptions(sessionsEnabledRef.current, sessionVisibilityRef.current, isDarkRef.current)

    chartRef.current = chart
    candleRef.current = candle
    volumeRef.current = volume
    sessionBandsRef.current = sessionBands
    renderedRef.current = { key: '', count: 0, vol: false }

    // OHLC legend: hovered candle, falling back to the latest bar.
    const onCrosshair = (param: MouseEventParams) => {
      const d = param.time != null
        ? (param.seriesData.get(candle) as CandlestickData<UTCTimestamp> | undefined)
        : undefined
      setLegendRef.current(d ?? lastCandleRef.current)
    }
    chart.subscribeCrosshairMove(onCrosshair)

    // GOAL G: expose the clicked price so the page can arm place-on-chart SL/TP.
    const onClick = (param: MouseEventParams) => {
      const h = liveRef.current.onChartClick
      if (!h || !param.point) return
      const price = candleRef.current?.coordinateToPrice(param.point.y)
      if (price != null) h(price)
    }
    chart.subscribeClick(onClick)

    // GOAL H: right-click opens the page's context menu (only over the chart).
    const onCtx = (e: MouseEvent) => {
      const h = liveRef.current.onContextMenu
      if (!h) return
      e.preventDefault()
      h(e.clientX, e.clientY)
    }
    el.addEventListener('contextmenu', onCtx)

    // Theme: follow the app's dark class on <html> live (respecting bg override).
    const applyTheme = () => {
      const dark = document.documentElement.classList.contains('dark')
      isDarkRef.current = dark
      const bg = backgroundRef.current
      chart.applyOptions({
        ...themeOptionsRef.current(dark, bg),
        watermark: watermarkOptionsRef.current(dark),
      })
      const eff = bg ?? (dark ? THEMES.dark.bg : THEMES.light.bg)
      if (containerRef.current) containerRef.current.style.background = eff
      if (rootRef.current) rootRef.current.style.background = eff
      sessionBandsRef.current?.setOptions(sessionsEnabledRef.current, sessionVisibilityRef.current, dark)
      setLegendRef.current(lastCandleRef.current)
    }
    applyThemeRef.current = applyTheme
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
      el.removeEventListener('contextmenu', onCtx)
      chart.unsubscribeCrosshairMove(onCrosshair)
      chart.unsubscribeClick(onClick)
      chart.remove()
      chartRef.current = null
      candleRef.current = null
      volumeRef.current = null
      sessionBandsRef.current = null
      applyThemeRef.current = null
      priceLinesRef.current = []
      lastCandleRef.current = null
      renderedRef.current = { key: '', count: 0, vol: false }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── watermark + tz/format labels on prop change ───────────────────────────
  // Applies timezone/12-24h/date-mask LIVE: new formatter closures invalidate
  // the tick cache, forcing a relabel with no recreate and no loss of zoom.
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    chart.applyOptions({
      watermark: watermarkOptionsRef.current(isDarkRef.current),
      localization: {
        timeFormatter: (time: Time) =>
          fmtZoned(time, liveRef.current.timezone, liveRef.current.hour12, liveRef.current.showDate),
      },
      timeScale: {
        tickMarkFormatter: (time: Time) =>
          fmtZoned(time, liveRef.current.timezone, liveRef.current.hour12, false),
      },
    })
    candleRef.current?.applyOptions({
      priceFormat: { type: 'price', ...(PRICE_FORMAT[instrument] ?? { precision: 2, minMove: 0.25 }) },
    })
    setLegendRef.current(lastCandleRef.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instrument, displayTf, showDate, timezone, hour12])

  // ── colors: apply candle/wick + background live; retint existing volume ───
  useEffect(() => {
    const candle = candleRef.current
    if (!candle) return
    candle.applyOptions({ upColor: upC, downColor: downC, wickUpColor: upC, wickDownColor: downC })
    applyThemeRef.current?.() // re-applies bg override (or theme bg) + wrapper bg
    const volume = volumeRef.current
    if (volume && renderedRef.current.vol && bars.length) {
      const { vols } = resample(bars, displayTf, withAlpha(upC, 0.45), withAlpha(downC, 0.45))
      volume.setData(vols)
    }
    setLegendRef.current(lastCandleRef.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [upC, downC, background])

  // ── session bands: enable/visibility toggles live ─────────────────────────
  useEffect(() => {
    sessionBandsRef.current?.setOptions(sessionsEnabledRef.current, sessionVisibilityRef.current, isDarkRef.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionsEnabled, resolvedVisibility.asia, resolvedVisibility.london, resolvedVisibility.nyAm, resolvedVisibility.nyLunch, resolvedVisibility.nyPm])

  // ── data: setData only on hard reset, series.update() during playback ─────
  useEffect(() => {
    const chart = chartRef.current
    const candle = candleRef.current
    const volume = volumeRef.current
    if (!chart || !candle || !volume) return
    const key = `${resetKey}|${displayTf}`
    const volUp = withAlpha(upC, 0.45)
    const volDown = withAlpha(downC, 0.45)
    if (bars.length === 0) {
      if (renderedRef.current.key !== key || renderedRef.current.count > 0) {
        candle.setData([])
        volume.setData([])
        sessionBandsRef.current?.setBands([])
        renderedRef.current = { key, count: 0, vol: false }
        lastCandleRef.current = null
        setLegend(null)
      }
      return
    }
    const { candles, vols } = resample(bars, displayTf, volUp, volDown)
    // Prod candle_cache volume is sparse on some days (4-24% of 1m bars have
    // any) — a few isolated spikes over a blank band reads as broken, so only
    // show the pane when a meaningful share of revealed bars carry volume.
    const nonzero = bars.reduce((n, b) => n + ((b.volume ?? 0) > 0 ? 1 : 0), 0)
    const hasVolume = bars.length > 0 && nonzero / bars.length >= 0.3
    const prev = renderedRef.current
    if (prev.key !== key || candles.length < prev.count) {
      // New day / TF switch / rewind: full redraw, then snap to the right edge.
      candle.setData(candles)
      volume.setData(hasVolume ? vols : [])
      chart.timeScale().scrollToRealTime()
    } else {
      // Reveal path: update the in-progress bucket + append completed ones.
      // Same-time update replaces the forming candle — TradingView tick behavior.
      if (hasVolume !== prev.vol) volume.setData(hasVolume ? vols : []) // threshold crossed: backfill/clear in one shot
      for (let i = Math.max(0, prev.count - 1); i < candles.length; i++) {
        candle.update(candles[i])
        if (hasVolume && prev.vol) volume.update(vols[i])
      }
    }
    sessionBandsRef.current?.setBands(computeBandRanges(candles))
    renderedRef.current = { key, count: candles.length, vol: hasVolume }
    lastCandleRef.current = candles[candles.length - 1] ?? null
    setLegend(lastCandleRef.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bars, displayTf, resetKey])

  // ── price lines: entry + optional SL/TP + PDH/PDL ─────────────────────────
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
      // GOAL G: draw only the levels that exist; a cleared SL/TP removes its line.
      if (position.stopPrice != null) add(position.stopPrice, DOWN, LineStyle.Dashed, 'SL')
      if (position.targetPrice != null) add(position.targetPrice, UP, LineStyle.Dashed, 'TP')
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
      // GOAL G: r may be null (no initial stop) — show a neutral exit glyph.
      const hasR = tr.r != null
      markers.push({
        time: snap(tr.exitTime),
        position: 'aboveBar',
        shape: 'circle',
        color: !hasR ? ENTRY_LINE : (tr.r! >= 0 ? UP : DOWN),
        text: !hasR ? '' : `${tr.r! >= 0 ? '+' : ''}${tr.r!.toFixed(1)}R`,
      })
    }
    if (position) markers.push(entryMarker(position.entryTime, position.direction))
    // setMarkers requires ascending times.
    markers.sort((a, b) => (a.time as number) - (b.time as number))
    candle.setMarkers(markers)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trades, position, displayTf, bars, resetKey])

  return (
    <div ref={rootRef} className="relative w-full h-full">
      <div ref={containerRef} className="absolute inset-0"/>
      <div
        ref={legendRef}
        className="absolute top-2 left-3 z-10 pointer-events-none select-none text-[11px] font-medium tracking-tight whitespace-nowrap"
      />
    </div>
  )
}
