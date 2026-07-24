// ─────────────────────────────────────────────────────────────────────────────
// replayDrawings.ts — FX-Replay / TradingView-style drawing layer for the
// replay chart (lightweight-charts v4). Builder 1: the DRAWING ENGINE.
//
// lightweight-charts ships NO drawing tools. This module implements them as a
// single custom SERIES PRIMITIVE (attach via candleSeries.attachPrimitive) that
// owns the whole drawing list, the in-progress preview and the selection
// handles, and renders them on the pane canvas each frame. It doubles as the
// interaction CONTROLLER: the chart forwards raw container mouse/keyboard events
// to it, and the page drives tool/style/delete/clear through a small imperative
// api (see DrawingApi + DrawingState at the bottom — that is builder 2's
// contract).
//
// COORDINATE STRATEGY (see notes in the builder return):
//   Every anchor is stored as { time, price } where `time` is the ET-SHIFTED
//   epoch second (same space as the candles — the output of etShift()) and
//   `price` is the real price. This is the ONLY representation that survives the
//   three invariants the replay chart requires:
//     • reveal (series.update appends) — times are stable,
//     • timeframe switch (series.setData with a different bucket count) — we
//       recompute a fresh bar-time table each data push and re-derive the
//       logical index per frame, so nothing drifts,
//     • pan / zoom — logical→pixel is read live from the time scale.
//   time↔pixel goes time → fractional-logical (interp/extrapolate against the
//   current bar-time table) → timeScale.logicalToCoordinate. Logical works in
//   whitespace (past the last bar / between bars), so rays / extended lines /
//   future-projected endpoints never return null and never vanish.
//
// BLIND MODE: nothing here ever renders a date. Horizontal lines print a PRICE
// on the axis; text is user-authored. Persistence is keyed by INSTRUMENT ONLY
// (never by date), so a stored blind day cannot leak its date through the key.
// ─────────────────────────────────────────────────────────────────────────────
import type {
  IChartApi, ISeriesApi, ISeriesPrimitive, ISeriesPrimitivePaneRenderer,
  ISeriesPrimitivePaneView, ISeriesPrimitiveAxisView, SeriesAttachedParameter,
  SeriesPrimitivePaneViewZOrder, Time, Logical,
} from 'lightweight-charts'

// The fancy-canvas render target type, recovered without importing fancy-canvas
// (same trick the SessionBands primitive uses in TVReplayChart).
type DrawTarget = Parameters<ISeriesPrimitivePaneRenderer['draw']>[0]

// ── public types ─────────────────────────────────────────────────────────────

/** All palette tools. 'cursor' = select/drag (no drawing). */
export type DrawTool =
  | 'cursor' | 'trendline' | 'ray' | 'extended' | 'hline' | 'hray'
  | 'vline' | 'rect' | 'fib' | 'text' | 'measure'

/** Tools that actually produce a persisted drawing (everything but the cursor). */
export type DrawKind = Exclude<DrawTool, 'cursor'>

export type LineDash = 'solid' | 'dashed' | 'dotted'

export type DrawStyle = {
  color: string
  /** line width in CSS px, 1–4. */
  width: number
  dash: LineDash
  /** fill opacity 0–1 for rect / fib bands / measure. */
  fillOpacity: number
  /** font size in CSS px for the text tool. */
  fontSize: number
}

/** One endpoint: ET-shifted epoch seconds + real price. Never rendered as text. */
export type Anchor = { time: number; price: number }

export type Drawing = {
  id: string
  tool: DrawKind
  /** 1 anchor for hline/hray/vline/text; 2 for everything else. */
  points: Anchor[]
  style: DrawStyle
  /** only for the text tool. */
  text?: string
}

/** A resampled display candle (what the engine needs for magnet + bar counts). */
export type DrawBar = { time: number; open: number; high: number; low: number; close: number }

/** Info about the current selection, surfaced to the page's style bar. */
export type SelectionInfo = {
  id: string
  tool: DrawKind
  style: DrawStyle
  text?: string
  /** rect / fib / measure expose a fill-opacity control. */
  supportsFill: boolean
  /** the text tool exposes a font-size + edit-text control. */
  supportsText: boolean
}

/** The full engine state pushed to the page after every mutation. */
export type DrawingState = {
  activeTool: DrawTool
  magnet: boolean
  hidden: boolean
  locked: boolean
  /** when true the active tool stays selected after a draw (TV "pin"). */
  stayInTool: boolean
  count: number
  selection: SelectionInfo | null
  defaults: DrawStyle
}

/** Imperative handle handed to the page (builder 2) via TVReplayChart.onDrawingApi. */
export type DrawingApi = {
  setActiveTool(tool: DrawTool): void
  getActiveTool(): DrawTool
  setDefaults(patch: Partial<DrawStyle>): void
  setMagnet(on: boolean): void
  setHidden(on: boolean): void
  setLocked(on: boolean): void
  setStayInTool(on: boolean): void
  /** restyle the current selection (no-op if nothing selected). */
  restyleSelected(patch: Partial<DrawStyle>): void
  deleteSelected(): void
  clearAll(): void
  /** open a prompt to edit the selected text drawing (no-op otherwise). */
  editSelectedText(): void
  selectNone(): void
  hasSelection(): boolean
  getState(): DrawingState
  /** subscribe to state changes; returns an unsubscribe fn. */
  subscribe(fn: (s: DrawingState) => void): () => void
}

export const DEFAULT_DRAW_STYLE: DrawStyle = {
  color: '#2962ff', width: 2, dash: 'solid', fillOpacity: 0.12, fontSize: 14,
}

export const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1]

