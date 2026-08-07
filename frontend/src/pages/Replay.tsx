import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Play, Pause, StepForward, FastForward, Shuffle, Rewind, Download,
  TrendingUp, TrendingDown, X, AlertTriangle, CalendarOff, Loader2, EyeOff,
  Settings as SettingsIcon, Maximize2, Minimize2, Plus, Check, Clock, Crosshair, RotateCcw, Layers,
  Trash2, Eraser, Flag, FlaskConical, History, Grid3x3,
} from 'lucide-react'
import { replayApi, strategiesApi, backtestsApi, type ReplayDay, type ReplayMeta } from '../api/endpoints'
import TVReplayChart, { type SessionVisibility, type TVTrade } from '../components/TVReplayChart'
import type { Strategy, BacktestRun } from '../types'
import DrawingToolbar from '../components/DrawingToolbar'
import {
  DEFAULT_DRAW_STYLE,
  type DrawTool, type DrawStyle, type DrawingApi, type DrawingState,
} from '../lib/replayDrawings'
import {
  POINT_VALUES, checkExit, checkLimitFill, closePosition, makePendingOrder,
  openFromLimit, openPosition, sessionStats, unrealized,
  type ClosedTrade, type Direction, type OpenPosition, type PendingOrder, type SimBar,
} from '../lib/replaySim'

const INSTRUMENTS = ['ES', 'NQ', 'YM', 'RTY']
// Playback speed in bars per second (FX-Replay style).
const SPEEDS = [1, 2, 4, 10]
// Chart-only display timeframes in MINUTES (the sim ALWAYS steps raw 1m bars).
// Any 1–240 works via the custom input; these are the quick chips.
const TF_CHIPS = [1, 2, 3, 5, 15, 30, 60, 240]
const tfLabel = (t: number) => (t % 60 === 0 ? `${t / 60}h` : `${t}m`)
const LOG_KEY = 'theta_replay_log'

// ── continuous multi-day replay (FX-Replay-style) ────────────────────────────
// A continuous load spans CONTEXT_DAYS of look-back before the start day through
// FORWARD_DAYS after it. As playback nears the loaded end we pull the next
// MORE_DAYS-day chunk (starting APPEND_BUFFER bars early so a fast run never
// stalls) and append it, so the replay rolls on until real data ends.
const CONTEXT_DAYS = 5
const FORWARD_DAYS = 20
const MORE_DAYS = 5
const APPEND_BUFFER = 240
// Flat-only rewind can scrub back through revealed history down to this floor
// (kept above 0 so the chart never empties).
const REWIND_FLOOR = 2

// ── range-backtest mode ──────────────────────────────────────────────────────
// The Replay page has two modes: the manual bar-by-bar "practice" replay
// (everything else on this page) and a "Range Backtest" mode that runs the
// EXISTING async backtest engine over a whole date range at once, then plots
// every trade the strategy took on the same TradingView chart. It reuses
// backtestsApi verbatim (POST run → poll list for status/redis progress → GET
// metrics + trades) — no engine is rebuilt here.
const DATA_START = '2023-04-30'            // candle_cache lower bound (matches BacktestLab)
const BT_LOG_KEY = 'theta_replay_backtest_log'
const BT_LOG_MAX = 8
// On-chart visualization: the chart is fed REAL 1m bars (via replayApi.continuous)
// anchored at the run's start date — NOT the /chart-data candles, which come back
// at the strategy's ~15m timeframe and would mis-bucket the 1m resampler + marker
// snap. A single continuous call spans up to 30 trading days; we chain a couple
// more chunks to widen the window but CAP it so a multi-year range never tries to
// pull years of 1m bars into the browser (we log + surface a note when capped).
const BT_CHART_MAX_CHUNKS = 3              // initial 30d + up to 3×30d more ≈ 120 trading days
const BT_CHART_MAX_BARS = 60000

const isoDay = (d: Date) => d.toISOString().split('T')[0]
const btYesterday = () => { const d = new Date(); d.setDate(d.getDate() - 1); return isoDay(d) }
const btMonthsBack = (from: string, months: number) => {
  const d = new Date(from + 'T00:00:00Z'); d.setUTCMonth(d.getUTCMonth() - months); return isoDay(d)
}
// Fixed-day lookback (not calendar months) so 1Y can't exceed the free-trial
// 365-day backend cap across a leap Feb 29.
const btDaysBack = (endIso: string, n: number) => {
  const d = new Date(endIso + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() - n); return isoDay(d)
}
const btClampStart = (s: string, end: string) => (s < DATA_START ? DATA_START : s > end ? end : s)
// "All" is bounded by data start AND the backend's 3-year paid cap.
const btAllStart = (end: string) => {
  const d = new Date(end + 'T00:00:00Z'); d.setUTCFullYear(d.getUTCFullYear() - 3); d.setUTCDate(d.getUTCDate() + 1)
  const threeYr = isoDay(d); return threeYr > DATA_START ? threeYr : DATA_START
}
const BT_PRESETS: { label: string; start: (end: string) => string }[] = [
  { label: '1M', start: (e) => btClampStart(btMonthsBack(e, 1), e) },
  { label: '3M', start: (e) => btClampStart(btMonthsBack(e, 3), e) },
  { label: '6M', start: (e) => btClampStart(btMonthsBack(e, 6), e) },
  { label: '1Y', start: (e) => btClampStart(btDaysBack(e, 364), e) },
  { label: 'All', start: (e) => btAllStart(e) },
]
const fmtMoney = (v: number) =>
  `${v < 0 ? '−' : ''}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`
const fmtPct01 = (v: number) => `${(v * 100).toFixed(1)}%`
const fmtBtElapsed = (s: number) => {
  const m = Math.floor(s / 60), sec = s % 60
  return m > 0 ? `${m}m ${sec.toString().padStart(2, '0')}s` : `${sec}s`
}
const fmtBtTime = (t: string | null) => {
  if (!t) return '—'
  const d = new Date(t)
  if (isNaN(d.getTime())) return t.slice(0, 16).replace('T', ' ')
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
const btIsActive = (status?: string) => {
  const s = (status || '').toLowerCase()
  return s === 'running' || s === 'queued' || s === 'pending'
}

type BtLogEntry = {
  run_id: string; strategy_name: string; instrument: string; start_date: string; end_date: string
  win_rate: number | null; profit_factor: number | null; net_profit: number | null
  total_trades: number | null; finished_at: string
}
function btReadLog(): BtLogEntry[] {
  try { const raw = localStorage.getItem(BT_LOG_KEY); const arr = raw ? JSON.parse(raw) : []; return Array.isArray(arr) ? arr : [] } catch { return [] }
}
function btWriteLog(entries: BtLogEntry[]) {
  try { localStorage.setItem(BT_LOG_KEY, JSON.stringify(entries.slice(0, BT_LOG_MAX))) } catch { /* quota — ignore */ }
}

// ── chart settings (GOAL A/B/I — persisted, passed live to TVReplayChart) ────

const SETTINGS_KEY = 'theta_replay_settings'

type SessionVis = { asia: boolean; london: boolean; nyAm: boolean; nyLunch: boolean; nyPm: boolean }
type ReplaySettings = {
  v: number
  upColor: string
  downColor: string
  background: string | null
  // GOAL 1: background grid on/off. Default true = the chart's existing look.
  showGrid: boolean
  sessionsEnabled: boolean
  sessionVisibility: SessionVis
  timezone: string    // IANA, or '__local__' for the browser zone
  hour12: boolean
}

const DEFAULT_SETTINGS: ReplaySettings = {
  v: 1,
  upColor: '#26a69a',
  downColor: '#ef5350',
  background: null,
  showGrid: true,
  sessionsEnabled: false,
  sessionVisibility: { asia: true, london: true, nyAm: true, nyLunch: true, nyPm: true },
  timezone: 'America/New_York',
  hour12: false,
}

// Tolerant loader: unknown/legacy shapes fall back to defaults key-by-key.
function loadSettings(): ReplaySettings {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY)
    if (!raw) return DEFAULT_SETTINGS
    const p = JSON.parse(raw) || {}
    return {
      ...DEFAULT_SETTINGS,
      ...p,
      background: typeof p.background === 'string' ? p.background : null,
      // Missing/legacy key ⇒ grid stays on (the pre-GOAL-1 appearance).
      showGrid: typeof p.showGrid === 'boolean' ? p.showGrid : true,
      sessionVisibility: { ...DEFAULT_SETTINGS.sessionVisibility, ...(p.sessionVisibility || {}) },
    }
  } catch {
    return DEFAULT_SETTINGS
  }
}
function saveSettings(s: ReplaySettings) {
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(s)) } catch { /* quota — ignore */ }
}

// ── drawing tool prefs (last-used default style + magnet) ────────────────────
// Persisted so the palette re-opens with the trader's last colour/width/style.
// NOT keyed by date — drawings themselves persist per-instrument inside the
// engine; this is only the style the palette applies to NEW drawings.
const DRAW_PREFS_KEY = 'theta_replay_draw_prefs'
type DrawPrefs = { style: DrawStyle; magnet: boolean }
const DEFAULT_DRAW_PREFS: DrawPrefs = { style: { ...DEFAULT_DRAW_STYLE }, magnet: false }

function loadDrawPrefs(): DrawPrefs {
  try {
    const raw = localStorage.getItem(DRAW_PREFS_KEY)
    if (!raw) return DEFAULT_DRAW_PREFS
    const p = JSON.parse(raw) || {}
    return { style: { ...DEFAULT_DRAW_STYLE, ...(p.style || {}) }, magnet: !!p.magnet }
  } catch {
    return DEFAULT_DRAW_PREFS
  }
}
function saveDrawPrefs(p: DrawPrefs) {
  try { localStorage.setItem(DRAW_PREFS_KEY, JSON.stringify(p)) } catch { /* quota — ignore */ }
}

const COLOR_PRESETS: { name: string; up: string; down: string }[] = [
  { name: 'TradingView', up: '#26a69a', down: '#ef5350' },
  { name: 'Classic', up: '#089981', down: '#f23645' },
  { name: 'Mono', up: '#b0b3ba', down: '#5d6069' },
  { name: 'Violet', up: '#8b5cf6', down: '#f43f5e' },
]

const TIMEZONES: { value: string; label: string; short: string }[] = [
  { value: 'America/New_York', label: 'Exchange · ET', short: 'ET' },
  { value: 'UTC', label: 'UTC', short: 'UTC' },
  { value: '__local__', label: 'Local (browser)', short: 'Local' },
  { value: 'America/Chicago', label: 'Chicago · CT', short: 'CT' },
  { value: 'Europe/London', label: 'London', short: 'LON' },
  { value: 'Asia/Tokyo', label: 'Tokyo', short: 'TYO' },
]
const TZ_SHORT: Record<string, string> = Object.fromEntries(TIMEZONES.map((z) => [z.value, z.short]))
function browserTz(): string {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' } catch { return 'UTC' }
}
const resolveTz = (tz: string) => (tz === '__local__' ? browserTz() : tz)

const SESSION_KEYS: { key: keyof SessionVis; label: string; hint: string }[] = [
  { key: 'asia', label: 'Asia', hint: '18:00–02:00 ET · ETH only' },
  { key: 'london', label: 'London', hint: '02:00–05:00 ET · ETH only' },
  { key: 'nyAm', label: 'NY AM', hint: '09:30–11:00 ET' },
  { key: 'nyLunch', label: 'NY Lunch', hint: '11:00–14:00 ET' },
  { key: 'nyPm', label: 'NY PM', hint: '14:00–16:00 ET' },
]

// ── localStorage trade log ───────────────────────────────────────────────────

type LogEntry = {
  session_id: string
  instrument: string
  date: string
  direction: Direction
  qty: number
  entry_price: number
  exit_price: number
  entry_time: number
  exit_time: number
  exit_reason: string
  points: number
  r: number | null   // GOAL G: null when the trade had no initial stop
  dollars: number
  logged_at: string
}

function readLog(): LogEntry[] {
  try {
    const raw = localStorage.getItem(LOG_KEY)
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr : []
  } catch {
    return []
  }
}

function appendLog(entry: LogEntry) {
  try {
    const arr = readLog()
    arr.push(entry)
    // Cap the log so it can't grow unbounded in localStorage.
    localStorage.setItem(LOG_KEY, JSON.stringify(arr.slice(-500)))
  } catch {
    // Quota/serialization failures shouldn't break the sim.
  }
}

// ── small display helpers ────────────────────────────────────────────────────

const fmtPts = (v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}`
const fmtR = (v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}R`
const fmtUsd = (v: number) =>
  `${v >= 0 ? '+' : '-'}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

const ET_TIME_FMT = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York', hour12: false, hour: '2-digit', minute: '2-digit',
})
const etClock = (epochSecs: number) => ET_TIME_FMT.format(new Date(epochSecs * 1000)) + ' ET'
// The ET calendar date (YYYY-MM-DD) of a bar — used to label the "current" day
// in a continuous multi-day window and to date multi-day trade-log entries.
const ET_DATE_FMT = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
})
const etDateOf = (epochSecs: number) => ET_DATE_FMT.format(new Date(epochSecs * 1000))

// Timezone/format-aware clock for the toolbar readout (time-only, blind-safe).
const _clockCache = new Map<string, Intl.DateTimeFormat>()
function zonedClock(epochSecs: number, timezone: string, hour12: boolean): string {
  const tz = resolveTz(timezone)
  const key = `${tz}|${hour12}`
  let f = _clockCache.get(key)
  if (!f) {
    f = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      hour12,
      minute: '2-digit',
      ...(hour12 ? { hour: 'numeric' } : { hourCycle: 'h23' as const, hour: '2-digit' }),
    })
    _clockCache.set(key, f)
  }
  return f.format(new Date(epochSecs * 1000))
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: 'pos' | 'neg' | 'muted' }) {
  const color = tone === 'pos' ? 'text-green-600 dark:text-green-400'
    : tone === 'neg' ? 'text-red-500'
    : tone === 'muted' ? 'text-slate-300 dark:text-slate-600'
    : 'text-slate-900 dark:text-slate-100'
  return (
    <div className="bg-slate-50 rounded-xl border border-slate-200 p-3 dark:bg-slate-900 dark:border-slate-700">
      <div className="text-[10px] text-slate-400 uppercase tracking-wider font-medium mb-1 dark:text-slate-500">{label}</div>
      <div className={`text-lg font-extrabold ${color}`}>{value}</div>
    </div>
  )
}

// FX-Replay-style compact stat chip for the session / all-time strip (GOAL D).
function StatChip({ label, value, tone, note }: { label: string; value: string; tone?: 'pos' | 'neg' | 'muted'; note?: string }) {
  const color = tone === 'pos' ? 'text-green-600 dark:text-green-400'
    : tone === 'neg' ? 'text-red-500'
    : tone === 'muted' ? 'text-slate-400 dark:text-slate-500'
    : 'text-slate-900 dark:text-slate-100'
  return (
    <div className="flex flex-col px-3 py-1.5 rounded-lg bg-white dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 min-w-0">
      <span className="text-[9px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500 whitespace-nowrap">{label}</span>
      <span className={`text-sm font-extrabold tabular-nums ${color}`}>
        {value}{note && <span className="ml-1 text-[9px] font-semibold text-slate-400 dark:text-slate-500">{note}</span>}
      </span>
    </div>
  )
}

/** Dependency-free equity line for the range-backtest results (client-rendered
 *  from metrics.equity_curve). Mirrors BacktestLab's EquityLine. */
function BtEquityLine({ data, height = 120 }: { data: number[]; height?: number }) {
  if (data.length < 2) return null
  const w = 600
  const min = Math.min(...data), max = Math.max(...data)
  const span = max - min || 1
  const pad = 3
  const stepX = (w - pad * 2) / (data.length - 1)
  const pts = data.map((v, i) =>
    `${(pad + i * stepX).toFixed(2)} ${(pad + (1 - (v - min) / span) * (height - pad * 2)).toFixed(2)}`)
  const line = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p}`).join(' ')
  const area = `${line} L${(pad + (data.length - 1) * stepX).toFixed(2)} ${height} L${pad} ${height} Z`
  const up = data[data.length - 1] >= data[0]
  const stroke = up ? '#10b981' : '#ef4444'
  return (
    <svg viewBox={`0 0 ${w} ${height}`} className="w-full" style={{ height }} aria-hidden="true" preserveAspectRatio="none">
      <path d={area} fill={stroke} opacity={0.08} stroke="none" />
      <path d={line} fill="none" stroke={stroke} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function Toggle({ checked, onChange, disabled }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <button type="button" role="switch" aria-checked={checked} disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors disabled:opacity-40 ${checked ? 'bg-violet-600' : 'bg-slate-300 dark:bg-slate-600'}`}>
      <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${checked ? 'translate-x-4' : 'translate-x-0.5'}`}/>
    </button>
  )
}

