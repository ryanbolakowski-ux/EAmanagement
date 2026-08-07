// ─────────────────────────────────────────────────────────────────────────────
// DrawingToolbar.tsx — FX-Replay / TradingView-style drawing PALETTE + style bar.
// Builder 2: the UI layer that drives the drawing engine (builder 1) through its
// imperative DrawingApi + reactive DrawingState (see ../lib/replayDrawings).
//
// The engine owns ALL interaction (placement/drag/select/keyboard) inside
// TVReplayChart; this component only renders controls and calls the api:
//   • a vertical tool palette docked on the chart's left edge (collapses to a
//     top wrapping sheet on mobile — 375px stays usable, never scrolls sideways),
//   • magnet / pin / hide-all / lock toggles + delete-selected + clear-all,
//   • a style bar that edits either the CURRENT SELECTION (when one exists) or
//     the DEFAULT style applied to new drawings.
//
// BLIND MODE: nothing here renders a date. Tool labels + style values only.
// ─────────────────────────────────────────────────────────────────────────────
import { useEffect, useRef, useState } from 'react'
import {
  MousePointer2, TrendingUp, ArrowUpRight, Slash, Minus, MoveRight,
  MoveVertical, Square, AlignJustify, Type, Ruler,
  Magnet, Lock, LockOpen, Eye, EyeOff, Pin, PinOff, Trash2, Eraser,
  Palette, Pencil, X, ArrowBigUp, ArrowBigDown, Plus, RotateCcw,
} from 'lucide-react'
import type {
  DrawTool, DrawStyle, LineDash, DrawingState, DrawingApi, FibLevel,
} from '../lib/replayDrawings'

type IconType = typeof MousePointer2

// Tool → icon + tooltip. Order matches FX Replay's core set.
const TOOLS: { tool: DrawTool; icon: IconType; label: string }[] = [
  { tool: 'cursor', icon: MousePointer2, label: 'Cursor — select & move' },
  { tool: 'trendline', icon: TrendingUp, label: 'Trend line' },
  { tool: 'ray', icon: ArrowUpRight, label: 'Ray — extends past the 2nd point' },
  { tool: 'extended', icon: Slash, label: 'Extended line — infinite both ways' },
  { tool: 'hline', icon: Minus, label: 'Horizontal line' },
  { tool: 'hray', icon: MoveRight, label: 'Horizontal ray' },
  { tool: 'vline', icon: MoveVertical, label: 'Vertical line' },
  { tool: 'rect', icon: Square, label: 'Rectangle / zone' },
  { tool: 'fib', icon: AlignJustify, label: 'Fib retracement' },
  { tool: 'long', icon: ArrowBigUp, label: 'Long position — entry / stop / target with R:R' },
  { tool: 'short', icon: ArrowBigDown, label: 'Short position — entry / stop / target with R:R' },
  { tool: 'text', icon: Type, label: 'Text label' },
  { tool: 'measure', icon: Ruler, label: 'Measure — Δprice / Δ% / bars' },
]
// cursor sits alone; the rest are the drawing tools (divider between).
const DRAW_TOOLS = TOOLS.slice(1)

// Quick colour swatches for the style bar.
const SWATCHES = [
  '#2962ff', '#f23645', '#089981', '#ff9800',
  '#ab47bc', '#00bcd4', '#ffeb3b', '#787b86',
]
const WIDTHS = [1, 2, 3, 4]
const DASHES: LineDash[] = ['solid', 'dashed', 'dotted']

const sameColor = (a: string, b: string) => a.toLowerCase() === b.toLowerCase()
const dashArray = (d: LineDash) => (d === 'dashed' ? '5 3' : d === 'dotted' ? '1.5 3' : undefined)

// ── small building blocks ─────────────────────────────────────────────────────