// ── constants ────────────────────────────────────────────────────────────────
const HIT_TOL = 6        // px, body hit tolerance
const HANDLE_TOL = 9     // px, handle grab tolerance
const HANDLE_SZ = 4      // px, handle half-size (drawn)
const CLICK_SLOP = 4     // px, drag-vs-click threshold on a two-point tool
const BIG = 1e5          // px, ray / extended-line extension (canvas clips to pane)
const PREVIEW_ID = '__preview__'

// ── persistence (per instrument — NEVER per date) ────────────────────────────
const KEY_PREFIX = 'theta_replay_drawings_v1_'
const keyFor = (instrument: string) => KEY_PREFIX + instrument

export function loadDrawings(instrument: string): Drawing[] {
  try {
    const raw = localStorage.getItem(keyFor(instrument))
    if (!raw) return []
    const p = JSON.parse(raw)
    const list: unknown = Array.isArray(p) ? p : p?.drawings
    if (!Array.isArray(list)) return []
    const out: Drawing[] = []
    for (const d of list) {
      if (!d || typeof d !== 'object') continue
      const dd = d as Record<string, unknown>
      if (typeof dd.tool !== 'string' || !Array.isArray(dd.points)) continue
      const pts = (dd.points as unknown[])
        .filter((a): a is Anchor => !!a && typeof (a as Anchor).time === 'number' && typeof (a as Anchor).price === 'number')
        .map((a) => ({ time: (a as Anchor).time, price: (a as Anchor).price }))
      if (pts.length === 0) continue
      out.push({
        id: typeof dd.id === 'string' ? dd.id : uid(),
        tool: dd.tool as DrawKind,
        points: pts,
        style: normalizeStyle(dd.style),
        text: typeof dd.text === 'string' ? dd.text : undefined,
      })
    }
    return out
  } catch {
    return []
  }
}

export function saveDrawings(instrument: string, drawings: Drawing[]): void {
  try {
    localStorage.setItem(keyFor(instrument), JSON.stringify({ v: 1, drawings }))
  } catch {
    /* quota — ignore */
  }
}

function normalizeStyle(s: unknown): DrawStyle {
  const o = (s && typeof s === 'object' ? s : {}) as Record<string, unknown>
  return {
    color: typeof o.color === 'string' ? o.color : DEFAULT_DRAW_STYLE.color,
    width: clampNum(o.width, 1, 4, DEFAULT_DRAW_STYLE.width),
    dash: o.dash === 'dashed' || o.dash === 'dotted' ? o.dash : 'solid',
    fillOpacity: clampNum(o.fillOpacity, 0, 1, DEFAULT_DRAW_STYLE.fillOpacity),
    fontSize: clampNum(o.fontSize, 8, 48, DEFAULT_DRAW_STYLE.fontSize),
  }
}
function clampNum(v: unknown, lo: number, hi: number, dflt: number): number {
  const n = typeof v === 'number' ? v : Number(v)
  if (!Number.isFinite(n)) return dflt
  return Math.min(hi, Math.max(lo, n))
}

let _seq = 0
function uid(): string {
  return 'd' + Date.now().toString(36) + (_seq++).toString(36) + Math.floor(Math.random() * 1e6).toString(36)
}

// ── pure geometry helpers (exported for headless testing) ────────────────────

/** Distance from point P to segment AB, in the same (screen) units as the inputs. */
export function distToSegment(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax
  const dy = by - ay
  const len2 = dx * dx + dy * dy
  if (len2 === 0) return Math.hypot(px - ax, py - ay)
  let t = ((px - ax) * dx + (py - ay) * dy) / len2
  t = Math.max(0, Math.min(1, t))
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy))
}

/** True if P is within `tol` px of the axis-aligned rectangle's BORDER (click-through fill). */
export function nearRectBorder(px: number, py: number, x0: number, y0: number, x1: number, y1: number, tol: number): boolean {
  const minx = Math.min(x0, x1), maxx = Math.max(x0, x1)
  const miny = Math.min(y0, y1), maxy = Math.max(y0, y1)
  const inX = px >= minx - tol && px <= maxx + tol
  const inY = py >= miny - tol && py <= maxy + tol
  const nearV = (Math.abs(px - minx) <= tol || Math.abs(px - maxx) <= tol) && inY
  const nearH = (Math.abs(py - miny) <= tol || Math.abs(py - maxy) <= tol) && inX
  return nearV || nearH
}

/** True if P is inside the (tol-expanded) axis-aligned rectangle. */
export function pointInRect(px: number, py: number, x0: number, y0: number, x1: number, y1: number, tol = 0): boolean {
  return (
    px >= Math.min(x0, x1) - tol && px <= Math.max(x0, x1) + tol &&
    py >= Math.min(y0, y1) - tol && py <= Math.max(y0, y1) + tol
  )
}

// ── time ↔ logical (pure, exported for headless testing) ──────────────────────
// barTimes = ascending ET-shifted candle times; logical index == array index.
// In range: interpolate between neighbours (linear in epoch == linear in logical
// because intraday buckets are contiguous). Out of range: extrapolate using the
// bucket size tfSec, matching how the chart lays out uniform whitespace slots.

export function timeToLogical(time: number, barTimes: number[], tfSec: number): number | null {
  const n = barTimes.length
  if (n === 0 || tfSec <= 0) return null
  if (n === 1) return (time - barTimes[0]) / tfSec
  if (time <= barTimes[0]) return -(barTimes[0] - time) / tfSec
  if (time >= barTimes[n - 1]) return (n - 1) + (time - barTimes[n - 1]) / tfSec
  let lo = 0, hi = n - 1
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1
    if (barTimes[mid] <= time) lo = mid
    else hi = mid
  }
  const span = barTimes[hi] - barTimes[lo]
  const frac = span > 0 ? (time - barTimes[lo]) / span : 0
  return lo + frac
}