function ColorField({ label, value, onChange }: { label?: string; value: string; onChange: (v: string) => void }) {
  return (
    <label className="flex flex-col gap-1 min-w-0">
      {label && <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">{label}</span>}
      <span className="flex items-center gap-2 rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 px-1.5 py-1">
        <input type="color" value={value} onChange={(e) => onChange(e.target.value)}
          className="w-7 h-7 rounded cursor-pointer bg-transparent border-0 p-0"/>
        <span className="text-xs font-mono text-slate-600 dark:text-slate-300 truncate">{value}</span>
      </span>
    </label>
  )
}

function MenuItem({ icon: Icon, label, onClick, active, autoFocus }: { icon?: any; label: string; onClick: () => void; active?: boolean; autoFocus?: boolean }) {
  return (
    <button role="menuitem" autoFocus={autoFocus} onClick={onClick}
      className="w-full flex items-center gap-2.5 px-3 py-1.5 text-left text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-700/60 focus:bg-slate-100 dark:focus:bg-slate-700/60 outline-none">
      {Icon ? <Icon size={14} className="text-slate-400 shrink-0"/> : <span className="w-3.5 shrink-0"/>}
      <span className="flex-1 whitespace-nowrap">{label}</span>
      {active && <Check size={13} className="text-violet-500 shrink-0"/>}
    </button>
  )
}

type SettingsTab = 'appearance' | 'sessions' | 'time'

// ── page ─────────────────────────────────────────────────────────────────────

export default function Replay() {
  // Day selection
  const [instrument, setInstrument] = useState('ES')
  const [date, setDate] = useState('')
  const [blind, setBlind] = useState(false)
  const [eth, setEth] = useState(false)
  const [day, setDay] = useState<ReplayDay | null>(null)
  const [loadState, setLoadState] = useState<'idle' | 'loading' | 'holiday' | 'error'>('idle')
  const [loadError, setLoadError] = useState('')

  // Playback
  const [revealed, setRevealed] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)
  const [tf, setTf] = useState<number>(1)
  const [customTf, setCustomTf] = useState('')
  const [done, setDone] = useState(false)
  // Forces a chart-view reset (fit/rescale) without touching the session id.
  const [chartNonce, setChartNonce] = useState(0)

  // Order ticket — SL/TP optional, empty by default (GOAL G).
  const [qtyStr, setQtyStr] = useState('1')
  const [stopStr, setStopStr] = useState('')
  const [targetMode, setTargetMode] = useState<'r' | 'points'>('r')
  const [targetStr, setTargetStr] = useState('')
  // GOAL 2: MARKET fills at the current close; LIMIT rests until a bar touches it.
  const [orderType, setOrderType] = useState<'market' | 'limit'>('market')
  const [limitStr, setLimitStr] = useState('')

  // Sim state
  const [pos, setPos] = useState<OpenPosition | null>(null)
  // GOAL 2: a single resting limit order, only while FLAT (mutually exclusive
  // with `pos` — the ticket blocks placing while either exists).
  const [pendingOrder, setPendingOrder] = useState<PendingOrder | null>(null)
  const [closed, setClosed] = useState<ClosedTrade[]>([])
  const sessionIdRef = useRef('')
  const [logVersion, setLogVersion] = useState(0)

  // Continuous multi-day window bookkeeping. `day.candles` is the WHOLE window
  // (context + forward days). hasMoreForward gates the append-as-you-advance
  // fetch; fetchingMoreRef single-flights it. Session bands per day are drawn by
  // the chart from bar ET times, so day boundaries need no extra state here.
  const [hasMoreForward, setHasMoreForward] = useState(false)
  const fetchingMoreRef = useRef(false)

  // Live SL/TP price editors + place-on-chart arming (GOAL G). 'limit' arms the
  // LIMIT ticket's price field to the next chart click (GOAL 2).
  const [slInput, setSlInput] = useState('')
  const [tpInput, setTpInput] = useState('')
  const [armed, setArmed] = useState<'sl' | 'tp' | 'limit' | null>(null)

  // Chart settings + UI chrome
  const [settings, setSettings] = useState<ReplaySettings>(() => loadSettings())
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsTab, setSettingsTab] = useState<SettingsTab>('appearance')
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number } | null>(null)
  const [fullscreen, setFullscreen] = useState(false)
  const fsRef = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  // Drawing layer (builder 1 engine): imperative controller + reactive state.
  // The api is handed once on chart mount (null on unmount); state pushes after
  // every tool/selection/toggle change.
  const drawApiRef = useRef<DrawingApi | null>(null)
  const [drawState, setDrawState] = useState<DrawingState | null>(null)
  const drawPrefsJsonRef = useRef('')

  // ── range-backtest mode (items 1-3) ──────────────────────────────────────
  const [mode, setMode] = useState<'practice' | 'backtest'>('practice')
  const btEndBound = useMemo(() => btYesterday(), [])
  const [strategyId, setStrategyId] = useState('')
  const [btStart, setBtStart] = useState(() => BT_PRESETS[2].start(btYesterday())) // 6M default
  const [btEnd, setBtEnd] = useState(btEndBound)
  const [btPreset, setBtPreset] = useState<string | null>('6M')
  // Per-run A/B toggle for the live-bias + NY-lunch owner gates. Default OFF =
  // today's raw-edge backtest exactly; ON mirrors what live/paper actually trade.
  const [btOwnerGates, setBtOwnerGates] = useState(false)
  const [runId, setRunId] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  const [btStartedAt, setBtStartedAt] = useState<number | null>(null)
  const [btElapsed, setBtElapsed] = useState(0)
  const [btLog, setBtLog] = useState<BtLogEntry[]>(() => btReadLog())
  const btLoggedRef = useRef<Set<string>>(new Set())
  const btMissesRef = useRef(0)
  const [runLost, setRunLost] = useState(false)
  // On-chart backtest window (real 1m bars) + trade overlay bookkeeping.
  const [btTf, setBtTf] = useState<number>(15)
  const [btChartBars, setBtChartBars] = useState<SimBar[]>([])
  const [btChartState, setBtChartState] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const [btChartCapped, setBtChartCapped] = useState(false)
  const [btChartSpan, setBtChartSpan] = useState<{ from: string; to: string } | null>(null)
  const [btChartNonce, setBtChartNonce] = useState(0)
  const btFsRef = useRef<HTMLDivElement>(null)

  const updateSettings = (patch: Partial<ReplaySettings>) => {
    setSettings((prev) => {
      const next = { ...prev, ...patch }
      saveSettings(next)
      return next
    })
  }
  const openSettings = (tab: SettingsTab) => { setSettingsTab(tab); setSettingsOpen(true) }
  const isDark = typeof document !== 'undefined' && document.documentElement.classList.contains('dark')

  const { data: meta } = useQuery<ReplayMeta | null>({
    queryKey: ['replay-meta'],
    queryFn: () => replayApi.meta().then((r) => r.data).catch(() => null),
    staleTime: 10 * 60 * 1000,
  })
  const instruments = meta?.instruments?.length ? meta.instruments : INSTRUMENTS
  // P&L math keys off the LOADED day's instrument so flipping the select
  // mid-session (which does not reload bars) can't corrupt point values.
  const activeInstrument = day?.instrument || instrument
  const pointValue = POINT_VALUES[activeInstrument] ?? 50

  const bars: SimBar[] = day?.candles ?? []
  const revealedBars = useMemo(() => bars.slice(0, revealed), [bars, revealed])
  const lastBar = revealedBars.length > 0 ? revealedBars[revealedBars.length - 1] : null

  // ── range-backtest data flow (items 1-3) ─────────────────────────────────
  // Lifted wholesale from BacktestLab: POST run → poll the list for status +
  // redis progress → GET metrics + trades once completed. Only fetches while in
  // backtest mode / while a run id is set, so practice mode is untouched.
  const { data: strategies = [] } = useQuery({
    queryKey: ['strategies'],
    queryFn: () => strategiesApi.list().then((r) => r.data as Strategy[]),
    enabled: mode === 'backtest',
  })

  const { data: btRuns = [] } = useQuery({
    queryKey: ['replay-backtest-runs'],
    queryFn: () => backtestsApi.list().then((r) => {
      const rows = r.data as BacktestRun[]
      if (runId && !rows.some((x) => x.id === runId)) {
        btMissesRef.current += 1
        if (btMissesRef.current >= 5) setRunLost(true)
      } else {
        btMissesRef.current = 0
      }
      return rows
    }),
    enabled: !!runId,
    refetchInterval: (q: any) => {
      const rows: BacktestRun[] = (q?.state?.data as BacktestRun[]) || []
      const mine = rows.find((r) => r.id === runId)
      if (!mine) return btMissesRef.current >= 5 ? false : 2000
      if (btIsActive(mine.status) && btStartedAt != null && Date.now() - btStartedAt > 35 * 60_000) return false
      return btIsActive(mine.status) ? 2000 : false
    },
  })

  const btRun = btRuns.find((r) => r.id === runId)
  const btRunStatus = (btRun?.status || (runId ? 'queued' : '')).toLowerCase()
  const btStalled = !!runId && (btRunStatus === '' || btIsActive(btRunStatus)) && btStartedAt != null && btElapsed > 35 * 60
  const btRunning = !!runId && !runLost && !btStalled && (btRunStatus === '' || btIsActive(btRunStatus))
  const btFailed = btRunStatus === 'failed' || btRunStatus === 'cancelled'
  const btCompleted = btRunStatus === 'completed'

  const { data: btMetrics, isLoading: btMetricsLoading }: any = useQuery({
    queryKey: ['replay-backtest-metrics', runId],
    queryFn: () => backtestsApi.getMetrics(runId!).then((r) => r.data),
    enabled: !!runId && btCompleted,
    refetchInterval: (q: any) => (q?.state?.data ? false : 2000), // metrics row trails the status flip
    retry: 3,
    retryDelay: 1500,
  })

  const { data: btTrades = [], isPending: btTradesPending }: any = useQuery({
    queryKey: ['replay-backtest-trades', runId],
    queryFn: () => backtestsApi.getTrades(runId!).then((r) => r.data),
    enabled: !!runId && btCompleted && !!btMetrics,
  })

  // Elapsed ticker while a backtest runs.
  useEffect(() => {
    if (!btRunning || btStartedAt == null) return
    const t = setInterval(() => setBtElapsed(Math.floor((Date.now() - btStartedAt) / 1000)), 1000)
    return () => clearInterval(t)
  }, [btRunning, btStartedAt])

  // Log completed backtests once per run id (localStorage, this browser).
  useEffect(() => {
    if (!runId || !btCompleted || !btMetrics || btLoggedRef.current.has(runId)) return
    btLoggedRef.current.add(runId)
    const entry: BtLogEntry = {
      run_id: runId,
      strategy_name: btRun?.strategy_name || strategies.find((s) => s.id === strategyId)?.name || 'Strategy',
      instrument: btRun?.instrument || instrument,
      start_date: (btRun?.start_date || btStart).slice(0, 10),
      end_date: (btRun?.end_date || btEnd).slice(0, 10),
      win_rate: btMetrics.win_rate ?? null,
      profit_factor: btMetrics.profit_factor ?? null,
      net_profit: btMetrics.net_profit ?? null,
      total_trades: btMetrics.total_trades ?? null,
      finished_at: new Date().toISOString(),
    }
    setBtLog((prev) => {
      const next = [entry, ...prev.filter((e) => e.run_id !== runId)].slice(0, BT_LOG_MAX)
      btWriteLog(next)
      return next
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, btCompleted, btMetrics])

  // Item 3: once a run completes, load a REAL 1m window (replayApi.continuous)
  // anchored at the run's start date and overlay its trade markers. Capped so a
  // long range never pulls years of 1m bars — logs + surfaces a note when capped.
  useEffect(() => {
    if (!runId || !btCompleted) return
    const r = btRun
    if (!r) return
    const myRun = runId
    const inst = r.instrument
    const startD = (r.start_date || btStart).slice(0, 10)
    const endD = (r.end_date || btEnd).slice(0, 10)
    const endTs = Math.floor(Date.parse(endD + 'T23:59:59Z') / 1000)
    let cancelled = false
    setBtChartState('loading'); setBtChartCapped(false); setBtChartSpan(null); setBtChartBars([])
    ;(async () => {
      try {
        const first = await replayApi.continuous(inst, startD, { context: 0, forward: 30, eth: false })
        if (cancelled) return
        let candles = first.data.candles || []
        let hasMore = first.data.hasMoreForward
        let chunks = 0
        while (hasMore && chunks < BT_CHART_MAX_CHUNKS && candles.length < BT_CHART_MAX_BARS) {
          const lastTs = candles.length ? candles[candles.length - 1].time : 0
          if (lastTs >= endTs) break // reached the range end — no need to load more
          const more = await replayApi.continuousMore(inst, lastTs, { days: 30, eth: false })
          if (cancelled) return
          const add = (more.data.candles || []).filter((b) => b.time > lastTs)
          if (!add.length) { hasMore = false; break }
          candles = candles.concat(add)
          hasMore = more.data.hasMoreForward
          chunks++
        }
        if (cancelled) return
        if (!candles.length) { setBtChartState('error'); return }
        const lastTs = candles[candles.length - 1].time
        const capped = (hasMore && lastTs < endTs) || candles.length >= BT_CHART_MAX_BARS
        if (capped) {
          console.warn('[Replay backtest] charted window capped', {
            run: myRun, bars: candles.length, chunks,
            loadedThrough: new Date(lastTs * 1000).toISOString(), rangeEnd: endD,
          })
        }
        setBtChartBars(candles as SimBar[])
        setBtChartSpan({ from: etDateOf(candles[0].time), to: etDateOf(lastTs) })
        setBtChartCapped(capped)
        setBtChartState('ready')
        setBtChartNonce((n) => n + 1)
      } catch (e) {
        if (!cancelled) { console.warn('[Replay backtest] chart-window load failed', e); setBtChartState('error') }
      }
    })()
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, btCompleted])

  async function startBacktest() {
    if (!strategyId || starting) return
    if (btStart > btEnd) { setStartError('Start date must be on or before the end date.'); return }
    btMissesRef.current = 0
    setRunLost(false); setStarting(true); setStartError(null)
    setBtChartState('idle'); setBtChartBars([]); setBtChartSpan(null); setBtChartCapped(false)
    try {
      const res = await backtestsApi.run({
        strategy_id: strategyId,
        instrument,
        start_date: btStart,
        end_date: btEnd,
        initial_capital: 100000,
        commission_per_side: 2.25,
        slippage_ticks: 1,
        apply_owner_gates: btOwnerGates,
      })
      setRunId((res.data as BacktestRun).id)
      setBtStartedAt(Date.now()); setBtElapsed(0)
    } catch (e: any) {
      setStartError(e?.response?.data?.detail || e?.message || 'Failed to start backtest')
    } finally {
      setStarting(false)
    }
  }

  function applyBtPreset(label: string) {
    const p = BT_PRESETS.find((x) => x.label === label)
    if (!p) return
    setBtEnd(btEndBound); setBtStart(p.start(btEndBound)); setBtPreset(label)
  }

  function viewBtLogEntry(e: BtLogEntry) {
    btMissesRef.current = 0; setRunLost(false)
    setRunId(e.run_id); setBtStartedAt(null); setStartError(null)
  }

  const switchMode = (m: 'practice' | 'backtest') => {
    if (m === mode) return
    if (m === 'backtest') { setPlaying(false); setArmed(null) } // never leave practice playback running
    setMode(m)
  }

  // Strategy grouping (V2 engine vs V1) for the picker.
  const v2Strats = strategies.filter((s) => s.engine_version === 'v2')
  const v1Strats = strategies.filter((s) => s.engine_version !== 'v2')
  const btGrouped = v2Strats.length > 0 && v1Strats.length > 0
  const btEquitySeries: number[] = (btMetrics?.equity_curve || []).map((p: any) => p.equity)

  // Owner-gate skip count for the current run, when the run opted into the
  // live-bias + NY-lunch gates. The backend stashes it on the run's
  // strategy_params_snapshot (JSON); read it defensively from wherever the API
  // surfaces it (metrics body or the run record) so it renders whenever present.
  const btOwnerGateSkips: number | null = (() => {
    const m: any = btMetrics
    const r: any = btRun
    const v = m?.owner_gate_skips ?? r?.owner_gate_skips ?? r?.strategy_params_snapshot?.owner_gate_skips
    return typeof v === 'number' && Number.isFinite(v) ? v : null
  })()

  // Backtest trades → chart markers, clipped to the loaded 1m window (item 3).
  // The trades API returns no stop_loss/R, so we pass r=null (no fake R label)
  // and colour the exit by is_winner via TVTrade.winner.
  const btChartTrades: TVTrade[] = useMemo(() => {
    if (!btChartBars.length || !btTrades.length) return []
    const first = btChartBars[0].time
    const last = btChartBars[btChartBars.length - 1].time
    const out: TVTrade[] = []
    for (const t of btTrades as any[]) {
      const et = Math.floor(Date.parse(t.entry_time) / 1000)
      if (!Number.isFinite(et) || et < first || et > last) continue
      const xtRaw = Date.parse(t.exit_time)
      const xt = Number.isFinite(xtRaw) ? Math.min(Math.max(Math.floor(xtRaw / 1000), first), last) : et
      out.push({ direction: t.direction === 'short' ? 'short' : 'long', entryTime: et, exitTime: xt, r: null, winner: !!t.is_winner })
    }
    return out
  }, [btChartBars, btTrades])

  const btPlottedCount = btChartTrades.length
  const btResultTradeCount = (btTrades as any[]).length

  // ── day loading ────────────────────────────────────────────────────────────

  const resetSession = () => {
    setDay(null); setRevealed(0); setPlaying(false); setDone(false)
    setPos(null); setPendingOrder(null); setClosed([]); setTf(1); setArmed(null)
    // A limit price from the previous window would be far off-market — clear it
    // (the LMT toggle / chart-click re-fills it from the new day's close).
    setLimitStr('')
    setHasMoreForward(false)
    fetchingMoreRef.current = false
    sessionIdRef.current = ''
  }

  // Clamp the initial playhead to a valid reveal count.
  const clampReveal = (idx: number, len: number) => Math.max(1, Math.min(idx || 0, len))

  // Continuous multi-day load. The window spans CONTEXT_DAYS before `start`
  // through FORWARD_DAYS after; revealed starts at the start day's OPEN
  // (playhead_index) so all look-back context sits behind the playhead. Reveals
  // roll straight into the next day — there is no per-day "session over" wall.
  const loadContinuous = async (inst: string, start: string, blindMode: boolean, ethMode = eth): Promise<'ok' | 'holiday' | 'error'> => {
    resetSession()
    setBlind(blindMode)
    setLoadState('loading')
    setLoadError('')
    try {
      const res = await replayApi.continuous(inst, start, { context: CONTEXT_DAYS, forward: FORWARD_DAYS, eth: ethMode })
      const c = res.data
      if (!c?.candles?.length) {
        setLoadState('holiday')
        return 'holiday'
      }
      setDay({ instrument: c.instrument, date: c.startDate, candles: c.candles, pdh: null, pdl: null })
      setDate(c.startDate)
      setHasMoreForward(c.hasMoreForward)
      setRevealed(clampReveal(c.playheadIndex, c.candles.length))
      sessionIdRef.current = `${inst}-${c.startDate}-${Date.now()}`
      setLoadState('idle')
      return 'ok'
    } catch (e: any) {
      if (e?.response?.status === 404) {
        setLoadState('holiday')
        return 'holiday'
      }
      setLoadState('error')
      setLoadError(e?.response?.data?.detail || e?.message || 'Failed to load bars.')
      return 'error'
    }
  }

  const loadRandomDay = async (ethMode = eth) => {
    // Blind mode: the SERVER picks a hidden weekday start (/replay/continuous/
    // random) so holidays are pre-filtered and the date can't be inferred
    // client-side. day_starts dates ARE in the payload but stay hidden until end.
    resetSession()
    setBlind(true)
    setLoadState('loading')
    setLoadError('')
    try {
      const res = await replayApi.continuousRandom(instrument, { context: CONTEXT_DAYS, forward: FORWARD_DAYS, eth: ethMode })
      const c = res.data
      if (!c?.candles?.length) {
        setLoadState('holiday')
        return
      }
      setDay({ instrument: c.instrument, date: c.startDate, candles: c.candles, pdh: null, pdl: null })
      setDate(c.startDate)
      setHasMoreForward(c.hasMoreForward)
      setRevealed(clampReveal(c.playheadIndex, c.candles.length))
      sessionIdRef.current = `${instrument}-${c.startDate}-${Date.now()}`
      setLoadState('idle')
    } catch (e: any) {
      setLoadState('error')
      setLoadError(e?.response?.data?.detail || e?.message || 'Failed to load a random day.')
    }
  }

  // ETH/RTH toggle: reload the SAME window with/without the overnight session.
  // Blind stays blind — we reuse the known start date internally (never shown
  // while hidden). Sim semantics are unchanged; only the 1m bar set differs.
  const toggleEth = () => {
    const next = !eth
    setEth(next)
    if (!day) return
    if (date) loadContinuous(activeInstrument, date, blind, next)
    else loadRandomDay(next)
  }

  // ── trade recording ────────────────────────────────────────────────────────

  const recordTrade = (t: ClosedTrade) => {
    setClosed((prev) => [...prev, t])
    appendLog({
      session_id: sessionIdRef.current,
      instrument: activeInstrument,
      // Multi-day window: date the entry by the trade's own ET day, not the
      // window's start date.
      date: etDateOf(t.exitTime),
      direction: t.direction,
      qty: t.qty,
      entry_price: t.entryPrice,
      exit_price: t.exitPrice,
      entry_time: t.entryTime,
      exit_time: t.exitTime,
      exit_reason: t.exitReason,
      points: t.points,
      r: t.r,
      dollars: t.dollars,
      logged_at: new Date().toISOString(),
    })
    setLogVersion((v) => v + 1)
  }

  // ── playback engine ────────────────────────────────────────────────────────

  const endSession = () => {
    setPlaying(false)
    if (pos && lastBar) {
      recordTrade(closePosition(pos, lastBar.close, lastBar.time, 'session_end', pointValue))
      setPos(null)
    }
    setPendingOrder(null) // GOAL 2: an unfilled resting order expires at session end
    setDone(true)
  }

  // Pull the next forward chunk and APPEND it to the END of the window (never
  // prepend — bars[0] is the chart's resample anchor and sessionIdRef must stay
  // stable so the chart does a cheap incremental append, not a full redraw that
  // loses zoom). Single-flighted; drops the result if the session changed.
  const maybeFetchMore = () => {
    if (fetchingMoreRef.current || !hasMoreForward || !day) return
    const cur = day
    if (!cur.candles.length) return
    const afterTs = cur.candles[cur.candles.length - 1].time
    const sid = sessionIdRef.current
    fetchingMoreRef.current = true
    replayApi.continuousMore(activeInstrument, afterTs, { days: MORE_DAYS, eth })
      .then((res) => {
        if (sessionIdRef.current !== sid) return // a new load happened — discard
        const chunk = res.data
        const add = (chunk.candles || []).filter((b) => b.time > afterTs) // defensive: no overlap
        if (add.length) {
          // Append ONLY to the end — bars[0] is the chart's resample anchor and
          // sessionIdRef stays stable, so this is a cheap incremental chart
          // append (no full redraw / zoom loss).
          setDay((prev) => (prev ? { ...prev, candles: [...prev.candles, ...add] } : prev))
        }
        setHasMoreForward(!!chunk.hasMoreForward)
      })
      .catch(() => { if (sessionIdRef.current === sid) setHasMoreForward(false) }) // stop cleanly on error
      .finally(() => { fetchingMoreRef.current = false })
  }

  // Advance n 1m bars in one shot. A single function (vs calling step() n
  // times) because synchronous repeat calls would all read the same stale
  // `revealed`/`pos` state from this render. Reveals roll ACROSS day boundaries
  // — the only true end is when the data is exhausted (no more forward) or the
  // trader ends the session.
  const advance = (n = 1) => {
    if (!day || done) return
    const total = bars.length
    if (revealed >= total) {
      // At the end of the loaded window. If more forward data exists, keep
      // playing while the append lands (spin-wait auto-resumes); otherwise this
      // is the real end of the data.
      if (hasMoreForward) { maybeFetchMore(); return }
      endSession()
      return
    }
    // Local mirrors — state won't update mid-function, and advance(10)/10x
    // playback processes every bar in ONE synchronous call, so both the position
    // AND the resting order must be mirrored or fills would read stale state.
    let curPos = pos
    let curOrder = pendingOrder
    const stop = Math.min(total, revealed + Math.max(1, n))
    for (let i = revealed; i < stop; i++) {
      const bar = bars[i] // the bar being revealed
      // GOAL 2: test the resting limit BEFORE the exit check. When it fills, the
      // SAME bar still runs checkExit on the new position (stop before target) —
      // a wide bar that spans both the limit and the stop exits immediately at
      // the stop, matching the engine's conservative worst-case convention.
      if (!curPos && curOrder) {
        const fill = checkLimitFill(curOrder, bar)
        if (fill) {
          curPos = openFromLimit(curOrder, bar.time, fill.price)
          curOrder = null
        }
      }
      if (!curPos) continue
      const ex = checkExit(curPos, bar) // stop checked before target (conservative)
      if (ex) {
        recordTrade(closePosition(curPos, ex.price, bar.time, ex.reason, pointValue))
        curPos = null
      }
    }
    setPos(curPos)
    setPendingOrder(curOrder)
    setRevealed(stop)
    // Prefetch the next chunk as we near the loaded end so a fast run rolls on
    // without stalling at the boundary.
    if (hasMoreForward && stop >= total - APPEND_BUFFER) maybeFetchMore()
    // True end only when the data is exhausted AND fully revealed.
    if (stop >= total && !hasMoreForward) {
      setPlaying(false)
      setDone(true)
      if (curPos) {
        const last = bars[total - 1]
        recordTrade(closePosition(curPos, last.close, last.time, 'session_end', pointValue))
        setPos(null)
      }
      if (curOrder) setPendingOrder(null) // unfilled resting order expires with the data
    }
  }

  // Flat-only rewind: scrub the reveal cursor BACK through revealed history.
  // Disabled while a position is open (re-advancing an open trade would
  // double-count its exit) and while a limit order rests (rewinding behind the
  // bars it was placed against is nonsense — cancel it first). Clears `done` so
  // you can rewind after the session ended, and pauses playback.
  const rewind = (n = 1) => {
    if (!day || pos || pendingOrder) return
    setDone(false)
    setPlaying(false)
    setRevealed((r) => Math.max(REWIND_FLOOR, Math.min(bars.length, r) - Math.max(1, n)))
  }

  // The interval must always call the LATEST advance (fresh state), so route
  // it through a ref that's reassigned every render.
  const stepRef = useRef(advance)
  stepRef.current = advance
  const rewindRef = useRef(rewind)
  rewindRef.current = rewind

  useEffect(() => {
    if (!playing) return
    const ms = Math.max(33, Math.round(1000 / speed))
    const id = setInterval(() => stepRef.current(), ms)
    return () => clearInterval(id)
  }, [playing, speed])

  // ── order actions ──────────────────────────────────────────────────────────

  const qty = Math.max(1, Math.floor(Number(qtyStr) || 0))
  const stopPtsNum = stopStr.trim() === '' ? null : Number(stopStr)
  const targetValNum = targetStr.trim() === '' ? null : Number(targetStr)
  const stopOk = stopPtsNum == null || (Number.isFinite(stopPtsNum) && stopPtsNum > 0)
  const targetOk = targetValNum == null || (Number.isFinite(targetValNum) && targetValNum > 0)
  // GOAL 2: a LIMIT ticket additionally needs a valid resting price.
  const limitNum = limitStr.trim() === '' ? null : Number(limitStr)
  const limitOk = orderType === 'market' || (limitNum != null && Number.isFinite(limitNum) && limitNum > 0)
  const ticketValid = qty >= 1 && stopOk && targetOk && limitOk

  const placeOrder = (direction: Direction) => {
    if (!lastBar || pos || pendingOrder || done || !ticketValid) return
    const target = targetValNum != null ? { kind: targetMode, value: targetValNum } : null
    if (orderType === 'limit' && limitNum != null) {
      // Rests until a revealed bar trades through it (checked in advance()).
      setPendingOrder(makePendingOrder(direction, qty, limitNum, stopPtsNum, target))
    } else {
      setPos(openPosition(direction, qty, lastBar, stopPtsNum, target))
    }
  }

  // Cancel button on the resting-order card (reset/end-of-data clear it themselves).
  const cancelPendingOrder = () => { setPendingOrder(null) }

  // Switching the ticket to LIMIT pre-fills the price with the last revealed
  // close so the field is never armed empty; MARKET clears the limit arm.
  const setTicketType = (t: 'market' | 'limit') => {
    setOrderType(t)
    if (t === 'limit') {
      if (limitStr.trim() === '' && lastBar) setLimitStr(lastBar.close.toFixed(2))
    } else {
      setArmed((a) => (a === 'limit' ? null : a))
    }
  }

  const manualClose = () => {
    if (!pos || !lastBar) return
    recordTrade(closePosition(pos, lastBar.close, lastBar.time, 'manual', pointValue))
    setPos(null)
  }

  // Keep the SL/TP editors in sync with the live position (new entry, a
  // place-on-chart set, or a cleared level). Guarded by the position identity +
  // its current levels so mid-typing edits aren't clobbered.
  useEffect(() => {
    setSlInput(pos?.stopPrice != null ? String(pos.stopPrice) : '')
    setTpInput(pos?.targetPrice != null ? String(pos.targetPrice) : '')
    if (!pos) setArmed(null)
  }, [pos?.entryTime, pos?.stopPrice, pos?.targetPrice])

  const commitSl = () => {
    const v = slInput.trim()
    if (v !== '' && (!Number.isFinite(Number(v)) || Number(v) <= 0)) {
      setSlInput(pos?.stopPrice != null ? String(pos.stopPrice) : '')
      return
    }
    setPos((p) => {
      if (!p) return p
      const nv = v === '' ? null : Number(v)
      return nv === p.stopPrice ? p : { ...p, stopPrice: nv }
    })
  }
  const commitTp = () => {
    const v = tpInput.trim()
    if (v !== '' && (!Number.isFinite(Number(v)) || Number(v) <= 0)) {
      setTpInput(pos?.targetPrice != null ? String(pos.targetPrice) : '')
      return
    }
    setPos((p) => {
      if (!p) return p
      const nv = v === '' ? null : Number(v)
      return nv === p.targetPrice ? p : { ...p, targetPrice: nv }
    })
  }
  const clearSl = () => { setSlInput(''); setPos((p) => (p && p.stopPrice != null ? { ...p, stopPrice: null } : p)) }
  const clearTp = () => { setTpInput(''); setPos((p) => (p && p.targetPrice != null ? { ...p, targetPrice: null } : p)) }

  // Place-on-chart: when armed, the next chart click sets that price. 'limit'
  // fills the LIMIT ticket's price field (flat, GOAL 2); 'sl'/'tp' edit the open
  // position's levels (GOAL G).
  const handleChartClick = (price: number) => {
    const a = armed
    if (!a) return
    if (a === 'limit') {
      setLimitStr(price.toFixed(2))
      setArmed(null)
      return
    }
    if (!pos) return
    setPos((p) => (p ? { ...p, [a === 'sl' ? 'stopPrice' : 'targetPrice']: price } : p))
    setArmed(null)
  }
  const handleContextMenu = (x: number, y: number) => setCtxMenu({ x, y })

  // ── drawing layer wiring ─────────────────────────────────────────────────────

  // Handed the engine controller on chart mount; re-apply the trader's last-used
  // default style + magnet so the palette opens where they left off.
  const handleDrawingApi = (api: DrawingApi | null) => {
    drawApiRef.current = api
    if (api) {
      const prefs = loadDrawPrefs()
      drawPrefsJsonRef.current = JSON.stringify(prefs)
      api.setDefaults(prefs.style)
      api.setMagnet(prefs.magnet)
    } else {
      setDrawState(null)
    }
  }
  // Reactive state → drive the palette; persist default style/magnet when they change.
  const handleDrawingState = (s: DrawingState) => {
    setDrawState(s)
    const json = JSON.stringify({ style: s.defaults, magnet: s.magnet })
    if (json !== drawPrefsJsonRef.current) {
      drawPrefsJsonRef.current = json
      saveDrawPrefs({ style: s.defaults, magnet: s.magnet })
    }
  }
  // Arming a drawing tool and arming SL/TP are mutually exclusive over the same
  // click surface: picking a drawing tool disarms SL/TP, and vice-versa.
  const selectDrawTool = (tool: DrawTool) => {
    drawApiRef.current?.setActiveTool(tool)
    if (tool !== 'cursor') setArmed(null)
  }
  const setDrawDefaults = (patch: Partial<DrawStyle>) => { drawApiRef.current?.setDefaults(patch) }
  const armLevel = (level: 'sl' | 'tp' | 'limit') => {
    setArmed((prev) => {
      const next = prev === level ? null : level
      if (next) drawApiRef.current?.setActiveTool('cursor') // leave any drawing tool
      return next
    })
  }

  // Esc disarms the place-on-chart mode.
  useEffect(() => {
    if (!armed) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setArmed(null) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [armed])

  // ── timeframe ───────────────────────────────────────────────────────────────

  const commitCustomTf = () => {
    const n = Math.floor(Number(customTf))
    if (Number.isFinite(n) && n >= 1 && n <= 240) setTf(n)
    setCustomTf('')
  }

  // ── fullscreen (GOAL E) ──────────────────────────────────────────────────────

  const toggleFullscreen = () => {
    const el = fsRef.current
    if (!el) return
    const fsEl = document.fullscreenElement || (document as any).webkitFullscreenElement
    if (!fsEl) {
      const req = el.requestFullscreen || (el as any).webkitRequestFullscreen
      req?.call(el)
    } else {
      const exit = document.exitFullscreen || (document as any).webkitExitFullscreen
      exit?.call(document)
    }
  }
  // Backtest-mode chart uses its own wrapper ref; the `fullscreen` flag is shared
  // (only one chart mounts at a time, so the icon state can't be ambiguous).
  const toggleBtFullscreen = () => {
    const el = btFsRef.current
    if (!el) return
    const fsEl = document.fullscreenElement || (document as any).webkitFullscreenElement
    if (!fsEl) {
      const req = el.requestFullscreen || (el as any).webkitRequestFullscreen
      req?.call(el)
    } else {
      const exit = document.exitFullscreen || (document as any).webkitExitFullscreen
      exit?.call(document)
    }
  }
  useEffect(() => {
    const onFs = () => setFullscreen(!!(document.fullscreenElement || (document as any).webkitFullscreenElement))
    document.addEventListener('fullscreenchange', onFs)
    document.addEventListener('webkitfullscreenchange', onFs)
    return () => {
      document.removeEventListener('fullscreenchange', onFs)
      document.removeEventListener('webkitfullscreenchange', onFs)
    }
  }, [])

  // ── context menu lifecycle (close on outside-click / Esc / scroll) ───────────

  useEffect(() => {
    if (!ctxMenu) return
    const outside = (e: Event) => {
      if (menuRef.current && e.target instanceof Node && menuRef.current.contains(e.target)) return
      setCtxMenu(null)
    }
    const closeNow = () => setCtxMenu(null)
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setCtxMenu(null) }
    window.addEventListener('mousedown', outside)
    window.addEventListener('scroll', closeNow, true)
    window.addEventListener('wheel', closeNow, true)
    window.addEventListener('resize', closeNow)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', outside)
      window.removeEventListener('scroll', closeNow, true)
      window.removeEventListener('wheel', closeNow, true)
      window.removeEventListener('resize', closeNow)
      window.removeEventListener('keydown', onKey)
    }
  }, [ctxMenu])

  const onMenuKey = (e: React.KeyboardEvent) => {
    const items = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])
    const idx = items.indexOf(document.activeElement as HTMLButtonElement)
    if (e.key === 'ArrowDown') { e.preventDefault(); items[(idx + 1) % items.length]?.focus() }
    else if (e.key === 'ArrowUp') { e.preventDefault(); items[(idx - 1 + items.length) % items.length]?.focus() }
  }

  // ── derived stats ────────────────────────────────────────────────────────────

  const stats = sessionStats(closed)
  const uPnl = pos && lastBar ? unrealized(pos, lastBar.close, pointValue) : null

  // All-time stats from the persisted log (GOAL D). Wins by dollars > 0; R sums
  // only over trades that carried a stop (tolerant of legacy null-r entries).
  const allTimeStats = useMemo(() => {
    const entries = readLog()
    const trades = entries.length
    const wins = entries.filter((e) => e.dollars > 0).length
    const rEntries = entries.filter((e) => e.r != null)
    const totalR = rEntries.reduce((a, e) => a + (e.r as number), 0)
    const totalDollars = entries.reduce((a, e) => a + e.dollars, 0)
    return { trades, wins, winRate: trades > 0 ? (wins / trades) * 100 : null, totalR, rCount: rEntries.length, totalDollars }
  }, [logVersion]) // eslint-disable-line react-hooks/exhaustive-deps

  // Recent sessions summary from the persisted log.
  const recentSessions = useMemo(() => {
    const entries = readLog()
    const bySession = new Map<string, { sid: string; instrument: string; date: string; trades: number; totalR: number; totalDollars: number; last: string }>()
    for (const e of entries) {
      const cur = bySession.get(e.session_id) || { sid: e.session_id, instrument: e.instrument, date: e.date, trades: 0, totalR: 0, totalDollars: 0, last: e.logged_at }
      cur.trades += 1
      cur.totalR += e.r ?? 0
      cur.totalDollars += e.dollars
      if (e.logged_at > cur.last) cur.last = e.logged_at
      bySession.set(e.session_id, cur)
    }
    return Array.from(bySession.values()).sort((a, b) => (a.last < b.last ? 1 : -1)).slice(0, 5)
    // logVersion re-derives this after each recorded trade
  }, [logVersion]) // eslint-disable-line react-hooks/exhaustive-deps

  const exportLog = () => {
    const blob = new Blob([JSON.stringify(readLog(), null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'theta_replay_log.json'
    a.click()
    URL.revokeObjectURL(url)
  }

  const dateHidden = blind && !done
  const progressPct = bars.length > 0 ? Math.round((revealed / bars.length) * 100) : 0
  const tzShort = TZ_SHORT[settings.timezone] ?? ''

  // Context-menu position, clamped to the viewport.
  const MENU_W = 210, MENU_H = 292
  const mx = ctxMenu ? Math.max(6, Math.min(ctxMenu.x, (typeof window !== 'undefined' ? window.innerWidth : 1200) - MENU_W)) : 0
  const my = ctxMenu ? Math.max(6, Math.min(ctxMenu.y, (typeof window !== 'undefined' ? window.innerHeight : 800) - MENU_H)) : 0

  // ── render ────────────────────────────────────────────────────────────────

  const chipBase = 'px-2.5 py-1 rounded-md text-xs font-bold transition-all'
  const chipOn = 'bg-white dark:bg-slate-700 text-violet-700 dark:text-violet-300 shadow-sm'
  const chipOff = 'text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200'
  const iconBtn = 'inline-flex items-center justify-center h-8 w-8 rounded-lg text-slate-600 dark:text-slate-300 bg-slate-200 hover:bg-slate-300 dark:bg-slate-800 dark:hover:bg-slate-700 transition-colors'
  const inputCls = 'rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 text-sm px-2 py-1.5'
  const fieldLabel = 'text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500'

  return (
    <div className="p-4 sm:p-8 max-w-screen-2xl">
      {/* HERO */}
      <div className="rounded-3xl bg-gradient-to-br from-slate-900 via-slate-900 to-violet-950 dark:from-slate-950 dark:via-slate-950 dark:to-violet-950 text-white p-6 md:p-8 shadow-xl mb-6">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="min-w-0 flex-1">
            <div className="text-[10px] uppercase tracking-[0.2em] text-violet-300 font-bold mb-1">{mode === 'practice' ? 'Practice' : 'Research'}</div>
            <h1 className="text-2xl md:text-3xl font-extrabold text-white flex items-center gap-2.5">
              {mode === 'backtest' && <FlaskConical size={26}/>} Replay
            </h1>
            <p className="text-sm text-slate-400 mt-1">
              {mode === 'practice'
                ? 'Bar-by-bar practice trading on historical futures days · stop checked before target, fills at level'
                : 'Pick a strategy and a date range — the engine backtests the whole span at once and plots every trade on the chart'}
            </p>
          </div>
          <div className="flex flex-col items-stretch sm:items-end gap-3">
            {/* MODE TOGGLE (item 1) */}
            <div className="inline-flex rounded-xl bg-white/10 p-0.5 border border-white/10 self-start sm:self-auto">
              <button onClick={() => switchMode('practice')}
                className={`px-3 py-1.5 rounded-lg text-xs font-bold transition-colors inline-flex items-center gap-1.5 ${mode === 'practice' ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-300 hover:text-white'}`}>
                <Rewind size={13}/> Bar Replay
              </button>
              <button onClick={() => switchMode('backtest')}
                className={`px-3 py-1.5 rounded-lg text-xs font-bold transition-colors inline-flex items-center gap-1.5 ${mode === 'backtest' ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-300 hover:text-white'}`}>
                <FlaskConical size={13}/> Range Backtest
              </button>
            </div>
            {mode === 'practice' && (
              <button onClick={() => loadRandomDay()} disabled={loadState === 'loading'}
                className="inline-flex items-center justify-center gap-2 bg-violet-600 hover:bg-violet-500 disabled:opacity-50 text-white px-4 py-2 rounded-xl text-sm font-bold transition-colors shadow-lg shadow-violet-900/30">
                <Shuffle size={15}/> Random day
              </button>
            )}
          </div>
        </div>
      </div>

      {/* ══ PRACTICE (bar-by-bar replay) ══════════════════════════════════ */}
      {mode === 'practice' && (<>
      {/* CONTROLS ROW */}
      <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 dark:bg-slate-900 dark:border-slate-700 mb-4">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">Instrument</span>
            <select value={instrument} onChange={(e) => setInstrument(e.target.value)}
              className="rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 text-sm px-2 py-1.5">
              {instruments.map((i) => <option key={i} value={i}>{i}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">Date</span>
            <input type="date" value={dateHidden ? '' : date}
              min={meta?.min_date} max={meta?.max_date}
              disabled={dateHidden}
              onChange={(e) => { if (e.target.value) loadContinuous(instrument, e.target.value, false) }}
              className="rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 text-sm px-2 py-1.5 disabled:opacity-60"/>
          </label>
          {dateHidden && (
            <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-violet-600 dark:text-violet-300 bg-violet-100 dark:bg-violet-900/30 rounded-lg px-2 py-1.5">
              <EyeOff size={12}/> Blind mode — date revealed at session end
            </span>
          )}
        </div>
      </div>

      {/* STATS STRIP (GOAL D) */}
      {day && (
        <div className="flex flex-wrap items-stretch gap-3 mb-4">
          <div className="flex-1 min-w-[280px] rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 p-3">
            <div className="text-[10px] uppercase tracking-wider font-bold text-violet-500 dark:text-violet-300 mb-2">This session</div>
            <div className="flex flex-wrap gap-2">
              <StatChip label="Trades" value={String(stats.trades)} tone={stats.trades === 0 ? 'muted' : undefined}/>
              <StatChip label="Win rate" value={stats.winRate == null ? '—' : `${stats.winRate.toFixed(0)}%`}
                tone={stats.winRate == null ? 'muted' : stats.winRate >= 50 ? 'pos' : 'neg'}/>
              <StatChip label="Total R" value={stats.rCount === 0 ? '—' : fmtR(stats.totalR)}
                note={stats.rCount > 0 && stats.rCount < stats.trades ? `${stats.rCount} w/ SL` : undefined}
                tone={stats.rCount === 0 ? 'muted' : stats.totalR >= 0 ? 'pos' : 'neg'}/>
              <StatChip label="Total $" value={stats.trades === 0 ? '—' : fmtUsd(stats.totalDollars)}
                tone={stats.trades === 0 ? 'muted' : stats.totalDollars >= 0 ? 'pos' : 'neg'}/>
            </div>
          </div>
          <div className="flex-1 min-w-[280px] rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 p-3">
            <div className="text-[10px] uppercase tracking-wider font-bold text-slate-500 dark:text-slate-400 mb-2">All time</div>
            <div className="flex flex-wrap gap-2">
              <StatChip label="Trades" value={String(allTimeStats.trades)} tone={allTimeStats.trades === 0 ? 'muted' : undefined}/>
              <StatChip label="Win rate" value={allTimeStats.winRate == null ? '—' : `${allTimeStats.winRate.toFixed(0)}%`}
                tone={allTimeStats.winRate == null ? 'muted' : allTimeStats.winRate >= 50 ? 'pos' : 'neg'}/>
              <StatChip label="Total R" value={allTimeStats.rCount === 0 ? '—' : fmtR(allTimeStats.totalR)}
                note={allTimeStats.rCount > 0 && allTimeStats.rCount < allTimeStats.trades ? `${allTimeStats.rCount} w/ SL` : undefined}
                tone={allTimeStats.rCount === 0 ? 'muted' : allTimeStats.totalR >= 0 ? 'pos' : 'neg'}/>
              <StatChip label="Total $" value={allTimeStats.trades === 0 ? '—' : fmtUsd(allTimeStats.totalDollars)}
                tone={allTimeStats.trades === 0 ? 'muted' : allTimeStats.totalDollars >= 0 ? 'pos' : 'neg'}/>
            </div>
          </div>
        </div>
      )}

      {/* CHART */}
      {loadState === 'loading' && (
        <div className="flex items-center justify-center h-72 rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 mb-4">
          <Loader2 className="animate-spin text-slate-400"/>
        </div>
      )}
      {loadState === 'holiday' && (
        <div className="flex flex-col items-center justify-center gap-2 h-72 rounded-xl border border-dashed border-slate-300 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 mb-4 text-center px-4">
          <CalendarOff className="text-slate-400" size={28}/>
          <div className="text-sm font-semibold text-slate-600 dark:text-slate-300">No bars for that date</div>
          <div className="text-xs text-slate-400 dark:text-slate-500">Probably a market holiday or weekend — pick another date or hit Random day.</div>
        </div>
      )}
      {loadState === 'error' && (
        <div className="flex flex-col items-center justify-center gap-2 h-72 rounded-xl border border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/20 mb-4 text-center px-4">
          <AlertTriangle className="text-red-400" size={28}/>
          <div className="text-sm font-semibold text-red-600 dark:text-red-400">Couldn't load bars</div>
          <div className="text-xs text-slate-500 dark:text-slate-400 break-all">{loadError}</div>
        </div>
      )}
      {loadState === 'idle' && !day && (
        <div className="flex flex-col items-center justify-center gap-2 h-72 rounded-xl border border-dashed border-slate-300 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 mb-4 text-center px-4">
          <Rewind className="text-violet-400" size={28}/>
          <div className="text-sm font-semibold text-slate-600 dark:text-slate-300">Pick a date or hit Random day to start</div>
          <div className="text-xs text-slate-400 dark:text-slate-500">Bars replay one at a time — trade it like it's live.</div>
        </div>
      )}
      {day && (
        <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_340px] gap-4 mb-4 items-start">
          {/* CHART CARD — FX-Replay style: TradingView chart + attached replay toolbar */}
          <div ref={fsRef}
            style={settings.background ? { background: settings.background } : undefined}
            className={`relative rounded-xl border border-slate-200 dark:border-slate-700 overflow-hidden bg-white dark:bg-[#131722] ${fullscreen ? 'flex flex-col' : ''}`}>
            <div className="flex flex-wrap items-center gap-2 px-2.5 py-2 border-b border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900">
              {/* timeframe chips + custom minutes (chart-only; sim still steps 1m) */}
              <div className="inline-flex rounded-lg bg-slate-100 dark:bg-slate-800 p-0.5 border border-slate-200 dark:border-slate-700">
                {TF_CHIPS.map((t) => (
                  <button key={t} onClick={() => setTf(t)} className={`${chipBase} ${tf === t ? chipOn : chipOff}`}>
                    {tfLabel(t)}
                  </button>
                ))}
                {!TF_CHIPS.includes(tf) && (
                  <button className={`${chipBase} ${chipOn}`} title="Custom timeframe">{tfLabel(tf)}</button>
                )}
              </div>
              <div className="inline-flex items-center">
                <input type="number" min={1} max={240} value={customTf}
                  onChange={(e) => setCustomTf(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') commitCustomTf() }}
                  onBlur={commitCustomTf}
                  placeholder="min" title="Custom timeframe in minutes (1–240)"
                  className="w-14 rounded-l-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 text-xs px-2 py-1.5"/>
                <button onClick={commitCustomTf} title="Apply custom timeframe"
                  className="inline-flex items-center justify-center px-1.5 py-1.5 rounded-r-lg border border-l-0 border-slate-300 dark:border-slate-600 bg-slate-200 hover:bg-slate-300 dark:bg-slate-700 dark:hover:bg-slate-600 text-slate-700 dark:text-slate-200">
                  <Plus size={13}/>
                </button>
              </div>
              <div className="h-5 w-px bg-slate-200 dark:bg-slate-700"/>
              <button onClick={() => setPlaying((p) => !p)} disabled={done}
                className="inline-flex items-center gap-1.5 bg-violet-600 hover:bg-violet-500 disabled:opacity-40 text-white px-3 py-1.5 rounded-lg text-xs font-bold transition-colors">
                {playing ? <Pause size={13}/> : <Play size={13}/>}
                {playing ? 'Pause' : 'Play'}
              </button>
              <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))} title="Bars per second"
                className="rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 text-xs font-bold px-1.5 py-1.5">
                {SPEEDS.map((s) => <option key={s} value={s}>{s}x</option>)}
              </select>
              {/* flat-only rewind (look-back scrub) — disabled with a position open
                  or a limit order resting (cancel it first) */}
              <button onClick={() => rewindRef.current(10)} disabled={!!pos || !!pendingOrder || playing || revealed <= REWIND_FLOOR}
                title={pos ? 'Close the position to rewind' : pendingOrder ? 'Cancel the resting order to rewind' : 'Rewind 10 bars'}
                className="inline-flex items-center gap-1 bg-slate-200 hover:bg-slate-300 dark:bg-slate-800 dark:hover:bg-slate-700 disabled:opacity-40 text-slate-700 dark:text-slate-200 px-2.5 py-1.5 rounded-lg text-xs font-bold transition-colors">
                <Rewind size={13}/> -10
              </button>
              <button onClick={() => rewindRef.current(1)} disabled={!!pos || !!pendingOrder || playing || revealed <= REWIND_FLOOR}
                title={pos ? 'Close the position to rewind' : pendingOrder ? 'Cancel the resting order to rewind' : 'Rewind 1 bar'}
                className="inline-flex items-center gap-1 bg-slate-200 hover:bg-slate-300 dark:bg-slate-800 dark:hover:bg-slate-700 disabled:opacity-40 text-slate-700 dark:text-slate-200 px-2.5 py-1.5 rounded-lg text-xs font-bold transition-colors">
                <Rewind size={13}/> -1
              </button>
              <button onClick={() => stepRef.current(1)} disabled={done || playing} title="Step 1 bar"
                className="inline-flex items-center gap-1 bg-slate-200 hover:bg-slate-300 dark:bg-slate-800 dark:hover:bg-slate-700 disabled:opacity-40 text-slate-700 dark:text-slate-200 px-2.5 py-1.5 rounded-lg text-xs font-bold transition-colors">
                <StepForward size={13}/> +1
              </button>
              <button onClick={() => stepRef.current(10)} disabled={done || playing} title="Step 10 bars"
                className="inline-flex items-center gap-1 bg-slate-200 hover:bg-slate-300 dark:bg-slate-800 dark:hover:bg-slate-700 disabled:opacity-40 text-slate-700 dark:text-slate-200 px-2.5 py-1.5 rounded-lg text-xs font-bold transition-colors">
                <FastForward size={13}/> +10
              </button>
              <div className="h-5 w-px bg-slate-200 dark:bg-slate-700"/>
              {/* ETH/RTH (GOAL F) */}
              <button onClick={toggleEth} title={eth ? 'Showing overnight (ETH) — click for RTH only' : 'Showing RTH only — click for overnight (ETH)'}
                className={`px-2.5 py-1.5 rounded-lg text-xs font-bold transition-colors ${eth ? 'bg-violet-600 text-white hover:bg-violet-500' : 'bg-slate-200 hover:bg-slate-300 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-200'}`}>
                {eth ? 'ETH' : 'RTH'}
              </button>
              {/* session shading quick toggle (GOAL B) */}
              <button onClick={() => updateSettings({ sessionsEnabled: !settings.sessionsEnabled })} title="Toggle session shading"
                className={`inline-flex items-center justify-center h-8 w-8 rounded-lg transition-colors ${settings.sessionsEnabled ? 'bg-violet-600 text-white hover:bg-violet-500' : iconBtn}`}>
                <Layers size={14}/>
              </button>
              {/* settings gear (GOAL A) */}
              <button onClick={() => openSettings('appearance')} title="Chart settings" className={iconBtn}>
                <SettingsIcon size={14}/>
              </button>
              {/* fullscreen (GOAL E) */}
              <button onClick={toggleFullscreen} title={fullscreen ? 'Exit fullscreen' : 'Fullscreen'} className={iconBtn}>
                {fullscreen ? <Minimize2 size={14}/> : <Maximize2 size={14}/>}
              </button>
              {/* end the session on-demand (multi-day replay never auto-ends per day) */}
              <button onClick={endSession} disabled={done} title="End session & show summary" className={iconBtn}>
                <Flag size={14}/>
              </button>
              {lastBar && (
                <div className="ml-auto text-right leading-tight">
                  <div className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">
                    {dateHidden ? 'Hidden day' : etDateOf(lastBar.time)} · {zonedClock(lastBar.time, settings.timezone, settings.hour12)} {tzShort}
                  </div>
                  <div className="text-xs font-extrabold text-slate-800 dark:text-slate-100">
                    {lastBar.close.toFixed(2)} <span className="text-[10px] font-semibold text-slate-400">({progressPct}% loaded{hasMoreForward ? '+' : ''})</span>
                  </div>
                </div>
              )}
            </div>
            <div className={`relative ${fullscreen ? 'flex-1 min-h-0' : 'h-[380px] md:h-[62vh] md:max-h-[620px]'}`}>
              <TVReplayChart
                instrument={activeInstrument}
                bars={revealedBars}
                displayTf={tf}
                resetKey={`${sessionIdRef.current}|${chartNonce}`}
                showDate={!dateHidden}
                pdh={day.pdh}
                pdl={day.pdl}
                position={pos}
                pendingOrder={pendingOrder}
                trades={closed}
                showGrid={settings.showGrid}
                upColor={settings.upColor}
                downColor={settings.downColor}
                background={settings.background}
                sessionsEnabled={settings.sessionsEnabled}
                sessionVisibility={settings.sessionVisibility as SessionVisibility}
                timezone={resolveTz(settings.timezone)}
                hour12={settings.hour12}
                onChartClick={handleChartClick}
                onContextMenu={handleContextMenu}
                onDrawingApi={handleDrawingApi}
                onDrawingState={handleDrawingState}
              />
              {/* FX-Replay-style drawing palette + style bar (overlays the chart,
                  visible in fullscreen; collapses to a top sheet on mobile). */}
              <DrawingToolbar
                state={drawState}
                api={drawApiRef.current}
                onSelectTool={selectDrawTool}
                onSetDefaults={setDrawDefaults}
              />
            </div>

            {/* place-on-chart armed banner (GOAL G) */}
            {armed && (
              <div className="absolute left-1/2 -translate-x-1/2 top-2 z-[80] flex items-center gap-2 rounded-lg bg-violet-600 text-white text-xs font-bold px-3 py-1.5 shadow-lg">
                <Crosshair size={13}/> Click the chart to set {armed === 'sl' ? 'SL' : armed === 'tp' ? 'TP' : 'the limit price'}
                <button onClick={() => setArmed(null)} className="ml-1 hover:opacity-80" title="Cancel (Esc)"><X size={12}/></button>
              </div>
            )}

            {/* right-click context menu (GOAL H) — inside the fullscreen element */}
            {ctxMenu && (
              <div ref={menuRef} role="menu" tabIndex={-1} onKeyDown={onMenuKey}
                style={{ position: 'fixed', left: mx, top: my, zIndex: 100 }}
                className="min-w-[190px] rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 shadow-xl py-1 text-sm">
                <MenuItem icon={SettingsIcon} label="Settings…" autoFocus onClick={() => { openSettings('appearance'); setCtxMenu(null) }}/>
                <MenuItem icon={Clock} label="Timezone & time…" onClick={() => { openSettings('time'); setCtxMenu(null) }}/>
                <MenuItem icon={Check} label="12-hour clock" active={settings.hour12} onClick={() => { updateSettings({ hour12: !settings.hour12 }); setCtxMenu(null) }}/>
                <MenuItem icon={Grid3x3} label="Show grid" active={settings.showGrid} onClick={() => { updateSettings({ showGrid: !settings.showGrid }); setCtxMenu(null) }}/>
                {(drawState?.selection || (drawState && drawState.count > 0)) && (
                  <div className="my-1 h-px bg-slate-200 dark:bg-slate-700"/>
                )}
                {drawState?.selection && (
                  <MenuItem icon={Trash2} label="Delete drawing" onClick={() => { drawApiRef.current?.deleteSelected(); setCtxMenu(null) }}/>
                )}
                {drawState && drawState.count > 0 && (
                  <MenuItem icon={Eraser} label="Clear all drawings"
                    onClick={() => { if (window.confirm(`Delete all ${drawState.count} drawing${drawState.count === 1 ? '' : 's'} on this chart?`)) drawApiRef.current?.clearAll(); setCtxMenu(null) }}/>
                )}
                <div className="my-1 h-px bg-slate-200 dark:bg-slate-700"/>
                <MenuItem icon={RotateCcw} label="Reset chart view" onClick={() => { setChartNonce((n) => n + 1); setCtxMenu(null) }}/>
              </div>
            )}

            {/* settings panel (GOAL A/B/I) — inside the fullscreen element */}
            {settingsOpen && (
              <div className="fixed inset-0 z-[95]">
                <div className="absolute inset-0 bg-black/30 dark:bg-black/50" onMouseDown={() => setSettingsOpen(false)}/>
                <div className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-[92vw] sm:w-[440px] max-h-[86vh] flex flex-col rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 shadow-2xl">
                  <div className="flex items-center justify-between px-4 pt-3 pb-1">
                    <div className="text-sm font-extrabold text-slate-800 dark:text-slate-100 flex items-center gap-2"><SettingsIcon size={15}/> Chart settings</div>
                    <button onClick={() => setSettingsOpen(false)} className="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200" title="Close"><X size={16}/></button>
                  </div>
                  <div className="flex gap-1 px-3 pt-1 border-b border-slate-200 dark:border-slate-700">
                    {([['appearance', 'Appearance'], ['sessions', 'Sessions'], ['time', 'Time']] as [SettingsTab, string][]).map(([id, lbl]) => (
                      <button key={id} onClick={() => setSettingsTab(id)}
                        className={`px-3 py-2 text-xs font-bold border-b-2 -mb-px transition-colors ${settingsTab === id ? 'border-violet-500 text-violet-600 dark:text-violet-300' : 'border-transparent text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200'}`}>
                        {lbl}
                      </button>
                    ))}
                  </div>
                  <div className="p-4 overflow-y-auto">
                    {settingsTab === 'appearance' && (
                      <div className="space-y-4">
                        <div>
                          <div className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500 mb-2">Presets</div>
                          <div className="flex flex-wrap gap-2">
                            {COLOR_PRESETS.map((p) => {
                              const on = settings.upColor === p.up && settings.downColor === p.down
                              return (
                                <button key={p.name} onClick={() => updateSettings({ upColor: p.up, downColor: p.down })}
                                  className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-xs font-bold transition-colors ${on ? 'border-violet-400 bg-violet-50 dark:bg-violet-900/20 text-violet-700 dark:text-violet-300' : 'border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800'}`}>
                                  <span className="flex overflow-hidden rounded">
                                    <span style={{ background: p.up }} className="w-3 h-3"/>
                                    <span style={{ background: p.down }} className="w-3 h-3"/>
                                  </span>
                                  {p.name}
                                </button>
                              )
                            })}
                          </div>
                        </div>
                        <div className="grid grid-cols-2 gap-3">
                          <ColorField label="Up color" value={settings.upColor} onChange={(v) => updateSettings({ upColor: v })}/>
                          <ColorField label="Down color" value={settings.downColor} onChange={(v) => updateSettings({ downColor: v })}/>
                        </div>
                        <div>
                          <div className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500 mb-1">Background</div>
                          <div className="flex flex-wrap items-center gap-3">
                            <ColorField value={settings.background ?? (isDark ? '#131722' : '#ffffff')} onChange={(v) => updateSettings({ background: v })}/>
                            <label className="flex items-center gap-1.5 text-xs font-semibold text-slate-600 dark:text-slate-300">
                              <input type="checkbox" checked={settings.background == null}
                                onChange={(e) => updateSettings({ background: e.target.checked ? null : (isDark ? '#131722' : '#ffffff') })}/>
                              Use theme default
                            </label>
                          </div>
                        </div>
                        {/* GOAL 1: background grid on/off (applied live to both charts) */}
                        <div className="flex items-center justify-between">
                          <span className="text-sm font-bold text-slate-800 dark:text-slate-100 flex items-center gap-2"><Grid3x3 size={14} className="text-slate-400"/> Show grid</span>
                          <Toggle checked={settings.showGrid} onChange={(v) => updateSettings({ showGrid: v })}/>
                        </div>
                        <button onClick={() => updateSettings({ upColor: DEFAULT_SETTINGS.upColor, downColor: DEFAULT_SETTINGS.downColor, background: null, showGrid: true })}
                          className="inline-flex items-center gap-1.5 text-xs font-bold text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200">
                          <RotateCcw size={13}/> Reset to defaults
                        </button>
                      </div>
                    )}
                    {settingsTab === 'sessions' && (
                      <div className="space-y-3">
                        <div className="flex items-center justify-between">
                          <span className="text-sm font-bold text-slate-800 dark:text-slate-100">Session shading</span>
                          <Toggle checked={settings.sessionsEnabled} onChange={(v) => updateSettings({ sessionsEnabled: v })}/>
                        </div>
                        <p className="text-[11px] text-slate-400 dark:text-slate-500">Translucent bands mark each trading session. Asia &amp; London only appear when overnight (ETH) bars are loaded.</p>
                        <div className="space-y-1.5">
                          {SESSION_KEYS.map(({ key, label, hint }) => (
                            <label key={key} className={`flex items-center justify-between rounded-lg px-2.5 py-2 border border-slate-200 dark:border-slate-700 ${settings.sessionsEnabled ? '' : 'opacity-50'}`}>
                              <span className="text-xs text-slate-700 dark:text-slate-200"><b>{label}</b> <span className="text-[10px] text-slate-400 dark:text-slate-500">{hint}</span></span>
                              <input type="checkbox" disabled={!settings.sessionsEnabled} checked={settings.sessionVisibility[key]}
                                onChange={(e) => updateSettings({ sessionVisibility: { ...settings.sessionVisibility, [key]: e.target.checked } })}/>
                            </label>
                          ))}
                        </div>
                      </div>
                    )}
                    {settingsTab === 'time' && (
                      <div className="space-y-4">
                        <div>
                          <div className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500 mb-1">Timezone</div>
                          <select value={settings.timezone} onChange={(e) => updateSettings({ timezone: e.target.value })}
                            className="w-full rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 text-sm px-2 py-1.5">
                            {TIMEZONES.map((z) => <option key={z.value} value={z.value}>{z.label}</option>)}
                          </select>
                          <p className="text-[11px] text-slate-400 dark:text-slate-500 mt-1">Only changes the time labels — candles, sessions and levels never move.</p>
                        </div>
                        <div className="flex items-center justify-between">
                          <span className="text-sm font-bold text-slate-800 dark:text-slate-100">Time format</span>
                          <div className="inline-flex rounded-lg bg-slate-100 dark:bg-slate-800 p-0.5 border border-slate-200 dark:border-slate-700">
                            <button onClick={() => updateSettings({ hour12: false })} className={`${chipBase} ${!settings.hour12 ? chipOn : chipOff}`}>24h</button>
                            <button onClick={() => updateSettings({ hour12: true })} className={`${chipBase} ${settings.hour12 ? chipOn : chipOff}`}>12h</button>
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* TRADE PANEL — right column on xl, stacked below the chart otherwise */}
          <div className="flex flex-col gap-4 min-w-0">
            {!done && (
          <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 dark:bg-slate-900 dark:border-slate-700">
            <div className="text-[11px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500 mb-3">
              Order ticket · {orderType === 'market' ? 'fills at current close' : 'rests until a bar touches it'}
            </div>
            <div className="flex flex-wrap items-end gap-3">
              {/* GOAL 2: MARKET | LIMIT toggle */}
              <div className="flex flex-col gap-1">
                <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">Type</span>
                <div className="inline-flex rounded-lg bg-slate-100 dark:bg-slate-800 p-0.5 border border-slate-200 dark:border-slate-700">
                  <button onClick={() => setTicketType('market')} className={`${chipBase} ${orderType === 'market' ? chipOn : chipOff}`}>MKT</button>
                  <button onClick={() => setTicketType('limit')} className={`${chipBase} ${orderType === 'limit' ? chipOn : chipOff}`}>LMT</button>
                </div>
              </div>
              <label className="flex flex-col gap-1">
                <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">Contracts</span>
                <input type="number" min={1} step={1} value={qtyStr} onChange={(e) => setQtyStr(e.target.value)}
                  className="w-20 rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 text-sm px-2 py-1.5"/>
              </label>
              {orderType === 'limit' && (
                <div className="flex flex-col gap-1">
                  <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">Limit price</span>
                  <div className="flex gap-1">
                    <input type="number" min={0.25} step={0.25} value={limitStr} placeholder="price"
                      onChange={(e) => setLimitStr(e.target.value)}
                      className="w-28 rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 text-sm px-2 py-1.5"/>
                    <button onClick={() => armLevel('limit')} title="Set the limit price by clicking the chart"
                      className={`shrink-0 inline-flex items-center justify-center h-8 w-8 rounded-lg transition-colors ${armed === 'limit' ? 'bg-violet-600 text-white' : 'bg-slate-200 hover:bg-slate-300 dark:bg-slate-700 dark:hover:bg-slate-600 text-slate-600 dark:text-slate-300'}`}>
                      <Crosshair size={13}/>
                    </button>
                  </div>
                </div>
              )}
              <label className="flex flex-col gap-1">
                <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">Stop (pts)</span>
                <input type="number" min={0.25} step={0.25} value={stopStr} onChange={(e) => setStopStr(e.target.value)} placeholder="optional"
                  className="w-24 rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 text-sm px-2 py-1.5"/>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">Target</span>
                <div className="flex gap-1">
                  <input type="number" min={0.25} step={0.25} value={targetStr} onChange={(e) => setTargetStr(e.target.value)} placeholder="optional"
                    className="w-20 rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 text-sm px-2 py-1.5"/>
                  <select value={targetMode} onChange={(e) => setTargetMode(e.target.value as 'r' | 'points')}
                    className="rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 text-sm px-2 py-1.5">
                    <option value="r">R</option>
                    <option value="points">pts</option>
                  </select>
                </div>
              </label>
              <div className="flex gap-2">
                <button onClick={() => placeOrder('long')} disabled={!!pos || !!pendingOrder || !ticketValid || !lastBar}
                  className="inline-flex items-center gap-1.5 bg-green-600 hover:bg-green-500 disabled:opacity-40 text-white px-4 py-1.5 rounded-lg text-sm font-bold transition-colors">
                  <TrendingUp size={14}/> Buy
                </button>
                <button onClick={() => placeOrder('short')} disabled={!!pos || !!pendingOrder || !ticketValid || !lastBar}
                  className="inline-flex items-center gap-1.5 bg-red-600 hover:bg-red-500 disabled:opacity-40 text-white px-4 py-1.5 rounded-lg text-sm font-bold transition-colors">
                  <TrendingDown size={14}/> Sell
                </button>
              </div>
            </div>
            <div className="text-[10px] text-slate-400 dark:text-slate-500 mt-2">
              {orderType === 'market'
                ? `${activeInstrument} point value $${pointValue}/contract · SL/TP optional — leave blank for a bare market order and set them on the chart later`
                : `${activeInstrument} point value $${pointValue}/contract · one resting order at a time · Buy fills when a bar trades DOWN to the limit, Sell when a bar trades UP to it · unfilled orders expire at session end`}
            </div>
          </div>
            )}

            {!done && (
          <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 dark:bg-slate-900 dark:border-slate-700">
            <div className="text-[11px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500 mb-3">{!pos && pendingOrder ? 'Resting order' : 'Open position'}</div>
            {pos && uPnl ? (
              <div className="space-y-3">
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
                  <span className={`inline-flex items-center gap-1 text-sm font-extrabold ${pos.direction === 'long' ? 'text-green-600' : 'text-red-500'}`}>
                    {pos.direction === 'long' ? <TrendingUp size={14}/> : <TrendingDown size={14}/>}
                    {pos.direction.toUpperCase()} ×{pos.qty}
                  </span>
                  <span className="text-xs text-slate-500 dark:text-slate-400">Entry <b className="text-slate-800 dark:text-slate-100">{pos.entryPrice.toFixed(2)}</b></span>
                  <span className={`text-sm font-extrabold ${uPnl.dollars >= 0 ? 'text-green-600' : 'text-red-500'}`}>
                    {fmtPts(uPnl.points)} pts · {uPnl.r != null ? fmtR(uPnl.r) : 'no SL'} · {fmtUsd(uPnl.dollars)}
                  </span>
                </div>
                {/* editable SL / TP prices (GOAL G) */}
                <div className="grid grid-cols-2 gap-2">
                  <div className="flex flex-col gap-1">
                    <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">SL price</span>
                    <div className="flex gap-1">
                      <input type="number" step={0.25} value={slInput} placeholder="optional"
                        onChange={(e) => setSlInput(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
                        onBlur={commitSl}
                        className="w-full min-w-0 rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 text-sm px-2 py-1.5"/>
                      <button onClick={() => armLevel('sl')} title="Set SL by clicking the chart"
                        className={`shrink-0 inline-flex items-center justify-center h-8 w-8 rounded-lg transition-colors ${armed === 'sl' ? 'bg-violet-600 text-white' : 'bg-slate-200 hover:bg-slate-300 dark:bg-slate-700 dark:hover:bg-slate-600 text-slate-600 dark:text-slate-300'}`}>
                        <Crosshair size={13}/>
                      </button>
                      {pos.stopPrice != null && (
                        <button onClick={clearSl} title="Clear SL"
                          className="shrink-0 inline-flex items-center justify-center h-8 w-8 rounded-lg bg-slate-200 hover:bg-slate-300 dark:bg-slate-700 dark:hover:bg-slate-600 text-slate-500 dark:text-slate-300"><X size={13}/></button>
                      )}
                    </div>
                  </div>
                  <div className="flex flex-col gap-1">
                    <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">TP price</span>
                    <div className="flex gap-1">
                      <input type="number" step={0.25} value={tpInput} placeholder="optional"
                        onChange={(e) => setTpInput(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
                        onBlur={commitTp}
                        className="w-full min-w-0 rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 text-sm px-2 py-1.5"/>
                      <button onClick={() => armLevel('tp')} title="Set TP by clicking the chart"
                        className={`shrink-0 inline-flex items-center justify-center h-8 w-8 rounded-lg transition-colors ${armed === 'tp' ? 'bg-violet-600 text-white' : 'bg-slate-200 hover:bg-slate-300 dark:bg-slate-700 dark:hover:bg-slate-600 text-slate-600 dark:text-slate-300'}`}>
                        <Crosshair size={13}/>
                      </button>
                      {pos.targetPrice != null && (
                        <button onClick={clearTp} title="Clear TP"
                          className="shrink-0 inline-flex items-center justify-center h-8 w-8 rounded-lg bg-slate-200 hover:bg-slate-300 dark:bg-slate-700 dark:hover:bg-slate-600 text-slate-500 dark:text-slate-300"><X size={13}/></button>
                      )}
                    </div>
                  </div>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[10px] text-slate-400 dark:text-slate-500">Edits apply from the next bar</span>
                  <button onClick={manualClose}
                    className="inline-flex items-center gap-1 bg-slate-200 hover:bg-slate-300 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-200 px-3 py-1.5 rounded-lg text-xs font-bold transition-colors">
                    <X size={12}/> Close
                  </button>
                </div>
              </div>
            ) : pendingOrder ? (
              // GOAL 2: resting limit order card — blind-safe (prices/qty only, no dates).
              <div className="space-y-3">
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
                  <span className={`inline-flex items-center gap-1 text-sm font-extrabold ${pendingOrder.direction === 'long' ? 'text-green-600' : 'text-red-500'}`}>
                    {pendingOrder.direction === 'long' ? <TrendingUp size={14}/> : <TrendingDown size={14}/>}
                    {pendingOrder.direction.toUpperCase()} LMT ×{pendingOrder.qty}
                  </span>
                  <span className="text-xs text-slate-500 dark:text-slate-400">Limit <b className="text-slate-800 dark:text-slate-100">{pendingOrder.limitPrice.toFixed(2)}</b></span>
                  {pendingOrder.stopPrice != null && (
                    <span className="text-xs text-slate-500 dark:text-slate-400">SL <b className="text-red-500">{pendingOrder.stopPrice.toFixed(2)}</b></span>
                  )}
                  {pendingOrder.targetPrice != null && (
                    <span className="text-xs text-slate-500 dark:text-slate-400">TP <b className="text-green-600">{pendingOrder.targetPrice.toFixed(2)}</b></span>
                  )}
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[10px] text-slate-400 dark:text-slate-500">
                    Fills when a bar trades {pendingOrder.direction === 'long' ? 'down to' : 'up to'} the limit · expires at session end
                  </span>
                  <button onClick={cancelPendingOrder}
                    className="inline-flex items-center gap-1 bg-slate-200 hover:bg-slate-300 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-200 px-3 py-1.5 rounded-lg text-xs font-bold transition-colors">
                    <X size={12}/> Cancel
                  </button>
                </div>
              </div>
            ) : (
              <div className="text-xs text-slate-400 dark:text-slate-500">Flat — place an order to open a position.</div>
            )}
          </div>
            )}

            {/* SESSION STATS */}
            <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-2 gap-3">
          <Stat label="Trades" value={String(stats.trades)} tone={stats.trades === 0 ? 'muted' : undefined}/>
          <Stat label="W / L" value={stats.trades === 0 ? '—' : `${stats.wins} / ${stats.losses}`} tone={stats.trades === 0 ? 'muted' : undefined}/>
          <Stat label="Win rate" value={stats.winRate == null ? '—' : `${stats.winRate.toFixed(0)}%`} tone={stats.winRate == null ? 'muted' : stats.winRate >= 50 ? 'pos' : undefined}/>
          <Stat label="Total R" value={stats.rCount === 0 ? '—' : fmtR(stats.totalR)} tone={stats.rCount === 0 ? 'muted' : stats.totalR >= 0 ? 'pos' : 'neg'}/>
          <Stat label="Total $" value={stats.trades === 0 ? '—' : fmtUsd(stats.totalDollars)} tone={stats.trades === 0 ? 'muted' : stats.totalDollars >= 0 ? 'pos' : 'neg'}/>
          <Stat label="Avg R" value={stats.avgR == null ? '—' : fmtR(stats.avgR)} tone={stats.avgR == null ? 'muted' : stats.avgR >= 0 ? 'pos' : 'neg'}/>
            </div>
          </div>
        </div>
      )}

      {/* SESSION SUMMARY (on-demand or at the true end of the data — the
          multi-day replay never auto-ends per day). Stats span every day traded. */}
      {done && day && (
        <div className="rounded-2xl border border-violet-200 dark:border-violet-900/50 bg-violet-50 dark:bg-violet-950/20 p-5 mb-4">
          <div className="flex items-start justify-between gap-4 flex-wrap">
            <div>
              <div className="text-[10px] uppercase tracking-[0.2em] text-violet-500 dark:text-violet-300 font-bold mb-1">Session complete</div>
              <div className="text-lg font-extrabold text-slate-900 dark:text-slate-100">
                {activeInstrument} · {date}{lastBar && etDateOf(lastBar.time) !== date ? ` → ${etDateOf(lastBar.time)}` : ''}
                {blind && <span className="ml-2 text-xs font-bold text-violet-500 dark:text-violet-300">(blind window revealed)</span>}
              </div>
              <div className="text-sm text-slate-600 dark:text-slate-300 mt-1">
                {stats.trades === 0
                  ? 'No trades taken this session.'
                  : `${stats.trades} trade${stats.trades === 1 ? '' : 's'} · ${stats.wins}W/${stats.losses}L · ${stats.rCount === 0 ? '—R' : fmtR(stats.totalR)} · ${fmtUsd(stats.totalDollars)}`}
              </div>
            </div>
            <div className="flex items-center gap-2">
              {(revealed < bars.length || hasMoreForward) && (
                <button onClick={() => setDone(false)}
                  className="inline-flex items-center gap-2 bg-slate-200 hover:bg-slate-300 dark:bg-slate-700 dark:hover:bg-slate-600 text-slate-700 dark:text-slate-100 px-4 py-2 rounded-xl text-sm font-bold transition-colors">
                  <Play size={15}/> Resume replay
                </button>
              )}
              <button onClick={() => loadRandomDay()}
                className="inline-flex items-center gap-2 bg-violet-600 hover:bg-violet-500 text-white px-4 py-2 rounded-xl text-sm font-bold transition-colors">
                <Shuffle size={15}/> Another random day
              </button>
            </div>
          </div>
        </div>
      )}

      {/* SESSION TRADES */}
      {closed.length > 0 && (
        <div className="bg-slate-50 rounded-xl border border-slate-200 dark:bg-slate-900 dark:border-slate-700 mb-4 overflow-hidden">
          <div className="px-4 pt-3 pb-2 text-[11px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">This session's trades</div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-slate-400 dark:text-slate-500 border-b border-slate-200 dark:border-slate-800">
                  <th className="px-4 py-2 font-semibold">Dir</th>
                  <th className="px-4 py-2 font-semibold">Qty</th>
                  <th className="px-4 py-2 font-semibold">Entry</th>
                  <th className="px-4 py-2 font-semibold">Exit</th>
                  <th className="px-4 py-2 font-semibold">Reason</th>
                  <th className="px-4 py-2 font-semibold">Pts</th>
                  <th className="px-4 py-2 font-semibold">R</th>
                  <th className="px-4 py-2 font-semibold">$</th>
                </tr>
              </thead>
              <tbody>
                {closed.map((t, i) => (
                  <tr key={i} className="border-b border-slate-100 dark:border-slate-800/60 last:border-0">
                    <td className={`px-4 py-2 font-bold ${t.direction === 'long' ? 'text-green-600' : 'text-red-500'}`}>{t.direction.toUpperCase()}</td>
                    <td className="px-4 py-2 text-slate-600 dark:text-slate-300">{t.qty}</td>
                    <td className="px-4 py-2 text-slate-600 dark:text-slate-300">{t.entryPrice.toFixed(2)} <span className="text-slate-400">{etClock(t.entryTime)}</span></td>
                    <td className="px-4 py-2 text-slate-600 dark:text-slate-300">{t.exitPrice.toFixed(2)} <span className="text-slate-400">{etClock(t.exitTime)}</span></td>
                    <td className="px-4 py-2 text-slate-500 dark:text-slate-400">{t.exitReason}</td>
                    <td className={`px-4 py-2 font-semibold ${t.points >= 0 ? 'text-green-600' : 'text-red-500'}`}>{fmtPts(t.points)}</td>
                    <td className={`px-4 py-2 font-semibold ${t.r == null ? 'text-slate-400 dark:text-slate-500' : t.r >= 0 ? 'text-green-600' : 'text-red-500'}`}>{t.r == null ? '—' : fmtR(t.r)}</td>
                    <td className={`px-4 py-2 font-semibold ${t.dollars >= 0 ? 'text-green-600' : 'text-red-500'}`}>{fmtUsd(t.dollars)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* RECENT SESSIONS + EXPORT */}
      <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 dark:bg-slate-900 dark:border-slate-700">
        <div className="flex items-center justify-between gap-3 mb-2">
          <div className="text-[11px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">Recent replay sessions</div>
          <button onClick={exportLog}
            className="inline-flex items-center gap-1.5 text-xs font-bold text-violet-600 dark:text-violet-300 hover:text-violet-500 transition-colors">
            <Download size={13}/> Export JSON
          </button>
        </div>
        {recentSessions.length === 0 ? (
          <div className="text-xs text-slate-400 dark:text-slate-500">No logged trades yet — closed trades are saved locally in your browser.</div>
        ) : (
          <ul className="divide-y divide-slate-200 dark:divide-slate-800">
            {recentSessions.map((s, i) => (
              <li key={i} className="py-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
                <span className="font-bold text-slate-700 dark:text-slate-200">{s.instrument} · {dateHidden && s.sid === sessionIdRef.current ? 'blind day (hidden)' : s.date}</span>
                <span className="text-slate-500 dark:text-slate-400">{s.trades} trade{s.trades === 1 ? '' : 's'}</span>
                <span className={`font-semibold ${s.totalR >= 0 ? 'text-green-600' : 'text-red-500'}`}>{fmtR(s.totalR)}</span>
                <span className={`font-semibold ${s.totalDollars >= 0 ? 'text-green-600' : 'text-red-500'}`}>{fmtUsd(s.totalDollars)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
      </>)}

      {/* ══ RANGE BACKTEST (items 1-3) ═════════════════════════════════════ */}
      {mode === 'backtest' && (
        <>
          {/* CONTROLS ROW — instrument · date range · presets · strategy · Run */}
          <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 dark:bg-slate-900 dark:border-slate-700 mb-4">
            <div className="flex flex-wrap items-end gap-3">
              <label className="flex flex-col gap-1">
                <span className={fieldLabel}>Instrument</span>
                <select value={instrument} onChange={(e) => setInstrument(e.target.value)} className={inputCls}>
                  {instruments.map((i) => <option key={i} value={i}>{i}</option>)}
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className={fieldLabel}>From</span>
                <input type="date" value={btStart} min={DATA_START} max={btEnd}
                  onChange={(e) => { setBtStart(btClampStart(e.target.value, btEnd)); setBtPreset(null) }} className={inputCls}/>
              </label>
              <label className="flex flex-col gap-1">
                <span className={fieldLabel}>To</span>
                <input type="date" value={btEnd} min={btStart} max={btEndBound}
                  onChange={(e) => { const v = e.target.value > btEndBound ? btEndBound : e.target.value; setBtEnd(v); setBtPreset(null) }} className={inputCls}/>
              </label>
              <div className="flex flex-col gap-1">
                <span className={fieldLabel}>Range</span>
                <div className="inline-flex rounded-lg bg-slate-100 dark:bg-slate-800 p-0.5 border border-slate-200 dark:border-slate-700">
                  {BT_PRESETS.map((p) => (
                    <button key={p.label} onClick={() => applyBtPreset(p.label)}
                      className={`${chipBase} ${btPreset === p.label ? chipOn : chipOff}`}>{p.label}</button>
                  ))}
                </div>
              </div>
              <label className="flex flex-col gap-1 min-w-[180px] flex-1 sm:flex-none sm:w-56">
                <span className={fieldLabel}>Strategy</span>
                <select value={strategyId} onChange={(e) => setStrategyId(e.target.value)} className={inputCls}>
                  <option value="">Select a strategy…</option>
                  {btGrouped ? (
                    <>
                      <optgroup label="V2 engine">{v2Strats.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</optgroup>
                      <optgroup label="V1">{v1Strats.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</optgroup>
                    </>
                  ) : strategies.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
              </label>
              <button onClick={startBacktest} disabled={!strategyId || starting || btRunning}
                className="inline-flex items-center gap-2 bg-violet-600 hover:bg-violet-500 disabled:opacity-40 disabled:cursor-not-allowed text-white px-5 py-2 rounded-xl text-sm font-bold transition-colors shadow-lg shadow-violet-300/40 dark:shadow-violet-900/30 ml-auto">
                <Play size={15}/> {starting ? 'Starting…' : 'Run Backtest'}
              </button>
            </div>
            {/* OWNER-GATES A/B TOGGLE — mirrors the live/paper live-bias + NY-lunch gates */}
            <label className="mt-3 flex items-start gap-2.5 cursor-pointer select-none w-fit">
              <input type="checkbox" checked={btOwnerGates} disabled={btRunning}
                onChange={(e) => setBtOwnerGates(e.target.checked)}
                className="mt-0.5 h-4 w-4 rounded border-slate-300 dark:border-slate-600 accent-violet-600 cursor-pointer disabled:cursor-not-allowed"/>
              <span className="flex flex-col gap-0.5">
                <span className="text-xs font-bold text-slate-700 dark:text-slate-200">Apply live-bias + NY-lunch gates</span>
                <span className="text-[10.5px] text-slate-400 dark:text-slate-500">Off = raw strategy edge · On = mirrors live/paper · run both to A/B</span>
              </span>
            </label>
            <p className="text-[10.5px] text-slate-400 dark:text-slate-500 mt-2.5">
              Data from {DATA_START} · timeframes come from the strategy's own config · $100k sim account, $2.25/side commission, 1 tick slippage · live-bias / NY-lunch gates apply only when the checkbox above is on
            </p>
            {startError && (
              <div className="mt-3 rounded-lg border border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-950/30 px-3 py-2 text-xs text-red-700 dark:text-red-300 flex items-center gap-2">
                <AlertTriangle size={14} className="flex-shrink-0"/> {startError}
              </div>
            )}
          </div>

          {/* RUNNING */}
          {btRunning && (
            <div className="rounded-2xl border border-violet-300 dark:border-violet-700 bg-violet-50 dark:bg-violet-950/30 p-5 mb-4">
              <div className="flex items-center gap-3 mb-2 flex-wrap">
                <div className="inline-block w-5 h-5 border-2 border-violet-600 border-r-transparent rounded-full animate-spin flex-shrink-0"/>
                <div className="font-bold text-slate-900 dark:text-slate-100">
                  {btRunStatus === 'queued' || btRunStatus === '' ? 'Queued…' : 'Backtesting…'}{' '}
                  <span className="font-mono tabular-nums">{(btRun?.progress ?? 0).toFixed(0)}%</span>
                </div>
                {btStartedAt != null && (
                  <div className="text-xs text-slate-500 dark:text-slate-400 font-mono tabular-nums ml-auto">{fmtBtElapsed(btElapsed)} elapsed</div>
                )}
              </div>
              <div className="h-2 bg-slate-200 dark:bg-slate-700 rounded-full overflow-hidden mb-2">
                <div className="h-full bg-gradient-to-r from-violet-500 to-fuchsia-500 transition-all duration-700 ease-out" style={{ width: `${Math.max(2, btRun?.progress ?? 0)}%` }}/>
              </div>
              <div className="text-[11px] text-slate-500 dark:text-slate-400">
                {btRun?.strategy_name || strategies.find((s) => s.id === strategyId)?.name || 'Strategy'} · {btRun?.instrument || instrument} · {(btRun?.start_date || btStart).slice(0, 10)} → {(btRun?.end_date || btEnd).slice(0, 10)}
              </div>
            </div>
          )}

          {/* LOST / STALLED */}
          {(runLost || btStalled) && !btFailed && !btCompleted && (
            <div className="rounded-2xl border border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30 p-5 mb-4">
              <div className="font-bold text-amber-800 dark:text-amber-200 mb-1">{runLost ? 'Run not found' : 'Run stalled'}</div>
              <div className="text-xs text-amber-700 dark:text-amber-300 mb-3">
                {runLost
                  ? 'This run no longer appears in your runs list — it may have been deleted.'
                  : 'No progress for 35 minutes — the backend may have restarted mid-run. Try again.'}
              </div>
              <button onClick={startBacktest} disabled={!strategyId || starting}
                className="bg-amber-600 hover:bg-amber-500 disabled:opacity-40 text-white text-xs font-bold px-3 py-1.5 rounded-lg inline-flex items-center gap-1.5">
                <RotateCcw size={12}/> Run again
              </button>
            </div>
          )}

          {/* FAILED */}
          {btFailed && (
            <div className="rounded-2xl border border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-950/30 p-5 mb-4">
              <div className="font-bold text-red-800 dark:text-red-200 mb-1">Backtest {btRunStatus}</div>
              <div className="text-xs text-red-700 dark:text-red-300 mb-3">{(btRun as any)?.error_message || 'The run did not finish. Try a shorter date range or a different strategy.'}</div>
              <button onClick={startBacktest} disabled={!strategyId || starting}
                className="bg-red-600 hover:bg-red-500 disabled:opacity-40 text-white text-xs font-bold px-3 py-1.5 rounded-lg inline-flex items-center gap-1.5">
                <RotateCcw size={12}/> Run again
              </button>
            </div>
          )}

          {/* metrics loading */}
          {btCompleted && !btMetrics && btMetricsLoading && (
            <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-6 text-center mb-4">
              <div className="inline-block w-6 h-6 border-2 border-violet-500 border-r-transparent rounded-full animate-spin mb-2"/>
              <div className="text-sm text-slate-600 dark:text-slate-300">Loading results…</div>
            </div>
          )}

          {/* RESULTS + ON-CHART TRADE OVERLAY */}
          {btCompleted && btMetrics && (
            <div className="space-y-4 mb-4">
              {/* header */}
              <div className="flex items-center justify-between gap-3 flex-wrap">
                <div className="text-sm font-bold text-slate-900 dark:text-slate-100 min-w-0 truncate">
                  {btRun?.strategy_name || 'Strategy'}
                  <span className="text-slate-400 dark:text-slate-500 font-medium"> · {btRun?.instrument || instrument} · {(btRun?.start_date || btStart).slice(0, 10)} → {(btRun?.end_date || btEnd).slice(0, 10)}</span>
                </div>
                <button onClick={startBacktest} disabled={!strategyId || starting}
                  className="inline-flex items-center gap-1.5 text-xs font-bold px-3 py-1.5 rounded-lg bg-violet-600 hover:bg-violet-500 disabled:opacity-40 text-white transition-colors">
                  <RotateCcw size={12}/> Run again
                </button>
              </div>

              {/* stat tiles — only stats the metrics API actually returns */}
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
                <Stat label="Win rate" value={fmtPct01(btMetrics.win_rate)} tone={btMetrics.win_rate >= 0.5 ? 'pos' : 'neg'}/>
                <Stat label="Net P&L" value={fmtMoney(btMetrics.net_profit)} tone={btMetrics.net_profit >= 0 ? 'pos' : 'neg'}/>
                <Stat label="Profit factor" value={btMetrics.profit_factor?.toFixed(2) ?? '—'} tone={btMetrics.profit_factor >= 1 ? 'pos' : 'neg'}/>
                <Stat label="Trades" value={String(btMetrics.total_trades)}/>
                <Stat label="W / L" value={`${btMetrics.winning_trades ?? '—'} / ${btMetrics.losing_trades ?? '—'}`}/>
                <Stat label="Avg R" value={btMetrics.avg_rr != null ? btMetrics.avg_rr.toFixed(2) : '—'} tone={(btMetrics.avg_rr ?? 0) >= 0 ? 'pos' : 'neg'}/>
                <Stat label="Max drawdown" value={btMetrics.max_drawdown_pct != null ? `${btMetrics.max_drawdown_pct.toFixed(1)}%` : '—'} tone="neg"/>
                {btMetrics.expectancy != null && <Stat label="Expectancy" value={`${fmtMoney(btMetrics.expectancy)}/t`} tone={btMetrics.expectancy > 0 ? 'pos' : 'neg'}/>}
                {btMetrics.sharpe_ratio != null && <Stat label="Sharpe" value={btMetrics.sharpe_ratio.toFixed(2)} tone={btMetrics.sharpe_ratio >= 1 ? 'pos' : undefined}/>}
                {btOwnerGateSkips != null && <Stat label="Gate-skipped entries" value={String(btOwnerGateSkips)} tone={btOwnerGateSkips > 0 ? 'neg' : 'muted'}/>}
              </div>
              {btOwnerGateSkips != null && (
                <p className="text-[10.5px] text-slate-400 dark:text-slate-500 -mt-1">
                  Live-bias + NY-lunch gates were applied: {btOwnerGateSkips} entr{btOwnerGateSkips === 1 ? 'y' : 'ies'} the raw strategy would have taken were skipped. Run again with the toggle off to compare the raw edge.
                </p>
              )}

              {/* CHART CARD — the "backtest the entire thing" visualization */}
              <div ref={btFsRef}
                style={settings.background ? { background: settings.background } : undefined}
                className={`relative rounded-xl border border-slate-200 dark:border-slate-700 overflow-hidden bg-white dark:bg-[#131722] ${fullscreen ? 'flex flex-col' : ''}`}>
                <div className="flex flex-wrap items-center gap-2 px-2.5 py-2 border-b border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900">
                  <div className="inline-flex rounded-lg bg-slate-100 dark:bg-slate-800 p-0.5 border border-slate-200 dark:border-slate-700">
                    {TF_CHIPS.map((t) => (
                      <button key={t} onClick={() => setBtTf(t)} className={`${chipBase} ${btTf === t ? chipOn : chipOff}`}>{tfLabel(t)}</button>
                    ))}
                  </div>
                  <button onClick={() => updateSettings({ sessionsEnabled: !settings.sessionsEnabled })} title="Toggle session shading"
                    className={`inline-flex items-center justify-center h-8 w-8 rounded-lg transition-colors ${settings.sessionsEnabled ? 'bg-violet-600 text-white hover:bg-violet-500' : iconBtn}`}>
                    <Layers size={14}/>
                  </button>
                  <button onClick={toggleBtFullscreen} title={fullscreen ? 'Exit fullscreen' : 'Fullscreen'} className={iconBtn}>
                    {fullscreen ? <Minimize2 size={14}/> : <Maximize2 size={14}/>}
                  </button>
                  {btChartSpan && (
                    <div className="ml-auto text-right leading-tight">
                      <div className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">Charted {btChartSpan.from} → {btChartSpan.to}</div>
                      <div className="text-xs font-extrabold text-slate-800 dark:text-slate-100">
                        {btPlottedCount} of {btResultTradeCount} trades plotted{btChartCapped ? <span className="text-amber-600 dark:text-amber-400"> · window capped</span> : null}
                      </div>
                    </div>
                  )}
                </div>
                <div className={`relative ${fullscreen ? 'flex-1 min-h-0' : 'h-[380px] md:h-[62vh] md:max-h-[620px]'}`}>
                  {btChartState === 'loading' && (
                    <div className="absolute inset-0 flex items-center justify-center"><Loader2 className="animate-spin text-slate-400"/></div>
                  )}
                  {btChartState === 'error' && (
                    <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-center px-4">
                      <AlertTriangle className="text-amber-400" size={26}/>
                      <div className="text-sm font-semibold text-slate-600 dark:text-slate-300">Couldn't load chart bars for this range</div>
                      <div className="text-xs text-slate-400 dark:text-slate-500">The stats and trades table below are still complete.</div>
                    </div>
                  )}
                  {btChartState === 'ready' && btChartBars.length > 0 && (
                    <>
                      <TVReplayChart
                        instrument={btRun?.instrument || instrument}
                        bars={btChartBars}
                        displayTf={btTf}
                        resetKey={`bt-${runId}-${btChartNonce}`}
                        showDate
                        pdh={null}
                        pdl={null}
                        position={null}
                        trades={btChartTrades}
                        showGrid={settings.showGrid}
                        upColor={settings.upColor}
                        downColor={settings.downColor}
                        background={settings.background}
                        sessionsEnabled={settings.sessionsEnabled}
                        sessionVisibility={settings.sessionVisibility as SessionVisibility}
                        timezone={resolveTz(settings.timezone)}
                        hour12={settings.hour12}
                        onDrawingApi={handleDrawingApi}
                        onDrawingState={handleDrawingState}
                      />
                      <DrawingToolbar state={drawState} api={drawApiRef.current} onSelectTool={selectDrawTool} onSetDefaults={setDrawDefaults}/>
                    </>
                  )}
                </div>
              </div>

              {/* equity curve */}
              {btEquitySeries.length > 1 && (
                <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 sm:p-5 dark:bg-slate-900 dark:border-slate-700">
                  <div className="flex items-center justify-between mb-3">
                    <div className="text-sm font-semibold text-slate-700 dark:text-slate-200 flex items-center gap-2"><TrendingUp size={15} className="text-slate-400"/> Equity curve</div>
                    <div className="text-[11px] text-slate-400 dark:text-slate-500 font-mono tabular-nums">{fmtMoney(btEquitySeries[0])} → {fmtMoney(btEquitySeries[btEquitySeries.length - 1])}</div>
                  </div>
                  <BtEquityLine data={btEquitySeries}/>
                </div>
              )}

              {/* trades table */}
              <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 sm:p-5 dark:bg-slate-900 dark:border-slate-700">
                <div className="text-sm font-semibold text-slate-700 dark:text-slate-200 mb-3">
                  Trades <span className="text-slate-400 dark:text-slate-500 font-normal">({btTradesPending ? '…' : btTrades.length})</span>
                </div>
                {btTradesPending ? (
                  <div className="text-center text-sm text-slate-400 dark:text-slate-500 py-8 border border-dashed rounded-xl border-slate-300 dark:border-slate-700">Loading trades…</div>
                ) : btTrades.length === 0 ? (
                  <div className="text-center text-sm text-slate-400 dark:text-slate-500 py-8 border border-dashed rounded-xl border-slate-300 dark:border-slate-700">No trades in this window — the strategy's conditions never lined up.</div>
                ) : (
                  <div className="overflow-x-auto -mx-1 px-1">
                    <table className="w-full text-xs min-w-[640px]">
                      <thead>
                        <tr className="text-left text-[10px] uppercase tracking-wider text-slate-400 dark:text-slate-500 border-b border-slate-200 dark:border-slate-700">
                          <th className="py-2 pr-3 font-bold">#</th>
                          <th className="py-2 pr-3 font-bold">Dir</th>
                          <th className="py-2 pr-3 font-bold">Entry</th>
                          <th className="py-2 pr-3 font-bold">Exit</th>
                          <th className="py-2 pr-3 font-bold">Entry time</th>
                          <th className="py-2 pr-3 font-bold">Exit time</th>
                          <th className="py-2 pr-3 font-bold text-right">Net P&L</th>
                          <th className="py-2 font-bold">Exit reason</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(btTrades as any[]).map((t, i) => (
                          <tr key={t.id ?? i} className="border-b border-slate-100 dark:border-slate-800 last:border-0">
                            <td className="py-2 pr-3 text-slate-400 dark:text-slate-500 tabular-nums">{i + 1}</td>
                            <td className="py-2 pr-3"><span className={`font-bold uppercase ${t.direction === 'long' ? 'text-green-600 dark:text-green-400' : 'text-red-500'}`}>{t.direction}</span></td>
                            <td className="py-2 pr-3 font-mono tabular-nums text-slate-700 dark:text-slate-300">{t.entry_price ?? '—'}</td>
                            <td className="py-2 pr-3 font-mono tabular-nums text-slate-700 dark:text-slate-300">{t.exit_price ?? '—'}</td>
                            <td className="py-2 pr-3 text-slate-500 dark:text-slate-400 whitespace-nowrap">{fmtBtTime(t.entry_time)}</td>
                            <td className="py-2 pr-3 text-slate-500 dark:text-slate-400 whitespace-nowrap">{fmtBtTime(t.exit_time)}</td>
                            <td className={`py-2 pr-3 font-mono tabular-nums font-bold text-right ${((t.net_pnl ?? t.pnl) ?? 0) >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-500'}`}>{fmtMoney((t.net_pnl ?? t.pnl) ?? 0)}</td>
                            <td className="py-2 text-slate-500 dark:text-slate-400 whitespace-nowrap">{t.exit_reason || '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* EMPTY STATE (no run yet) */}
          {!runId && (
            strategies.length === 0 ? (
              <div className="text-center py-16 border border-dashed rounded-2xl border-slate-300 dark:border-slate-700 mb-4">
                <FlaskConical size={22} className="mx-auto text-slate-300 dark:text-slate-600 mb-2"/>
                <div className="text-sm font-semibold text-slate-600 dark:text-slate-300 mb-1">No strategies yet</div>
                <div className="text-xs text-slate-400 dark:text-slate-500">Build a strategy first, then backtest it across a whole date range here.</div>
              </div>
            ) : (
              <div className="text-center py-16 border border-dashed rounded-2xl border-slate-300 dark:border-slate-700 mb-4">
                <Play size={22} className="mx-auto text-slate-300 dark:text-slate-600 mb-2"/>
                <div className="text-sm font-semibold text-slate-600 dark:text-slate-300 mb-1">Backtest the entire range at once</div>
                <div className="text-xs text-slate-400 dark:text-slate-500">Pick a strategy + window above and hit Run — stats, the trades table and every trade on the chart land right here.</div>
              </div>
            )
          )}

          {/* RECENT BACKTESTS (localStorage log) */}
          {btLog.length > 0 && (
            <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 sm:p-5 dark:bg-slate-900 dark:border-slate-700">
              <div className="text-sm font-semibold text-slate-700 dark:text-slate-200 mb-3 flex items-center gap-2">
                <History size={15} className="text-slate-400"/> Recent backtests
                <span className="text-[10px] text-slate-400 dark:text-slate-500 font-normal">(this browser, last {BT_LOG_MAX})</span>
              </div>
              <div className="space-y-1.5">
                {btLog.map((e) => (
                  <button key={e.run_id} onClick={() => viewBtLogEntry(e)}
                    className={`w-full text-left rounded-lg border px-3 py-2 transition-colors flex items-center gap-3 flex-wrap ${runId === e.run_id ? 'border-violet-400 dark:border-violet-600 bg-violet-50 dark:bg-violet-950/30' : 'border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 hover:border-violet-300 dark:hover:border-violet-700'}`}>
                    <span className="text-xs font-bold text-slate-800 dark:text-slate-100 truncate min-w-0 flex-1">{e.strategy_name}</span>
                    <span className="text-[10.5px] text-slate-400 dark:text-slate-500 flex-shrink-0">{e.instrument} · {e.start_date} → {e.end_date}</span>
                    <span className="text-[11px] font-mono tabular-nums flex-shrink-0">
                      {e.win_rate != null && <span className={e.win_rate >= 0.5 ? 'text-green-600 dark:text-green-400' : 'text-red-500'}>WR {fmtPct01(e.win_rate)}</span>}
                      {e.profit_factor != null && <span className="text-slate-500 dark:text-slate-400"> · PF {e.profit_factor.toFixed(2)}</span>}
                      {e.total_trades != null && <span className="text-slate-400 dark:text-slate-500"> · {e.total_trades}t</span>}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