function ToolBtn({
  active, disabled, title, onClick, children,
}: { active?: boolean; disabled?: boolean; title: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <button type="button" title={title} aria-label={title} aria-pressed={!!active} disabled={disabled}
      onClick={onClick}
      className={`relative inline-flex items-center justify-center h-8 w-8 rounded-md transition-colors shrink-0
        disabled:opacity-30 disabled:pointer-events-none
        ${active
          ? 'bg-violet-600 text-white shadow-sm'
          : 'text-slate-600 dark:text-slate-300 hover:bg-slate-200/80 dark:hover:bg-slate-700/70'}`}>
      {children}
    </button>
  )
}

// A divider that is a horizontal rule in the vertical (desktop) palette and a
// vertical rule in the horizontal (mobile) sheet.
function PaletteDivider() {
  return <div className="shrink-0 bg-slate-200 dark:bg-slate-700 w-px h-6 my-0 sm:w-full sm:h-px sm:my-0.5"/>
}

// ── the shared style controls (used for both selection + defaults) ────────────

function StyleControls({
  style, onChange, showFill, showText,
}: { style: DrawStyle; onChange: (patch: Partial<DrawStyle>) => void; showFill: boolean; showText: boolean }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
      {/* colour swatches + custom picker */}
      <div className="flex items-center gap-1">
        {SWATCHES.map((c) => (
          <button key={c} type="button" title={c} onClick={() => onChange({ color: c })}
            className={`h-5 w-5 rounded-full border transition-transform hover:scale-110
              ${sameColor(style.color, c) ? 'ring-2 ring-offset-1 ring-violet-500 ring-offset-white dark:ring-offset-slate-900 border-transparent' : 'border-slate-300 dark:border-slate-600'}`}
            style={{ background: c }}/>
        ))}
        <label className="relative h-5 w-5 rounded-full overflow-hidden border border-slate-300 dark:border-slate-600 cursor-pointer" title="Custom colour"
          style={{ background: style.color }}>
          <input type="color" value={style.color} onChange={(e) => onChange({ color: e.target.value })}
            className="absolute inset-0 opacity-0 cursor-pointer"/>
        </label>
      </div>

      <div className="h-5 w-px bg-slate-200 dark:bg-slate-700"/>

      {/* line width */}
      <div className="flex items-center gap-0.5" title="Line width">
        {WIDTHS.map((w) => (
          <button key={w} type="button" onClick={() => onChange({ width: w })}
            className={`inline-flex items-center justify-center h-6 w-6 rounded transition-colors
              ${style.width === w ? 'bg-violet-600 text-white' : 'text-slate-500 dark:text-slate-300 hover:bg-slate-200/80 dark:hover:bg-slate-700/70'}`}>
            <span className="block w-3.5 rounded-full bg-current" style={{ height: w }}/>
          </button>
        ))}
      </div>

      <div className="h-5 w-px bg-slate-200 dark:bg-slate-700"/>

      {/* line style */}
      <div className="flex items-center gap-0.5" title="Line style">
        {DASHES.map((d) => (
          <button key={d} type="button" onClick={() => onChange({ dash: d })} title={d}
            className={`inline-flex items-center justify-center h-6 w-8 rounded transition-colors
              ${style.dash === d ? 'bg-violet-600 text-white' : 'text-slate-500 dark:text-slate-300 hover:bg-slate-200/80 dark:hover:bg-slate-700/70'}`}>
            <svg width="20" height="8" viewBox="0 0 20 8" aria-hidden>
              <line x1="1.5" y1="4" x2="18.5" y2="4" stroke="currentColor" strokeWidth="1.75"
                strokeDasharray={dashArray(d)} strokeLinecap="round"/>
            </svg>
          </button>
        ))}
      </div>

      {/* fill opacity — rect / fib / measure */}
      {showFill && (
        <>
          <div className="h-5 w-px bg-slate-200 dark:bg-slate-700"/>
          <label className="flex items-center gap-1.5 text-[11px] font-semibold text-slate-500 dark:text-slate-400" title="Fill opacity">
            <span className="uppercase tracking-wide">Fill</span>
            <input type="range" min={0} max={100} value={Math.round(style.fillOpacity * 100)}
              onChange={(e) => onChange({ fillOpacity: Number(e.target.value) / 100 })}
              className="w-16 accent-violet-600"/>
            <span className="tabular-nums w-8 text-right">{Math.round(style.fillOpacity * 100)}%</span>
          </label>
        </>
      )}

      {/* font size — text */}
      {showText && (
        <>
          <div className="h-5 w-px bg-slate-200 dark:bg-slate-700"/>
          <label className="flex items-center gap-1.5 text-[11px] font-semibold text-slate-500 dark:text-slate-400" title="Font size">
            <Type size={13}/>
            <input type="range" min={8} max={48} value={style.fontSize}
              onChange={(e) => onChange({ fontSize: Number(e.target.value) })}
              className="w-16 accent-violet-600"/>
            <span className="tabular-nums w-6 text-right">{style.fontSize}</span>
          </label>
        </>
      )}
    </div>
  )
}

