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

/** All palette tools. 'cursor' = select/drag (no drawing).
 *  'long' / 'short' are the TradingView-style risk/reward POSITION tools. */
export type DrawTool =
  | 'cursor' | 'trendline' | 'ray' | 'extended' | 'hline' | 'hray'
  | 'vline' | 'rect' | 'fib' | 'text' | 'measure' | 'long' | 'short'

/** Tools that actually produce a persisted drawing (everything but the cursor). */
export type DrawKind = Exclude<DrawTool, 'cursor'>

/** True for the two risk/reward position tools (entry + stop zone + target zone). */
export function isPositionTool(tool: DrawTool): tool is 'long' | 'short' {
  return tool === 'long' || tool === 'short'
}

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

/** One fib retracement level. `value` is the ratio (0..1 and beyond, e.g. 1.272,
 *  1.618). `visible` false hides it without discarding it (default visible).
 *  `color` overrides the drawing's line color for that one level (default: the
 *  drawing's style.color). Carried PER-drawing so each fib keeps its own levels. */
export type FibLevel = { value: number; visible?: boolean; color?: string }

export type Drawing = {
  id: string
  tool: DrawKind
  /** hline/hray/vline/text: 1 anchor. long/short: 3 (entry / stop / target).
   *  Everything else: 2. */
  points: Anchor[]
  style: DrawStyle
  /** only for the text tool. */
  text?: string
  /** fib only: custom levels. Undefined ⇒ the default 7 (FIB_LEVELS). Editable
   *  per drawing (add / remove / retype value / toggle / per-level color). */
  levels?: FibLevel[]
}

/** A resampled display candle (what the engine needs for magnet + bar counts). */
export type DrawBar = { time: number; open: number; high: number; low: number; close: number }

/** Info about the current selection, surfaced to the page's style bar. */
export type SelectionInfo = {
  id: string
  tool: DrawKind
  style: DrawStyle
  text?: string
  /** rect / fib / measure / long / short expose a fill-opacity control. */
  supportsFill: boolean
  /** the text tool exposes a font-size + edit-text control. */
  supportsText: boolean
  /** the fib tool exposes an "Edit levels" affordance. */
  supportsFibLevels: boolean
  /** effective fib levels of the selection (custom, else the default 7); only
   *  present when the selection is a fib — populate the "Edit levels" editor. */
  fibLevels?: FibLevel[]
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
  /** current DEFAULT fib levels applied to newly drawn fibs (resolved: the
   *  default 7 until setFibDefaults changes them). */
  fibDefaults: FibLevel[]
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
  // ── GOAL 4: editable fibonacci levels ──────────────────────────────────────
  /** Replace the SELECTED fib's levels (no-op unless a fib is selected).
   *  Pass a fresh array; the engine clones + persists it. */
  setSelectedFibLevels(levels: FibLevel[]): void
  /** Clear the selected fib back to the DEFAULT levels (no-op unless fib). */
  resetSelectedFibLevels(): void
  /** Effective levels of the selected fib (custom, else the default 7), or null
   *  when the selection isn't a fib — use to seed the editor. */
  getSelectedFibLevels(): FibLevel[] | null
  /** Set the DEFAULT levels applied to newly drawn fibs (does not touch existing
   *  drawings). Pass a copy of the default 7 to reset. */
  setFibDefaults(levels: FibLevel[]): void
  /** The current default fib levels for new fibs. */
  getFibDefaults(): FibLevel[]
}

export const DEFAULT_DRAW_STYLE: DrawStyle = {
  color: '#2962ff', width: 2, dash: 'solid', fillOpacity: 0.12, fontSize: 14,
}

export const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1]

/** A fresh default fib-level list (all visible, no per-level color). */
export function defaultFibLevels(): FibLevel[] {
  return FIB_LEVELS.map((value) => ({ value, visible: true }))
}