export function logicalToTime(logical: number, barTimes: number[], tfSec: number): number | null {
  const n = barTimes.length
  if (n === 0 || tfSec <= 0) return null
  if (n === 1) return Math.round(barTimes[0] + logical * tfSec)
  if (logical <= 0) return Math.round(barTimes[0] + logical * tfSec)
  if (logical >= n - 1) return Math.round(barTimes[n - 1] + (logical - (n - 1)) * tfSec)
  const i = Math.floor(logical)
  const frac = logical - i
  return Math.round(barTimes[i] + frac * (barTimes[i + 1] - barTimes[i]))
}

// ── color helpers ─────────────────────────────────────────────────────────────
function withAlpha(color: string, alpha: number): string {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color.trim())
  if (!m) return color
  let h = m[1]
  if (h.length === 3) h = h.split('').map((c) => c + c).join('')
  const n = parseInt(h, 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`
}

const FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif'

// ─────────────────────────────────────────────────────────────────────────────
// Renderer — projects stored (time, price) anchors to pixels every frame and
// paints all committed drawings + the preview + selection handles. Lines/fills
// draw in bitmap space (×pixelRatio, crisp); text draws in media space.
// ─────────────────────────────────────────────────────────────────────────────
class DrawingsRenderer implements ISeriesPrimitivePaneRenderer {
  constructor(private _src: DrawingsPrimitive) {}

  draw(target: DrawTarget) {
    const P = this._src
    if (!P.chart || !P.series) return

    // Committed drawings are hidden by hide-all; the live preview always shows.
    const list: { d: Drawing; sel: boolean; hov: boolean }[] = []
    if (!P.hidden) {
      for (const d of P.drawings) list.push({ d, sel: d.id === P.selectedId, hov: d.id === P.hoverId })
    }
    if (P.preview) list.push({ d: P.preview, sel: false, hov: false })

    // ---- bitmap pass: lines, fills, handles ----
    target.useBitmapCoordinateSpace((scope) => {
      const ctx = scope.context
      const hr = scope.horizontalPixelRatio
      const vr = scope.verticalPixelRatio
      const W = scope.bitmapSize.width
      const H = scope.bitmapSize.height
      for (const { d, sel, hov } of list) {
        this._drawGeom(ctx, d, hr, vr, W, H, sel, hov, !!P.preview && d.id === PREVIEW_ID)
      }
      // handles on top of everything
      if (!P.hidden && P.selectedId) {
        const d = P.drawings.find((x) => x.id === P.selectedId)
        if (d) this._drawHandles(ctx, P.handlePoints(d), hr, vr)
      }
    })

    // ---- media pass: text (labels are legible, not pixel-snapped) ----
    target.useMediaCoordinateSpace((scope) => {
      const ctx = scope.context
      for (const { d } of list) this._drawText(ctx, d, P)
    })
  }

  private _drawGeom(
    ctx: CanvasRenderingContext2D, d: Drawing, hr: number, vr: number, W: number, H: number,
    sel: boolean, hov: boolean, isPreview: boolean,
  ) {
    const P = this._src
    const st = d.style
    const lw = Math.max(1, st.width) * vr + (sel || hov ? vr : 0)
    ctx.save()
    ctx.globalAlpha = isPreview ? 0.75 : 1
    ctx.strokeStyle = st.color
    ctx.fillStyle = withAlpha(st.color, st.fillOpacity)
    ctx.lineWidth = lw
    this._dash(ctx, st.dash, vr)
    ctx.lineCap = st.dash === 'dotted' ? 'round' : 'butt'

    const X = (v: number) => v * hr
    const Y = (v: number) => v * vr

    switch (d.tool) {
      case 'trendline': {
        const a = P.pt(d.points[0]); const b = P.pt(d.points[1])
        if (a && b) this._seg(ctx, X(a.x), Y(a.y), X(b.x), Y(b.y))
        break
      }
      case 'ray':
      case 'extended': {
        const a = P.pt(d.points[0]); const b = P.pt(d.points[1])
        if (a && b) {
          let dx = b.x - a.x, dy = b.y - a.y
          const len = Math.hypot(dx, dy) || 1
          dx /= len; dy /= len
          const x2 = X(a.x + dx * BIG), y2 = Y(a.y + dy * BIG)
          const x1 = d.tool === 'extended' ? X(a.x - dx * BIG) : X(a.x)
          const y1 = d.tool === 'extended' ? Y(a.y - dy * BIG) : Y(a.y)
          this._seg(ctx, x1, y1, x2, y2)
        }
        break
      }
      case 'hline': {
        const y = P.yOf(d.points[0].price)
        if (y != null) this._seg(ctx, 0, Y(y), W, Y(y))
        break
      }
      case 'hray': {
        const x = P.xOf(d.points[0].time); const y = P.yOf(d.points[0].price)
        if (x != null && y != null) this._seg(ctx, X(x), Y(y), W, Y(y))
        break
      }
      case 'vline': {
        const x = P.xOf(d.points[0].time)
        if (x != null) this._seg(ctx, X(x), 0, X(x), H)
        break
      }
      case 'rect':
      case 'measure': {
        const a = P.pt(d.points[0]); const b = P.pt(d.points[1])
        if (a && b) {
          const rx = X(Math.min(a.x, b.x)), ry = Y(Math.min(a.y, b.y))
          const rw = Math.abs(X(b.x) - X(a.x)), rh = Math.abs(Y(b.y) - Y(a.y))
          ctx.setLineDash([])
          ctx.fillRect(rx, ry, rw, rh)
          this._dash(ctx, st.dash, vr)
          ctx.strokeRect(rx, ry, rw, rh)
        }
        break
      }
      case 'fib': {
        const a = P.pt(d.points[0]); const b = P.pt(d.points[1])
        if (a && b) {
          const x1 = X(Math.min(a.x, b.x)), x2 = X(Math.max(a.x, b.x))
          const p0 = d.points[0].price, p1 = d.points[1].price
          const ys = FIB_LEVELS.map((l) => {
            const yy = P.yOf(p0 + l * (p1 - p0))
            return yy == null ? null : Y(yy)
          })
          // translucent bands between consecutive levels
          if (st.fillOpacity > 0) {
            for (let i = 0; i < ys.length - 1; i++) {
              const y0 = ys[i]; const y1v = ys[i + 1]
              if (y0 == null || y1v == null) continue
              ctx.fillStyle = withAlpha(st.color, st.fillOpacity * (i % 2 === 0 ? 0.9 : 0.4))
              ctx.fillRect(x1, Math.min(y0, y1v), x2 - x1, Math.abs(y1v - y0))
            }
            ctx.fillStyle = withAlpha(st.color, st.fillOpacity)
          }
          for (const yy of ys) {
            if (yy == null) continue
            this._seg(ctx, x1, yy, x2, yy)
          }
        }
        break
      }
      case 'text': {
        // marker dot at the anchor so an empty label is still grabbable
        const a = P.pt(d.points[0])
        if (a && (sel || hov)) {
          ctx.setLineDash([])
          ctx.beginPath()
          ctx.arc(X(a.x), Y(a.y), 2.5 * vr, 0, Math.PI * 2)
          ctx.fill()
        }
        break
      }
    }
    ctx.restore()
  }

  private _seg(ctx: CanvasRenderingContext2D, x1: number, y1: number, x2: number, y2: number) {
    ctx.beginPath()
    ctx.moveTo(x1, y1)
    ctx.lineTo(x2, y2)
    ctx.stroke()
  }

  private _dash(ctx: CanvasRenderingContext2D, dash: LineDash, r: number) {
    if (dash === 'dashed') ctx.setLineDash([6 * r, 4 * r])
    else if (dash === 'dotted') ctx.setLineDash([Math.max(1, 1.5 * r), 3 * r])
    else ctx.setLineDash([])
  }

  private _drawHandles(ctx: CanvasRenderingContext2D, pts: { x: number; y: number }[], hr: number, vr: number) {
    ctx.save()
    ctx.setLineDash([])
    ctx.lineWidth = Math.max(1, vr)
    const s = HANDLE_SZ * vr
    for (const p of pts) {
      const x = p.x * hr, y = p.y * vr
      ctx.fillStyle = '#ffffff'
      ctx.strokeStyle = '#2962ff'
      ctx.fillRect(x - s, y - s, s * 2, s * 2)
      ctx.strokeRect(x - s, y - s, s * 2, s * 2)
    }
    ctx.restore()
  }

  private _drawText(ctx: CanvasRenderingContext2D, d: Drawing, P: DrawingsPrimitive) {
    if (d.tool === 'text') {
      const a = P.pt(d.points[0])
      if (!a) return
      const label = d.text ?? ''
      if (!label) return
      ctx.save()
      ctx.font = `${d.style.fontSize}px ${FONT}`
      ctx.textBaseline = 'top'
      const w = ctx.measureText(label).width
      ctx.fillStyle = P.dark ? 'rgba(19,23,34,0.72)' : 'rgba(255,255,255,0.78)'
      ctx.fillRect(a.x - 3, a.y - 2, w + 6, d.style.fontSize + 6)
      ctx.fillStyle = d.style.color
      ctx.fillText(label, a.x, a.y)
      ctx.restore()
      return
    }
    if (d.tool === 'fib') {
      const a = P.pt(d.points[0]); const b = P.pt(d.points[1])
      if (!a || !b) return
      const rightX = Math.max(a.x, b.x)
      const p0 = d.points[0].price, p1 = d.points[1].price
      ctx.save()
      ctx.font = `10px ${FONT}`
      ctx.textBaseline = 'middle'
      ctx.fillStyle = d.style.color
      for (const l of FIB_LEVELS) {
        const price = p0 + l * (p1 - p0)
        const yy = P.yOf(price)
        if (yy == null) continue
        ctx.fillText(`${l.toFixed(3)}  ${P.fmtPrice(price)}`, rightX + 4, yy)
      }
      ctx.restore()
      return
    }
    if (d.tool === 'measure') {
      const a = P.pt(d.points[0]); const b = P.pt(d.points[1])
      if (!a || !b) return
      const dPrice = d.points[1].price - d.points[0].price
      const dPct = d.points[0].price !== 0 ? (dPrice / d.points[0].price) * 100 : 0
      const l0 = P.logicalOf(d.points[0].time)
      const l1 = P.logicalOf(d.points[1].time)
      const bars = l0 != null && l1 != null ? Math.abs(Math.round(l1 - l0)) : 0
      const lines = [
        `${dPrice >= 0 ? '+' : ''}${P.fmtPrice(dPrice)}  (${dPct >= 0 ? '+' : ''}${dPct.toFixed(2)}%)`,
        `${bars} bar${bars === 1 ? '' : 's'}`,
      ]
      const cx = (a.x + b.x) / 2
      const cy = (a.y + b.y) / 2
      ctx.save()
      ctx.font = `11px ${FONT}`
      ctx.textBaseline = 'top'
      const w = Math.max(...lines.map((s) => ctx.measureText(s).width))
      const bw = w + 12, bh = lines.length * 15 + 8
      const bx = cx - bw / 2, by = cy - bh / 2
      ctx.fillStyle = withAlpha(d.style.color, 0.92)
      ctx.fillRect(bx, by, bw, bh)
      ctx.fillStyle = '#ffffff'
      lines.forEach((s, i) => ctx.fillText(s, bx + 6, by + 5 + i * 15))
      ctx.restore()
    }
  }
}

class DrawingsPaneView implements ISeriesPrimitivePaneView {
  private _renderer: DrawingsRenderer
  constructor(private _src: DrawingsPrimitive) {
    this._renderer = new DrawingsRenderer(_src)
  }
  zOrder(): SeriesPrimitivePaneViewZOrder {
    return 'top'
  }
  renderer(): ISeriesPrimitivePaneRenderer | null {
    if (!this._src.chart || !this._src.series) return null
    return this._renderer
  }
}

// Price-axis label for horizontal lines / rays (the "price on the axis" spec).
class DrawingsAxisView implements ISeriesPrimitiveAxisView {
  constructor(private _src: DrawingsPrimitive, private _price: number, private _color: string) {}
  coordinate(): number {
    return this._src.yOf(this._price) ?? -100
  }
  text(): string {
    return this._src.fmtPrice(this._price)
  }
  textColor(): string {
    return '#ffffff'
  }
  backColor(): string {
    return this._color
  }
  visible(): boolean {
    return this._src.yOf(this._price) != null
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// DrawingsPrimitive — the primitive AND the interaction controller. One instance
// per chart; owns all state, coordinate conversion, hit-testing and the event
// state machine. The chart forwards pointer/keyboard events; the page drives it
// through getApi().
// ─────────────────────────────────────────────────────────────────────────────
export class DrawingsPrimitive implements ISeriesPrimitive<Time> {
  // lightweight-charts wiring
  private _chart: IChartApi
  private _series: ISeriesApi<'Candlestick'>
  private _requestUpdate: (() => void) | null = null
  private _paneViews: DrawingsPaneView[]

  // persisted state
  instrument: string
  drawings: Drawing[]
  preview: Drawing | null = null
  selectedId: string | null = null
  hoverId: string | null = null

  // tool / mode state
  activeTool: DrawTool = 'cursor'
  defaults: DrawStyle = { ...DEFAULT_DRAW_STYLE }
  magnet = false
  hidden = false
  locked = false
  stayInTool = false
  dark = true

  // data + display context (fed by the chart)
  bars: DrawBar[] = []
  private _barTimes: number[] = []
  tfSec = 60
  pricePrecision = 2
  minMove = 0.25
  private _vw = 0
  private _vh = 0

  // event machine
  private _mode: 'idle' | 'placing' | 'dragBody' | 'dragHandle' = 'idle'
  private _placePts: Anchor[] = []
  private _handleIdx = -1
  private _dragOrig: Anchor[] = []
  private _dragStartLogical = 0
  private _dragStartPrice = 0
  private _downX = 0
  private _downY = 0
  private _moved = false
  /** read + reset by the chart's subscribeClick handler to suppress SL/TP arm. */
  suppressClick = false

  // listeners + external sinks
  private _listeners = new Set<(s: DrawingState) => void>()
  private _cursorSink: ((c: string) => void) | null = null

  constructor(chart: IChartApi, series: ISeriesApi<'Candlestick'>, instrument: string) {
    this._chart = chart
    this._series = series
    this.instrument = instrument
    this.drawings = loadDrawings(instrument)
    this._paneViews = [new DrawingsPaneView(this)]
  }

  // ── ISeriesPrimitive ────────────────────────────────────────────────────────
  get chart(): IChartApi | null {
    return this._chart
  }
  get series(): ISeriesApi<'Candlestick'> | null {
    return this._series
  }
  attached(p: SeriesAttachedParameter<Time>) {
    this._requestUpdate = p.requestUpdate
  }
  detached() {
    this._requestUpdate = null
  }
  updateAllViews() {
    /* coordinates are read live in the renderer; nothing to precompute */
  }
  paneViews(): readonly ISeriesPrimitivePaneView[] {
    return this._paneViews
  }
  priceAxisViews(): readonly ISeriesPrimitiveAxisView[] {
    if (this.hidden) return []
    const out: DrawingsAxisView[] = []
    for (const d of this.drawings) {
      if (d.tool === 'hline' || d.tool === 'hray') {
        out.push(new DrawingsAxisView(this, d.points[0].price, d.style.color))
      }
    }
    return out
  }

  private _req() {
    this._requestUpdate?.()
  }

  // ── coordinate conversion ────────────────────────────────────────────────────
  /** epoch time → x pixel (media). null only if wholly unresolvable. */
  xOf(time: number): number | null {
    const lg = timeToLogical(time, this._barTimes, this.tfSec)
    if (lg == null) return null
    const c = this._chart.timeScale().logicalToCoordinate(lg as unknown as Logical)
    return c == null ? null : (c as unknown as number)
  }
  /** price → y pixel (media). */
  yOf(price: number): number | null {
    const c = this._series.priceToCoordinate(price)
    return c == null ? null : (c as unknown as number)
  }
  /** both, for a two-price/two-time anchor. */
  pt(a: Anchor): { x: number; y: number } | null {
    const x = this.xOf(a.time)
    const y = this.yOf(a.price)
    if (x == null || y == null) return null
    return { x, y }
  }
  logicalOf(time: number): number | null {
    return timeToLogical(time, this._barTimes, this.tfSec)
  }
  fmtPrice(p: number): string {
    return p.toFixed(this.pricePrecision)
  }

  private _logicalAt(x: number): number | null {
    const l = this._chart.timeScale().coordinateToLogical(x)
    return l == null ? null : (l as unknown as number)
  }
  private _priceAt(y: number): number | null {
    const p = this._series.coordinateToPrice(y)
    return p == null ? null : (p as unknown as number)
  }

  /** container (x,y) → a stored anchor, applying magnet snapping. null if off-scale. */
  private _anchorAt(x: number, y: number): Anchor | null {
    const price0 = this._priceAt(y)
    const lg = this._logicalAt(x)
    if (price0 == null || lg == null) return null
    let time = logicalToTime(lg, this._barTimes, this.tfSec)
    if (time == null) return null
    let price = price0
    if (this.magnet && this.bars.length) {
      const b = this._nearestBar(time)
      if (b) {
        time = b.time
        price = this._nearestOHLC(b, price)
      }
    }
    return { time, price }
  }
  private _nearestBar(time: number): DrawBar | null {
    let best: DrawBar | null = null
    let bd = Infinity
    for (const b of this.bars) {
      const d = Math.abs(b.time - time)
      if (d < bd) { bd = d; best = b }
    }
    return best
  }
  private _nearestOHLC(b: DrawBar, price: number): number {
    const cands = [b.open, b.high, b.low, b.close]
    let best = cands[0], bd = Infinity
    for (const c of cands) {
      const d = Math.abs(c - price)
      if (d < bd) { bd = d; best = c }
    }
    return best
  }

  // ── handle geometry (media px) ────────────────────────────────────────────────
  handlePoints(d: Drawing): { x: number; y: number }[] {
    const out: { x: number; y: number }[] = []
    const push = (p: { x: number; y: number } | null) => { if (p) out.push(p) }
    switch (d.tool) {
      case 'trendline': case 'ray': case 'extended': case 'rect': case 'fib': case 'measure':
        push(this.pt(d.points[0])); push(this.pt(d.points[1])); break
      case 'text':
        push(this.pt(d.points[0])); break
      case 'hline': {
        const y = this.yOf(d.points[0].price)
        if (y != null) out.push({ x: this._vw / 2, y }); break
      }
      case 'hray': {
        push(this.pt(d.points[0])); break
      }
      case 'vline': {
        const x = this.xOf(d.points[0].time)
        if (x != null) out.push({ x, y: this._vh / 2 }); break
      }
    }
    return out
  }
  private _handleHitTest(d: Drawing, x: number, y: number): number {
    const pts = this.handlePoints(d)
    // map handle-array index back to a points[] index
    for (let i = 0; i < pts.length; i++) {
      if (Math.hypot(pts[i].x - x, pts[i].y - y) <= HANDLE_TOL) {
        // for two-point tools handle i == point i; for single-anchor tools it's 0
        return d.points.length > 1 ? i : 0
      }
    }
    return -1
  }

  // ── body hit-testing ──────────────────────────────────────────────────────────
  private _hit(d: Drawing, x: number, y: number): boolean {
    const t = HIT_TOL
    switch (d.tool) {
      case 'trendline': case 'measure': {
        const a = this.pt(d.points[0]); const b = this.pt(d.points[1])
        if (d.tool === 'measure' && a && b) {
          if (nearRectBorder(x, y, a.x, a.y, b.x, b.y, t)) return true
        }
        return !!a && !!b && distToSegment(x, y, a.x, a.y, b.x, b.y) <= t
      }
      case 'ray': case 'extended': {
        const a = this.pt(d.points[0]); const b = this.pt(d.points[1])
        if (!a || !b) return false
        let dx = b.x - a.x, dy = b.y - a.y
        const len = Math.hypot(dx, dy) || 1
        dx /= len; dy /= len
        const x2 = a.x + dx * BIG, y2 = a.y + dy * BIG
        const x1 = d.tool === 'extended' ? a.x - dx * BIG : a.x
        const y1 = d.tool === 'extended' ? a.y - dy * BIG : a.y
        return distToSegment(x, y, x1, y1, x2, y2) <= t
      }
      case 'hline': {
        const yy = this.yOf(d.points[0].price)
        return yy != null && Math.abs(y - yy) <= t
      }
      case 'hray': {
        const p = this.pt(d.points[0])
        return !!p && Math.abs(y - p.y) <= t && x >= p.x - t
      }
      case 'vline': {
        const xx = this.xOf(d.points[0].time)
        return xx != null && Math.abs(x - xx) <= t
      }
      case 'rect': {
        const a = this.pt(d.points[0]); const b = this.pt(d.points[1])
        return !!a && !!b && nearRectBorder(x, y, a.x, a.y, b.x, b.y, t)
      }
      case 'fib': {
        const a = this.pt(d.points[0]); const b = this.pt(d.points[1])
        if (!a || !b) return false
        const x1 = Math.min(a.x, b.x), x2 = Math.max(a.x, b.x)
        if (x < x1 - t || x > x2 + t) return false
        const p0 = d.points[0].price, p1 = d.points[1].price
        for (const l of FIB_LEVELS) {
          const yy = this.yOf(p0 + l * (p1 - p0))
          if (yy != null && Math.abs(y - yy) <= t) return true
        }
        return false
      }
      case 'text': {
        const p = this.pt(d.points[0])
        if (!p) return false
        const w = (d.text?.length ?? 0) * d.style.fontSize * 0.6
        return pointInRect(x, y, p.x, p.y, p.x + Math.max(12, w), p.y + d.style.fontSize, t)
      }
    }
    return false
  }
  /** topmost drawing under (x,y), or null. */
  private _bodyHitTest(x: number, y: number): Drawing | null {
    for (let i = this.drawings.length - 1; i >= 0; i--) {
      if (this._hit(this.drawings[i], x, y)) return this.drawings[i]
    }
    return null
  }

  // ── pointer event machine (called by the chart's container listeners) ─────────
  /** @returns whether the chart should disable pan/scale for this gesture. */
  pointerDown(x: number, y: number, _mods?: { shift?: boolean }): { capture: boolean } {
    this.suppressClick = false
    const tool = this.activeTool

    if (tool !== 'cursor') {
      // second click of a two-point tool commits it
      if (this._mode === 'placing' && this.preview) {
        const a = this._anchorAt(x, y)
        if (a) {
          this._commit(this.preview.tool, [this._placePts[0], a], this.preview.text)
          this.suppressClick = true
          return { capture: false }
        }
      }
      this.suppressClick = true
      // one-click tools
      if (tool === 'hline' || tool === 'hray' || tool === 'vline' || tool === 'text') {
        const a = this._anchorAt(x, y)
        if (!a) return { capture: false }
        if (tool === 'text') {
          const txt = typeof window !== 'undefined' ? window.prompt('Text label:') : ''
          if (txt == null || txt.trim() === '') return { capture: false }
          this._commit('text', [a], txt)
        } else {
          this._commit(tool, [a])
        }
        return { capture: false }
      }
      // two-point tools: begin placing (drag OR click-click both supported)
      const a = this._anchorAt(x, y)
      if (!a) return { capture: false }
      this._mode = 'placing'
      this._placePts = [a]
      this._downX = x; this._downY = y; this._moved = false
      this.preview = { id: PREVIEW_ID, tool, points: [a, a], style: { ...this.defaults } }
      this._req()
      return { capture: true }
    }

    // cursor mode → select / drag
    if (this.locked) return { capture: false }
    const sel = this._selected()
    if (sel) {
      const hi = this._handleHitTest(sel, x, y)
      if (hi >= 0) {
        this._mode = 'dragHandle'
        this._handleIdx = hi
        this._dragOrig = sel.points.map((p) => ({ ...p }))
        this._downX = x; this._downY = y; this._moved = false
        this.suppressClick = true
        return { capture: true }
      }
    }
    const hit = this._bodyHitTest(x, y)
    if (hit) {
      const changed = this.selectedId !== hit.id
      this.selectedId = hit.id
      this._mode = 'dragBody'
      this._dragOrig = hit.points.map((p) => ({ ...p }))
      const lg = this._logicalAt(x); const pr = this._priceAt(y)
      this._dragStartLogical = lg ?? 0
      this._dragStartPrice = pr ?? 0
      this._downX = x; this._downY = y; this._moved = false
      this.suppressClick = true
      if (changed) this._emit()
      this._req()
      return { capture: true }
    }
    // empty space → deselect and let the chart handle it (SL/TP arming)
    if (this.selectedId) { this.selectedId = null; this._emit() }
    this._mode = 'idle'
    this._req()
    this.suppressClick = false
    return { capture: false }
  }

  pointerMove(x: number, y: number) {
    if (this._mode === 'placing' && this.preview) {
      const a = this._anchorAt(x, y)
      if (a) { this.preview.points[1] = a; if (Math.hypot(x - this._downX, y - this._downY) > CLICK_SLOP) this._moved = true; this._req() }
      return
    }
    if (this._mode === 'dragHandle') {
      const sel = this._selected()
      if (!sel) return
      const a = this._anchorAt(x, y)
      if (!a) return
      sel.points[this._handleIdx] = a
      this._moved = true
      this._req()
      return
    }
    if (this._mode === 'dragBody') {
      const sel = this._selected()
      if (!sel) return
      const lg = this._logicalAt(x); const pr = this._priceAt(y)
      if (lg == null || pr == null) return
      const dLogical = lg - this._dragStartLogical
      const dPrice = pr - this._dragStartPrice
      for (let i = 0; i < sel.points.length; i++) {
        const o = this._dragOrig[i]
        const ol = timeToLogical(o.time, this._barTimes, this.tfSec)
        const nt = ol != null ? logicalToTime(ol + dLogical, this._barTimes, this.tfSec) : null
        sel.points[i] = { time: nt ?? o.time, price: o.price + dPrice }
      }
      this._moved = true
      this._req()
      return
    }
    // idle hover feedback in cursor mode
    if (this.activeTool === 'cursor' && this._mode === 'idle') {
      let hid: string | null = null
      const sel = this._selected()
      if (sel && this._handleHitTest(sel, x, y) >= 0) hid = sel.id
      else { const h = this._bodyHitTest(x, y); hid = h ? h.id : null }
      if (hid !== this.hoverId) {
        this.hoverId = hid
        this._cursor(hid ? 'pointer' : 'default')
        this._req()
      }
    }
  }

  pointerUp(x: number, y: number) {
    if (this._mode === 'placing') {
      const moved = Math.hypot(x - this._downX, y - this._downY) > CLICK_SLOP
      if (moved && this.preview) {
        const a = this._anchorAt(x, y)
        if (a) this._commit(this.preview.tool, [this._placePts[0], a], this.preview.text)
        this._mode = 'idle'
        this.preview = null
        this._req()
      }
      // else: stay in 'placing' (rubber-band) until the second click
      return
    }
    if (this._mode === 'dragBody' || this._mode === 'dragHandle') {
      this._mode = 'idle'
      this._handleIdx = -1
      if (this._moved) { this._persist(); this._emit() }
      this._req()
    }
  }

  pointerDblClick(x: number, y: number) {
    const hit = this._bodyHitTest(x, y)
    if (hit && hit.tool === 'text') {
      this.selectedId = hit.id
      this._editText(hit)
    }
  }

  /** Esc: cancel an in-progress draw / return to cursor / deselect. @returns handled. */
  cancel(): boolean {
    let handled = false
    if (this._mode === 'placing' || this.preview) {
      this._mode = 'idle'; this.preview = null; handled = true
    }
    if (this.activeTool !== 'cursor') { this.activeTool = 'cursor'; this._cursor('default'); handled = true }
    if (this.selectedId) { this.selectedId = null; handled = true }
    if (handled) { this._emit(); this._req() }
    return handled
  }

  private _selected(): Drawing | null {
    return this.selectedId ? this.drawings.find((d) => d.id === this.selectedId) ?? null : null
  }

  private _commit(tool: DrawKind, points: Anchor[], text?: string) {
    const d: Drawing = { id: uid(), tool, points, style: { ...this.defaults }, text }
    this.drawings.push(d)
    this.preview = null
    this._mode = 'idle'
    this.selectedId = d.id
    if (!this.stayInTool) { this.activeTool = 'cursor'; this._cursor('default') }
    this._persist()
    this._emit()
    this._req()
  }

  private _editText(d: Drawing) {
    if (typeof window === 'undefined') return
    const txt = window.prompt('Edit text:', d.text ?? '')
    if (txt == null) return
    if (txt.trim() === '') {
      this.drawings = this.drawings.filter((x) => x.id !== d.id)
      if (this.selectedId === d.id) this.selectedId = null
    } else {
      d.text = txt
    }
    this._persist()
    this._emit()
    this._req()
  }

  // ── persistence + notification ────────────────────────────────────────────────
  private _persist() {
    saveDrawings(this.instrument, this.drawings)
  }
  private _emit() {
    const s = this.getState()
    this._listeners.forEach((fn) => fn(s))
  }
  private _cursor(c: string) {
    this._cursorSink?.(c)
  }

  // ── external wiring (from the chart) ──────────────────────────────────────────
  setCursorSink(fn: ((c: string) => void) | null) {
    this._cursorSink = fn
  }
  /** Feed the resampled display candles after each data push (reveal / TF / reset). */
  setBars(bars: DrawBar[]) {
    this.bars = bars
    this._barTimes = bars.map((b) => b.time)
    this._req()
  }
  setTf(tfMin: number) {
    this.tfSec = Math.max(1, tfMin) * 60
    this._req()
  }
  setPriceFormat(precision: number, minMove: number) {
    this.pricePrecision = precision
    this.minMove = minMove
  }
  setViewport(w: number, h: number) {
    this._vw = w
    this._vh = h
  }
  setDark(dark: boolean) {
    this.dark = dark
    this._req()
  }
  setInstrument(instrument: string) {
    if (instrument === this.instrument) return
    this._persist()
    this.instrument = instrument
    this.drawings = loadDrawings(instrument)
    this.selectedId = null
    this.preview = null
    this._mode = 'idle'
    this._emit()
    this._req()
  }
  /** read + reset the SL/TP-arm suppression flag (chart's subscribeClick). */
  takeSuppress(): boolean {
    const s = this.suppressClick
    this.suppressClick = false
    return s
  }

  // ── imperative api (builder 2) ────────────────────────────────────────────────
  getState(): DrawingState {
    const sel = this._selected()
    return {
      activeTool: this.activeTool,
      magnet: this.magnet,
      hidden: this.hidden,
      locked: this.locked,
      stayInTool: this.stayInTool,
      count: this.drawings.length,
      selection: sel
        ? {
          id: sel.id, tool: sel.tool, style: { ...sel.style }, text: sel.text,
          supportsFill: sel.tool === 'rect' || sel.tool === 'fib' || sel.tool === 'measure',
          supportsText: sel.tool === 'text',
        }
        : null,
      defaults: { ...this.defaults },
    }
  }
  subscribe(fn: (s: DrawingState) => void): () => void {
    this._listeners.add(fn)
    return () => this._listeners.delete(fn)
  }
  getApi(): DrawingApi {
    return {
      setActiveTool: (t) => {
        if (this.activeTool === t) return
        this.activeTool = t
        this._mode = 'idle'
        this.preview = null
        if (t !== 'cursor') { this.selectedId = null; this._cursor('crosshair') }
        else this._cursor('default')
        this._emit()
        this._req()
      },
      getActiveTool: () => this.activeTool,
      setDefaults: (patch) => { this.defaults = { ...this.defaults, ...patch }; this._emit() },
      setMagnet: (on) => { this.magnet = on; this._emit() },
      setHidden: (on) => { this.hidden = on; this._emit(); this._req() },
      setLocked: (on) => { this.locked = on; if (on) { this._mode = 'idle' } this._emit() },
      setStayInTool: (on) => { this.stayInTool = on; this._emit() },
      restyleSelected: (patch) => {
        const sel = this._selected()
        if (!sel) return
        sel.style = { ...sel.style, ...patch }
        this._persist(); this._emit(); this._req()
      },
      deleteSelected: () => {
        if (!this.selectedId) return
        this.drawings = this.drawings.filter((d) => d.id !== this.selectedId)
        this.selectedId = null
        this._persist(); this._emit(); this._req()
      },
      clearAll: () => {
        this.drawings = []
        this.selectedId = null
        this.preview = null
        this._mode = 'idle'
        this._persist(); this._emit(); this._req()
      },
      editSelectedText: () => {
        const sel = this._selected()
        if (sel && sel.tool === 'text') this._editText(sel)
      },
      selectNone: () => { if (this.selectedId) { this.selectedId = null; this._emit(); this._req() } },
      hasSelection: () => this.selectedId != null,
      getState: () => this.getState(),
      subscribe: (fn) => this.subscribe(fn),
    }
  }
  hasSelection(): boolean {
    return this.selectedId != null
  }
  deleteSelected() {
    this.getApi().deleteSelected()
  }
  /** persist on teardown (chart.remove disposes the primitive itself). */
  destroy() {
    this._persist()
    this._listeners.clear()
    this._cursorSink = null
    this._requestUpdate = null
  }
}