// ── fib "Edit levels" editor (GOAL 4) ─────────────────────────────────────────
// Small panel below the style bar, shown when the selection is a fib. Rows are
// kept as LOCAL strings so mid-typing values ("0.", "") never turn into NaN;
// only rows that parse to a finite number are pushed to the engine, and an
// all-invalid set is simply not applied (the drawing keeps its last levels).
// Re-seeded only when the SELECTED drawing changes, so engine state pushes
// (which echo our own edits back) can't clobber in-progress typing.

type FibRow = { value: string; visible: boolean; color?: string }

const rowsFrom = (levels: FibLevel[]): FibRow[] =>
  levels.map((l) => ({ value: String(l.value), visible: l.visible !== false, color: l.color }))
const levelsFrom = (rows: FibRow[]): FibLevel[] =>
  rows
    .filter((r) => r.value.trim() !== '' && Number.isFinite(Number(r.value)))
    .map((r) => ({ value: Number(r.value), visible: r.visible, ...(r.color ? { color: r.color } : {}) }))

function FibLevelsEditor({
  api, selectionId, seed, onClose,
}: { api: DrawingApi | null; selectionId: string; seed: FibLevel[]; onClose: () => void }) {
  const [rows, setRows] = useState<FibRow[]>(() => rowsFrom(seed))
  const seededForRef = useRef(selectionId)

  // Re-seed only when a DIFFERENT fib gets selected (see header comment).
  useEffect(() => {
    if (seededForRef.current === selectionId) return
    seededForRef.current = selectionId
    setRows(rowsFrom(seed))
  }, [selectionId, seed])

  // Push a cleaned level list to the engine; empty/duplicate/garbage rows are
  // tolerated locally and simply skipped (never applied as NaN).
  const apply = (next: FibRow[]) => {
    const levels = levelsFrom(next)
    if (levels.length > 0) api?.setSelectedFibLevels(levels)
  }

  const setValue = (i: number, v: string) =>
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, value: v } : r)))
  const commitValues = () => apply(rows)
  // NOTE: compute `next` OUTSIDE setRows — updaters must stay pure (StrictMode
  // double-invokes them, which would double-write the engine via apply()).
  const toggleVisible = (i: number) => {
    const next = rows.map((r, j) => (j === i ? { ...r, visible: !r.visible } : r))
    setRows(next)
    apply(next)
  }
  const removeRow = (i: number) => {
    const next = rows.filter((_, j) => j !== i)
    setRows(next)
    apply(next)
  }
  const addRow = () => setRows((rs) => [...rs, { value: '', visible: true }])
  const resetToDefaults = () => {
    api?.resetSelectedFibLevels()
    const fresh = api?.getSelectedFibLevels()
    if (fresh) setRows(rowsFrom(fresh))
  }
  const useAsDefault = () => {
    const levels = levelsFrom(rows)
    if (levels.length > 0) api?.setFibDefaults(levels)
  }

  const canDefault = levelsFrom(rows).length > 0

  return (
    <div className="pointer-events-auto absolute z-20 top-24 sm:top-14 left-2 sm:left-14 w-[240px]
      rounded-xl border border-slate-200/80 dark:border-slate-700/80
      bg-white/95 dark:bg-slate-900/95 backdrop-blur shadow-lg p-2.5">
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-[10px] uppercase tracking-wider font-bold text-violet-500 dark:text-violet-300">Fib levels</span>
        <button type="button" title="Close" onClick={onClose}
          className="inline-flex items-center justify-center h-5 w-5 rounded text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800">
          <X size={13}/>
        </button>
      </div>
      <div className="max-h-52 overflow-y-auto space-y-1 pr-0.5">
        {rows.map((r, i) => (
          <div key={i} className="flex items-center gap-1.5">
            <input type="checkbox" checked={r.visible} onChange={() => toggleVisible(i)}
              title={r.visible ? 'Hide level' : 'Show level'}
              className="h-3.5 w-3.5 accent-violet-600 shrink-0"/>
            <input type="number" step={0.001} value={r.value} placeholder="ratio"
              onChange={(e) => setValue(i, e.target.value)}
              onBlur={commitValues}
              onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
              className="flex-1 min-w-0 rounded-md border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800
                text-slate-800 dark:text-slate-100 text-xs px-1.5 py-1 tabular-nums"/>
            <button type="button" title="Remove level" onClick={() => removeRow(i)}
              className="inline-flex items-center justify-center h-5 w-5 rounded text-slate-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-950/40 shrink-0">
              <X size={12}/>
            </button>
          </div>
        ))}
        {rows.length === 0 && (
          <div className="text-[11px] text-slate-400 dark:text-slate-500 py-1">No levels — add one or reset.</div>
        )}
      </div>
      <div className="flex items-center gap-1 mt-2 pt-2 border-t border-slate-200 dark:border-slate-700">
        <button type="button" onClick={addRow}
          className="inline-flex items-center gap-1 text-[11px] font-bold text-slate-600 dark:text-slate-300 hover:text-violet-600 dark:hover:text-violet-300 px-1 py-0.5">
          <Plus size={12}/> Add
        </button>
        <button type="button" onClick={resetToDefaults} title="Back to the default levels"
          className="inline-flex items-center gap-1 text-[11px] font-bold text-slate-600 dark:text-slate-300 hover:text-violet-600 dark:hover:text-violet-300 px-1 py-0.5">
          <RotateCcw size={12}/> Reset
        </button>
        <button type="button" onClick={useAsDefault} disabled={!canDefault} title="Use these levels for every NEW fib"
          className="ml-auto text-[11px] font-bold text-violet-600 dark:text-violet-300 hover:text-violet-500 disabled:opacity-40 px-1 py-0.5">
          Set default
        </button>
      </div>
    </div>
  )
}