/** Deep-copy a level list (arrays are handed across the api boundary). */
function cloneFibLevels(levels: FibLevel[]): FibLevel[] {
  return levels.map((l) => ({ value: l.value, visible: l.visible !== false, ...(l.color ? { color: l.color } : {}) }))
}

/** Coerce arbitrary parsed input into a clean FibLevel[] (tolerant loader).
 *  Accepts the rich {value,visible,color} shape AND a legacy number[]. Returns
 *  undefined when there is nothing usable, so the drawing falls back to default. */
function normalizeFibLevels(v: unknown): FibLevel[] | undefined {
  if (!Array.isArray(v)) return undefined
  const out: FibLevel[] = []
  for (const raw of v) {
    if (typeof raw === 'number') {
      if (Number.isFinite(raw)) out.push({ value: raw, visible: true })
      continue
    }
    if (raw && typeof raw === 'object') {
      const o = raw as Record<string, unknown>
      const value = typeof o.value === 'number' ? o.value : Number(o.value)
      if (!Number.isFinite(value)) continue
      const level: FibLevel = { value, visible: o.visible !== false }
      if (typeof o.color === 'string') level.color = o.color
      out.push(level)
    }
  }
  return out.length ? out : undefined
}

/** The levels a fib should render with: its own custom set, else the default 7. */
function fibLevelsOf(d: Drawing): FibLevel[] {
  return d.levels && d.levels.length ? d.levels : defaultFibLevels()
}

// ── position tool (long/short) geometry — pure helpers ────────────────────────
// A position drawing stores THREE anchors: points[0] = (leftTime, entryPrice),
// points[1] = (rightTime, stopPrice), points[2] = (rightTime, targetPrice). The
// box spans [points[0].time, points[1].time]; the three prices are the levels.
const POS_MIN_BARS = 10 // default box width (bars) when placed with ~zero width

/** From an entry + a dragged "other" price, derive stop/target for a direction:
 *  the drag distance becomes the risk; the reward defaults to 2R (TradingView's
 *  Long/Short Position tool default). Stop always sits on the losing side. */
function posLevels(tool: 'long' | 'short', entry: number, other: number): { entry: number; stop: number; target: number } {
  const raw = Math.abs(other - entry)
  const dist = raw > 0 ? raw : (entry !== 0 ? Math.abs(entry) * 0.002 : 1)
  const sign = tool === 'long' ? 1 : -1
  return { entry, stop: entry - sign * dist, target: entry + sign * 2 * dist }
}

/** Turn the two placement anchors into the 3-anchor position representation. */
function buildPositionPoints(tool: 'long' | 'short', p0: Anchor, p1: Anchor, tfSec: number): Anchor[] {
  const tLeft = Math.min(p0.time, p1.time)
  let tRight = Math.max(p0.time, p1.time)
  if (tRight - tLeft < tfSec) tRight = tLeft + POS_MIN_BARS * tfSec
  const { entry, stop, target } = posLevels(tool, p0.price, p1.price)
  return [
    { time: tLeft, price: entry },
    { time: tRight, price: stop },
    { time: tRight, price: target },
  ]
}

/** Resolve the display geometry of a position drawing (committed 3-anchor form,
 *  or an in-progress 2-anchor preview). null if it has no usable anchors. */
function positionGeom(d: Drawing): { tLeft: number; tRight: number; entry: number; stop: number; target: number } | null {
  const pts = d.points
  if (pts.length >= 3) {
    return { tLeft: pts[0].time, tRight: pts[1].time, entry: pts[0].price, stop: pts[1].price, target: pts[2].price }
  }
  if (pts.length === 2 && (d.tool === 'long' || d.tool === 'short')) {
    const tLeft = Math.min(pts[0].time, pts[1].time)
    const tRight = Math.max(pts[0].time, pts[1].time)
    const { entry, stop, target } = posLevels(d.tool, pts[0].price, pts[1].price)
    return { tLeft, tRight, entry, stop, target }
  }
  return null
}