// ── the palette + style bar ───────────────────────────────────────────────────

type Props = {
  /** Latest engine state (null before the chart mounts). */
  state: DrawingState | null
  /** Imperative controller (null before mount / after unmount). */
  api: DrawingApi | null
  /** Page arms the tool (also clears any SL/TP place-on-chart arm). */
  onSelectTool: (tool: DrawTool) => void
  /** Page applies + persists the new-drawing default style. */
  onSetDefaults: (patch: Partial<DrawStyle>) => void
}

export default function DrawingToolbar({ state, api, onSelectTool, onSetDefaults }: Props) {
  const [mobileOpen, setMobileOpen] = useState(false)
  const [styleOpen, setStyleOpen] = useState(false)
  const [fibOpen, setFibOpen] = useState(false)

  const activeTool = state?.activeTool ?? 'cursor'
  const magnet = !!state?.magnet
  const locked = !!state?.locked
  const hidden = !!state?.hidden
  const stayInTool = !!state?.stayInTool
  const count = state?.count ?? 0
  const selection = state?.selection ?? null
  const defaults = state?.defaults ?? null

  // A live selection takes over the style bar; hide the manual defaults popover.
  useEffect(() => { if (selection) setStyleOpen(false) }, [selection])
  // The fib-levels editor only makes sense while a fib is selected.
  useEffect(() => { if (!selection?.supportsFibLevels) setFibOpen(false) }, [selection?.supportsFibLevels])

  if (!state) return null

  const clearAll = () => {
    if (count === 0) return
    if (window.confirm(`Delete all ${count} drawing${count === 1 ? '' : 's'} on this chart?`)) api?.clearAll()
  }

  // What the top style bar shows: the selection's own style, or the defaults.
  const showSelectionBar = !!selection
  const showDefaultsBar = !selection && styleOpen && !!defaults

  return (
    // pointer-events-none wrapper so the transparent gaps never eat chart drags;
    // each interactive box re-enables pointer events.
    <div className="absolute inset-0 z-30 pointer-events-none">
      {/* mobile collapse toggle (hidden on sm+) */}
      <button type="button" onClick={() => setMobileOpen((o) => !o)}
        title={mobileOpen ? 'Hide drawing tools' : 'Drawing tools'}
        className="sm:hidden pointer-events-auto absolute z-30 top-2 left-2 inline-flex items-center justify-center h-8 w-8 rounded-lg
          bg-white/90 dark:bg-slate-900/90 backdrop-blur border border-slate-200/80 dark:border-slate-700/80 shadow-lg
          text-slate-600 dark:text-slate-300">
        {mobileOpen ? <X size={16}/> : <Pencil size={15}/>}
      </button>

      {/* PALETTE — vertical strip on the left (desktop); wrapping top sheet (mobile) */}
      <div className={`pointer-events-auto absolute z-10
        top-12 left-2 right-2 sm:top-2 sm:right-auto
        ${mobileOpen ? 'flex' : 'hidden'} sm:flex
        flex-row flex-wrap items-center sm:flex-col
        gap-0.5 p-1 rounded-xl border border-slate-200/80 dark:border-slate-700/80
        bg-white/90 dark:bg-slate-900/90 backdrop-blur shadow-lg`}>
        {/* cursor */}
        <ToolBtn active={activeTool === 'cursor'} title={TOOLS[0].label}
          onClick={() => onSelectTool('cursor')}>
          <MousePointer2 size={16}/>
        </ToolBtn>
        <PaletteDivider/>
        {/* drawing tools */}
        {DRAW_TOOLS.map(({ tool, icon: Icon, label }) => (
          <ToolBtn key={tool} active={activeTool === tool} title={label}
            onClick={() => onSelectTool(tool)}>
            <Icon size={16}/>
          </ToolBtn>
        ))}
        <PaletteDivider/>
        {/* style (defaults) popover trigger */}
        <ToolBtn active={showDefaultsBar} title="Default style for new drawings"
          onClick={() => setStyleOpen((o) => !o)}>
          <Palette size={16}/>
        </ToolBtn>
        {/* magnet — snap endpoints to bar OHLC */}
        <ToolBtn active={magnet} title={magnet ? 'Magnet on — snapping to bar OHLC' : 'Magnet — snap to bar OHLC'}
          onClick={() => api?.setMagnet(!magnet)}>
          <Magnet size={16}/>
        </ToolBtn>
        {/* stay-in-tool pin */}
        <ToolBtn active={stayInTool} title={stayInTool ? 'Staying in tool after each draw' : 'Pin — stay in tool after drawing'}
          onClick={() => api?.setStayInTool(!stayInTool)}>
          {stayInTool ? <Pin size={16}/> : <PinOff size={16}/>}
        </ToolBtn>
        {/* hide/show all */}
        <ToolBtn active={hidden} disabled={count === 0} title={hidden ? 'Drawings hidden — click to show' : 'Hide all drawings'}
          onClick={() => api?.setHidden(!hidden)}>
          {hidden ? <EyeOff size={16}/> : <Eye size={16}/>}
        </ToolBtn>
        {/* lock — freeze selection/drag */}
        <ToolBtn active={locked} title={locked ? 'Drawings locked — click to unlock' : 'Lock drawings (no select/drag)'}
          onClick={() => api?.setLocked(!locked)}>
          {locked ? <Lock size={16}/> : <LockOpen size={16}/>}
        </ToolBtn>
        <PaletteDivider/>
        {/* delete selected */}
        <ToolBtn disabled={!selection} title="Delete selected (Del)"
          onClick={() => api?.deleteSelected()}>
          <Trash2 size={16}/>
        </ToolBtn>
        {/* clear all */}
        <ToolBtn disabled={count === 0} title="Clear all drawings"
          onClick={clearAll}>
          <Eraser size={16}/>
        </ToolBtn>
      </div>

      {/* STYLE BAR — selection style, or the defaults popover. Sits along the top,
          offset past the vertical palette on desktop. */}
      {(showSelectionBar || showDefaultsBar) && defaults && (
        <div className="pointer-events-auto absolute z-20
          top-2 left-2 right-2 sm:left-14 sm:right-2
          flex flex-wrap items-center gap-x-3 gap-y-2
          px-2.5 py-1.5 rounded-xl border border-slate-200/80 dark:border-slate-700/80
          bg-white/95 dark:bg-slate-900/95 backdrop-blur shadow-lg max-w-full">
          {showSelectionBar && selection ? (
            <>
              <span className="text-[10px] uppercase tracking-wider font-bold text-violet-500 dark:text-violet-300 shrink-0">
                {selection.tool}
              </span>
              <StyleControls style={selection.style}
                onChange={(patch) => api?.restyleSelected(patch)}
                showFill={selection.supportsFill} showText={selection.supportsText}/>
              {selection.supportsText && (
                <button type="button" onClick={() => api?.editSelectedText()}
                  className="inline-flex items-center gap-1 text-[11px] font-bold text-slate-600 dark:text-slate-300 hover:text-violet-600 dark:hover:text-violet-300">
                  <Pencil size={12}/> Edit text
                </button>
              )}
              {selection.supportsFibLevels && (
                <button type="button" onClick={() => setFibOpen((o) => !o)}
                  className={`inline-flex items-center gap-1 text-[11px] font-bold transition-colors ${fibOpen
                    ? 'text-violet-600 dark:text-violet-300'
                    : 'text-slate-600 dark:text-slate-300 hover:text-violet-600 dark:hover:text-violet-300'}`}>
                  <AlignJustify size={12}/> Edit levels
                </button>
              )}
              <div className="h-5 w-px bg-slate-200 dark:bg-slate-700"/>
              <button type="button" title="Delete (Del)" onClick={() => api?.deleteSelected()}
                className="inline-flex items-center justify-center h-6 w-6 rounded text-red-500 hover:bg-red-50 dark:hover:bg-red-950/40">
                <Trash2 size={15}/>
              </button>
              <button type="button" title="Deselect" onClick={() => api?.selectNone()}
                className="inline-flex items-center justify-center h-6 w-6 rounded text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800">
                <X size={15}/>
              </button>
            </>
          ) : (
            <>
              <span className="text-[10px] uppercase tracking-wider font-bold text-slate-400 dark:text-slate-500 shrink-0">
                Default
              </span>
              <StyleControls style={defaults}
                onChange={(patch) => onSetDefaults(patch)}
                showFill showText/>
              <button type="button" title="Done" onClick={() => setStyleOpen(false)}
                className="inline-flex items-center justify-center h-6 w-6 rounded text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800">
                <X size={15}/>
              </button>
            </>
          )}
        </div>
      )}

      {/* FIB "EDIT LEVELS" PANEL (GOAL 4) — below the style bar while a fib is
          selected. Keyed by the selection id so switching fibs re-seeds it. */}
      {showSelectionBar && selection?.supportsFibLevels && fibOpen && (
        <FibLevelsEditor
          key={selection.id}
          api={api}
          selectionId={selection.id}
          seed={selection.fibLevels ?? api?.getSelectedFibLevels() ?? []}
          onClose={() => setFibOpen(false)}
        />
      )}
    </div>
  )
}