// Semantic colors for the position tool zones (fixed, like TradingView).
const POS_STOP = '#ef5350'   // red — risk zone
const POS_TARGET = '#26a69a' // green — reward zone
const POS_ENTRY = '#787b86'  // neutral grey — entry line

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
        // Preserve custom fib levels across reloads / instrument switches (the
        // loader would otherwise silently drop this per-drawing field).
        levels: dd.tool === 'fib' ? normalizeFibLevels(dd.levels) : undefined,
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

/** Compact ratio label: 0.500→"0.5", 1.000→"1", 1.272→"1.272" (≤3 decimals). */
function fmtRatio(v: number): string {
  return v.toFixed(3).replace(/\.?0+$/, '')
}

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
          // Custom (or default) levels, visible only, sorted so the bands sit
          // between adjacent ratios regardless of the edit order.
          const levels = fibLevelsOf(d).filter((l) => l.visible !== false).slice().sort((m, n) => m.value - n.value)
          const ys = levels.map((l) => {
            const yy = P.yOf(p0 + l.value * (p1 - p0))
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
          }
          for (let i = 0; i < ys.length; i++) {
            const yy = ys[i]
            if (yy == null) continue
            ctx.strokeStyle = levels[i].color ?? st.color // per-level color override
            this._seg(ctx, x1, yy, x2, yy)
          }
        }
        break
      }
      case 'long':
      case 'short': {
        // TradingView-style risk/reward box: entry line + red stop zone + green
        // target zone, spanning the drawing's time extent. Zones use the semantic
        // colors (not style.color); style.color paints the entry line + edges,
        // style.fillOpacity scales the zone translucency, width/dash the lines.
        const g = positionGeom(d)
        if (g) {
          let x0 = P.xOf(g.tLeft); let x1 = P.xOf(g.tRight)
          if (x0 != null && x1 != null) {
            if (x1 < x0) { const t = x0; x0 = x1; x1 = t }
            const Wm = W / hr
            if (x1 - x0 < 2) x1 = Wm // keep a near-zero-width preview visible
            const ye = P.yOf(g.entry), ys = P.yOf(g.stop), yt = P.yOf(g.target)
            const zoneAlpha = Math.max(0.05, Math.min(0.5, st.fillOpacity + 0.03))
            ctx.setLineDash([])
            if (ye != null && ys != null) {
              ctx.fillStyle = withAlpha(POS_STOP, zoneAlpha)
              ctx.fillRect(X(x0), Math.min(Y(ye), Y(ys)), X(x1) - X(x0), Math.abs(Y(ys) - Y(ye)))
            }
            if (ye != null && yt != null) {
              ctx.fillStyle = withAlpha(POS_TARGET, zoneAlpha)
              ctx.fillRect(X(x0), Math.min(Y(ye), Y(yt)), X(x1) - X(x0), Math.abs(Y(yt) - Y(ye)))
            }
            // vertical edges spanning the whole box (faint)
            const yTop = Math.min(ye ?? Infinity, ys ?? Infinity, yt ?? Infinity)
            const yBot = Math.max(ye ?? -Infinity, ys ?? -Infinity, yt ?? -Infinity)
            if (Number.isFinite(yTop) && Number.isFinite(yBot)) {
              ctx.strokeStyle = withAlpha(st.color, 0.5)
              ctx.lineWidth = Math.max(1, vr)
              this._seg(ctx, X(x0), Y(yTop), X(x0), Y(yBot))
              this._seg(ctx, X(x1), Y(yTop), X(x1), Y(yBot))
            }
            ctx.lineWidth = lw
            this._dash(ctx, st.dash, vr)
            if (ye != null) { ctx.strokeStyle = st.color; this._seg(ctx, X(x0), Y(ye), X(x1), Y(ye)) }
            if (ys != null) { ctx.strokeStyle = POS_STOP; this._seg(ctx, X(x0), Y(ys), X(x1), Y(ys)) }
            if (yt != null) { ctx.strokeStyle = POS_TARGET; this._seg(ctx, X(x0), Y(yt), X(x1), Y(yt)) }
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
      for (const l of fibLevelsOf(d)) {
        if (l.visible === false) continue
        const price = p0 + l.value * (p1 - p0)
        const yy = P.yOf(price)
        if (yy == null) continue
        ctx.fillStyle = l.color ?? d.style.color
        ctx.fillText(`${fmtRatio(l.value)}  ${P.fmtPrice(price)}`, rightX + 4, yy)
      }
      ctx.restore()
      return
    }
    if (d.tool === 'long' || d.tool === 'short') {
      const g = positionGeom(d)
      if (!g) return
      let x0 = P.xOf(g.tLeft); let x1 = P.xOf(g.tRight)
      if (x0 == null || x1 == null) return
      if (x1 < x0) { const t = x0; x0 = x1; x1 = t }
      const ye = P.yOf(g.entry), ys = P.yOf(g.stop), yt = P.yOf(g.target)
      const risk = Math.abs(g.entry - g.stop)
      const reward = Math.abs(g.target - g.entry)
      const rr = risk > 0 ? reward / risk : 0
      const pct = (v: number) => (g.entry !== 0 ? (v / g.entry) * 100 : 0)
      const cx = x0 + 6
      ctx.save()
      ctx.font = `11px ${FONT}`
      ctx.textBaseline = 'middle'
      const chip = (text: string, x: number, y: number, bg: string) => {
        const w = ctx.measureText(text).width
        ctx.fillStyle = withAlpha(bg, 0.92)
        ctx.fillRect(x - 3, y - 8, w + 6, 16)
        ctx.fillStyle = '#ffffff'
        ctx.fillText(text, x, y)
      }
      if (ye != null && yt != null) {
        chip(`Target ${P.fmtPrice(g.target)}  +${P.fmtPrice(reward)} (${pct(reward).toFixed(2)}%)`, cx, (ye + yt) / 2, POS_TARGET)
      }
      if (ye != null && ys != null) {
        chip(`Stop ${P.fmtPrice(g.stop)}  -${P.fmtPrice(risk)} (${pct(risk).toFixed(2)}%)`, cx, (ye + ys) / 2, POS_STOP)
      }
      if (ye != null) {
        chip(`${d.tool === 'long' ? 'LONG' : 'SHORT'}  Entry ${P.fmtPrice(g.entry)}  R:R ${rr.toFixed(2)}`, cx, ye, POS_ENTRY)
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
  /** default fib levels applied to NEW fibs; undefined ⇒ the standard 7. */
  fibDefaults: FibLevel[] | undefined = undefined
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
      } else if (d.tool === 'long' || d.tool === 'short') {
        const g = positionGeom(d)
        if (!g) continue
        out.push(new DrawingsAxisView(this, g.entry, POS_ENTRY))
        out.push(new DrawingsAxisView(this, g.stop, POS_STOP))
        out.push(new DrawingsAxisView(this, g.target, POS_TARGET))
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
      case 'long': case 'short': {
        // entry / stop / target handles at the box's horizontal center. Hit
        // mapping lives in _handleHitTest (level-index based), so skipping an
        // off-scale handle here only affects which squares get drawn.
        const g = positionGeom(d)
        if (g) {
          const x0 = this.xOf(g.tLeft); const x1 = this.xOf(g.tRight)
          if (x0 != null && x1 != null) {
            const mx = (x0 + x1) / 2
            for (const price of [g.entry, g.stop, g.target]) {
              const yy = this.yOf(price)
              if (yy != null) out.push({ x: mx, y: yy })
            }
          }
        }
        break
      }
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
    // Position tools: the three level handles map to points[0/1/2] (entry/stop/
    // target) by LEVEL, independent of how handlePoints packed the drawn squares.
    if (d.tool === 'long' || d.tool === 'short') {
      const g = positionGeom(d)
      if (!g) return -1
      const x0 = this.xOf(g.tLeft); const x1 = this.xOf(g.tRight)
      if (x0 == null || x1 == null) return -1
      const mx = (x0 + x1) / 2
      const levels = [g.entry, g.stop, g.target]
      for (let i = 0; i < 3; i++) {
        const yy = this.yOf(levels[i])
        if (yy != null && Math.hypot(mx - x, yy - y) <= HANDLE_TOL) return i
      }
      return -1
    }
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
        for (const l of fibLevelsOf(d)) {
          if (l.visible === false) continue
          const yy = this.yOf(p0 + l.value * (p1 - p0))
          if (yy != null && Math.abs(y - yy) <= t) return true
        }
        return false
      }
      case 'long':
      case 'short': {
        // whole risk/reward box grabbable (easy select + body-drag)
        const g = positionGeom(d)
        if (!g) return false
        let x0 = this.xOf(g.tLeft); let x1 = this.xOf(g.tRight)
        if (x0 == null || x1 == null) return false
        if (x1 < x0) { const tt = x0; x0 = x1; x1 = tt }
        const ye = this.yOf(g.entry), ys = this.yOf(g.stop), yt = this.yOf(g.target)
        if (ye == null || ys == null || yt == null) return false
        const top = Math.min(ye, ys, yt), bot = Math.max(ye, ys, yt)
        return pointInRect(x, y, x0, top, x1, bot, t)
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
    if (this.hidden) return null // hidden drawings are non-interactive
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
      if (sel.tool === 'long' || sel.tool === 'short') {
        // level handles move the PRICE only; the box's time extent is unchanged.
        sel.points[this._handleIdx] = { time: sel.points[this._handleIdx].time, price: a.price }
      } else {
        sel.points[this._handleIdx] = a
      }
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
    // Position tools carry THREE anchors (entry/stop/target); the placement gives
    // two, so derive the risk/reward box here.
    let pts = points
    if ((tool === 'long' || tool === 'short') && points.length === 2) {
      pts = buildPositionPoints(tool, points[0], points[1], this.tfSec)
    }
    // New fibs inherit the current default levels (if the user customized them).
    const levels = tool === 'fib' && this.fibDefaults ? cloneFibLevels(this.fibDefaults) : undefined
    const d: Drawing = { id: uid(), tool, points: pts, style: { ...this.defaults }, text, levels }
    if (this.hidden) this.hidden = false // a fresh draw un-hides so it is never an invisible commit
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
          supportsFill: sel.tool === 'rect' || sel.tool === 'fib' || sel.tool === 'measure'
            || sel.tool === 'long' || sel.tool === 'short',
          supportsText: sel.tool === 'text',
          supportsFibLevels: sel.tool === 'fib',
          fibLevels: sel.tool === 'fib' ? cloneFibLevels(fibLevelsOf(sel)) : undefined,
        }
        : null,
      defaults: { ...this.defaults },
      fibDefaults: cloneFibLevels(this.fibDefaults ?? defaultFibLevels()),
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
      setSelectedFibLevels: (levels) => {
        const sel = this._selected()
        if (!sel || sel.tool !== 'fib') return
        sel.levels = cloneFibLevels(levels)
        this._persist(); this._emit(); this._req()
      },
      resetSelectedFibLevels: () => {
        const sel = this._selected()
        if (!sel || sel.tool !== 'fib') return
        sel.levels = undefined
        this._persist(); this._emit(); this._req()
      },
      getSelectedFibLevels: () => {
        const sel = this._selected()
        return sel && sel.tool === 'fib' ? cloneFibLevels(fibLevelsOf(sel)) : null
      },
      setFibDefaults: (levels) => {
        this.fibDefaults = cloneFibLevels(levels)
        this._emit()
      },
      getFibDefaults: () => cloneFibLevels(this.fibDefaults ?? defaultFibLevels()),
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
