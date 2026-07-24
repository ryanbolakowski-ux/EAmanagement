import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Play, Pause, StepForward, FastForward, Shuffle, Rewind, Download,
  TrendingUp, TrendingDown, X, AlertTriangle, CalendarOff, Loader2, EyeOff,
  Settings as SettingsIcon, Maximize2, Minimize2, Plus, Check, Clock, Crosshair, RotateCcw, Layers,
} from 'lucide-react'
import { replayApi, type ReplayDay, type ReplayMeta } from '../api/endpoints'
import TVReplayChart, { type SessionVisibility } from '../components/TVReplayChart'
import {
  POINT_VALUES, checkExit, closePosition, openPosition,
  sessionStats, unrealized,
  type ClosedTrade, type Direction, type OpenPosition, type SimBar,
} from '../lib/replaySim'

const INSTRUMENTS = ['ES', 'NQ', 'YM', 'RTY']
// Playback speed in bars per second (FX-Replay style).
const SPEEDS = [1, 2, 4, 10]
// Chart-only display timeframes in MINUTES (the sim ALWAYS steps raw 1m bars).
// Any 1–240 works via the custom input; these are the quick chips.
const TF_CHIPS = [1, 2, 3, 5, 15, 30, 60, 240]
const tfLabel = (t: number) => (t % 60 === 0 ? `${t / 60}h` : `${t}m`)
const LOG_KEY = 'theta_replay_log'
// How many bars are pre-revealed when an RTH day loads, so the trader has
// context instead of a single candle. (ETH loads reveal up to the NY open.)
const INITIAL_REVEAL = 60

// ── chart settings (GOAL A/B/I — persisted, passed live to TVReplayChart) ────

const SETTINGS_KEY = 'theta_replay_settings'

type SessionVis = { asia: boolean; london: boolean; nyAm: boolean; nyLunch: boolean; nyPm: boolean }
type ReplaySettings = {
  v: number
  upColor: string
  downColor: string
  background: string | null
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
      sessionVisibility: { ...DEFAULT_SETTINGS.sessionVisibility, ...(p.sessionVisibility || {}) },
    }
  } catch {
    return DEFAULT_SETTINGS
  }
}
function saveSettings(s: ReplaySettings) {
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(s)) } catch { /* quota — ignore */ }
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
// Minutes-since-midnight in ET for a UTC epoch (used to find the NY open bar).
function etMinutesOf(epochSecs: number): number {
  const parts = ET_TIME_FMT.formatToParts(new Date(epochSecs * 1000))
  const h = Number(parts.find((p) => p.type === 'hour')?.value ?? '0')
  const m = Number(parts.find((p) => p.type === 'minute')?.value ?? '0')
  return (h % 24) * 60 + m
}

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

  // Sim state
  const [pos, setPos] = useState<OpenPosition | null>(null)
  const [closed, setClosed] = useState<ClosedTrade[]>([])
  const sessionIdRef = useRef('')
  const [logVersion, setLogVersion] = useState(0)

  // Live SL/TP price editors + place-on-chart arming (GOAL G).
  const [slInput, setSlInput] = useState('')
  const [tpInput, setTpInput] = useState('')
  const [armed, setArmed] = useState<'sl' | 'tp' | null>(null)

  // Chart settings + UI chrome
  const [settings, setSettings] = useState<ReplaySettings>(() => loadSettings())
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsTab, setSettingsTab] = useState<SettingsTab>('appearance')
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number } | null>(null)
  const [fullscreen, setFullscreen] = useState(false)
  const fsRef = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)

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

  // ── day loading ────────────────────────────────────────────────────────────

  const resetSession = () => {
    setDay(null); setRevealed(0); setPlaying(false); setDone(false)
    setPos(null); setClosed([]); setTf(1); setArmed(null)
    sessionIdRef.current = ''
  }

  // Where to start the reveal. RTH: a small pre-roll. ETH: advance through the
  // overnight so playback opens at the NY open with full context behind it.
  const initialRevealFor = (d: ReplayDay, ethMode: boolean): number => {
    const len = d.candles.length
    if (!ethMode) return Math.min(INITIAL_REVEAL, len)
    let idx = -1
    if (d.ny_open_ts != null) idx = d.candles.findIndex((b) => b.time >= (d.ny_open_ts as number))
    if (idx < 0) idx = d.candles.findIndex((b) => etMinutesOf(b.time) >= 570) // 09:30 ET
    if (idx <= 0) return Math.min(INITIAL_REVEAL, len)
    return Math.min(len, idx)
  }

  const loadDay = async (inst: string, dt: string, blindMode: boolean, ethMode = eth): Promise<'ok' | 'holiday' | 'error'> => {
    resetSession()
    setBlind(blindMode)
    setLoadState('loading')
    setLoadError('')
    try {
      const res = await replayApi.day(inst, dt, ethMode)
      const d = res.data
      if (!d?.candles?.length) {
        setLoadState('holiday')
        return 'holiday'
      }
      setDay(d)
      setDate(dt)
      setRevealed(initialRevealFor(d, ethMode))
      sessionIdRef.current = `${inst}-${dt}-${Date.now()}`
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
    // Blind mode: the SERVER picks a hidden full weekday (/replay/random) so
    // holidays are pre-filtered and the date can't be inferred client-side.
    resetSession()
    setBlind(true)
    setLoadState('loading')
    setLoadError('')
    try {
      const res = await replayApi.random(instrument, ethMode)
      const d = res.data
      if (!d?.candles?.length) {
        setLoadState('holiday')
        return
      }
      setDay(d)
      setDate(d.date)
      setRevealed(initialRevealFor(d, ethMode))
      sessionIdRef.current = `${instrument}-${d.date}-${Date.now()}`
      setLoadState('idle')
    } catch (e: any) {
      setLoadState('error')
      setLoadError(e?.response?.data?.detail || e?.message || 'Failed to load a random day.')
    }
  }

  // ETH/RTH toggle: reload the SAME day with/without the overnight session.
  // Blind stays blind — we reuse the known date internally (never shown while
  // hidden). Sim semantics are unchanged; only the 1m bar set differs.
  const toggleEth = () => {
    const next = !eth
    setEth(next)
    if (!day) return
    if (date) loadDay(activeInstrument, date, blind, next)
    else loadRandomDay(next)
  }

  // ── trade recording ────────────────────────────────────────────────────────

  const recordTrade = (t: ClosedTrade) => {
    setClosed((prev) => [...prev, t])
    appendLog({
      session_id: sessionIdRef.current,
      instrument: activeInstrument,
      date: day?.date || date,
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
    setDone(true)
  }

  // Advance n 1m bars in one shot. A single function (vs calling step() n
  // times) because synchronous repeat calls would all read the same stale
  // `revealed`/`pos` state from this render.
  const advance = (n = 1) => {
    if (!day || done) return
    if (revealed >= bars.length) {
      endSession()
      return
    }
    let curPos = pos // local mirror — state won't update mid-function
    const stop = Math.min(bars.length, revealed + Math.max(1, n))
    for (let i = revealed; i < stop; i++) {
      const bar = bars[i] // the bar being revealed
      if (!curPos) continue
      const ex = checkExit(curPos, bar) // stop checked before target (conservative)
      if (ex) {
        recordTrade(closePosition(curPos, ex.price, bar.time, ex.reason, pointValue))
        curPos = null
      }
    }
    setPos(curPos)
    setRevealed(stop)
    if (stop >= bars.length) {
      // Day is fully revealed: close out and show the summary.
      setPlaying(false)
      setDone(true)
      if (curPos) {
        const last = bars[bars.length - 1]
        recordTrade(closePosition(curPos, last.close, last.time, 'session_end', pointValue))
        setPos(null)
      }
    }
  }
  // The interval must always call the LATEST advance (fresh state), so route
  // it through a ref that's reassigned every render.
  const stepRef = useRef(advance)
  stepRef.current = advance

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
  const ticketValid = qty >= 1 && stopOk && targetOk

  const placeOrder = (direction: Direction) => {
    if (!lastBar || pos || done || !ticketValid) return
    const target = targetValNum != null ? { kind: targetMode, value: targetValNum } : null
    setPos(openPosition(direction, qty, lastBar, stopPtsNum, target))
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

  // Place-on-chart: when armed, the next chart click sets that level's price.
  const handleChartClick = (price: number) => {
    const a = armed
    if (!a || !pos) return
    setPos((p) => (p ? { ...p, [a === 'sl' ? 'stopPrice' : 'targetPrice']: price } : p))
    setArmed(null)
  }
  const handleContextMenu = (x: number, y: number) => setCtxMenu({ x, y })

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
  const MENU_W = 210, MENU_H = 190
  const mx = ctxMenu ? Math.max(6, Math.min(ctxMenu.x, (typeof window !== 'undefined' ? window.innerWidth : 1200) - MENU_W)) : 0
  const my = ctxMenu ? Math.max(6, Math.min(ctxMenu.y, (typeof window !== 'undefined' ? window.innerHeight : 800) - MENU_H)) : 0

  // ── render ────────────────────────────────────────────────────────────────

  const chipBase = 'px-2.5 py-1 rounded-md text-xs font-bold transition-all'
  const chipOn = 'bg-white dark:bg-slate-700 text-violet-700 dark:text-violet-300 shadow-sm'
  const chipOff = 'text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200'
  const iconBtn = 'inline-flex items-center justify-center h-8 w-8 rounded-lg text-slate-600 dark:text-slate-300 bg-slate-200 hover:bg-slate-300 dark:bg-slate-800 dark:hover:bg-slate-700 transition-colors'

  return (
    <div className="p-4 sm:p-8 max-w-screen-2xl">
      {/* HERO */}
      <div className="rounded-3xl bg-gradient-to-br from-slate-900 via-slate-900 to-violet-950 dark:from-slate-950 dark:via-slate-950 dark:to-violet-950 text-white p-6 md:p-8 shadow-xl mb-6">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="min-w-0 flex-1">
            <div className="text-[10px] uppercase tracking-[0.2em] text-violet-300 font-bold mb-1">Practice</div>
            <h1 className="text-2xl md:text-3xl font-extrabold text-white">Replay</h1>
            <p className="text-sm text-slate-400 mt-1">Bar-by-bar practice trading on historical futures days · stop checked before target, fills at level</p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => loadRandomDay()} disabled={loadState === 'loading'}
              className="inline-flex items-center gap-2 bg-violet-600 hover:bg-violet-500 disabled:opacity-50 text-white px-4 py-2 rounded-xl text-sm font-bold transition-colors shadow-lg shadow-violet-900/30">
              <Shuffle size={15}/> Random day
            </button>
          </div>
        </div>
      </div>

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
              onChange={(e) => { if (e.target.value) loadDay(instrument, e.target.value, false) }}
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
              {lastBar && (
                <div className="ml-auto text-right leading-tight">
                  <div className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">
                    {dateHidden ? 'Hidden day' : date} · {zonedClock(lastBar.time, settings.timezone, settings.hour12)} {tzShort}
                  </div>
                  <div className="text-xs font-extrabold text-slate-800 dark:text-slate-100">
                    {lastBar.close.toFixed(2)} <span className="text-[10px] font-semibold text-slate-400">({progressPct}% of day)</span>
                  </div>
                </div>
              )}
            </div>
            <div className={fullscreen ? 'flex-1 min-h-0' : 'h-[380px] md:h-[62vh] md:max-h-[620px]'}>
              <TVReplayChart
                instrument={activeInstrument}
                bars={revealedBars}
                displayTf={tf}
                resetKey={`${sessionIdRef.current}|${chartNonce}`}
                showDate={!dateHidden}
                pdh={day.pdh}
                pdl={day.pdl}
                position={pos}
                trades={closed}
                upColor={settings.upColor}
                downColor={settings.downColor}
                background={settings.background}
                sessionsEnabled={settings.sessionsEnabled}
                sessionVisibility={settings.sessionVisibility as SessionVisibility}
                timezone={resolveTz(settings.timezone)}
                hour12={settings.hour12}
                onChartClick={handleChartClick}
                onContextMenu={handleContextMenu}
              />
            </div>

            {/* place-on-chart armed banner (GOAL G) */}
            {armed && (
              <div className="absolute left-1/2 -translate-x-1/2 top-2 z-[80] flex items-center gap-2 rounded-lg bg-violet-600 text-white text-xs font-bold px-3 py-1.5 shadow-lg">
                <Crosshair size={13}/> Click the chart to set {armed === 'sl' ? 'SL' : 'TP'}
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
                        <button onClick={() => updateSettings({ upColor: DEFAULT_SETTINGS.upColor, downColor: DEFAULT_SETTINGS.downColor, background: null })}
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
            <div className="text-[11px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500 mb-3">Order ticket · fills at current close</div>
            <div className="flex flex-wrap items-end gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">Contracts</span>
                <input type="number" min={1} step={1} value={qtyStr} onChange={(e) => setQtyStr(e.target.value)}
                  className="w-20 rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 text-sm px-2 py-1.5"/>
              </label>
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
                <button onClick={() => placeOrder('long')} disabled={!!pos || !ticketValid || !lastBar}
                  className="inline-flex items-center gap-1.5 bg-green-600 hover:bg-green-500 disabled:opacity-40 text-white px-4 py-1.5 rounded-lg text-sm font-bold transition-colors">
                  <TrendingUp size={14}/> Buy
                </button>
                <button onClick={() => placeOrder('short')} disabled={!!pos || !ticketValid || !lastBar}
                  className="inline-flex items-center gap-1.5 bg-red-600 hover:bg-red-500 disabled:opacity-40 text-white px-4 py-1.5 rounded-lg text-sm font-bold transition-colors">
                  <TrendingDown size={14}/> Sell
                </button>
              </div>
            </div>
            <div className="text-[10px] text-slate-400 dark:text-slate-500 mt-2">
              {activeInstrument} point value ${pointValue}/contract · SL/TP optional — leave blank for a bare market order and set them on the chart later
            </div>
          </div>
            )}

            {!done && (
          <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 dark:bg-slate-900 dark:border-slate-700">
            <div className="text-[11px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500 mb-3">Open position</div>
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
                      <button onClick={() => setArmed(armed === 'sl' ? null : 'sl')} title="Set SL by clicking the chart"
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
                      <button onClick={() => setArmed(armed === 'tp' ? null : 'tp')} title="Set TP by clicking the chart"
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

      {/* DAY-END SUMMARY */}
      {done && day && (
        <div className="rounded-2xl border border-violet-200 dark:border-violet-900/50 bg-violet-50 dark:bg-violet-950/20 p-5 mb-4">
          <div className="flex items-start justify-between gap-4 flex-wrap">
            <div>
              <div className="text-[10px] uppercase tracking-[0.2em] text-violet-500 dark:text-violet-300 font-bold mb-1">Session complete</div>
              <div className="text-lg font-extrabold text-slate-900 dark:text-slate-100">
                {activeInstrument} · {date}{blind && <span className="ml-2 text-xs font-bold text-violet-500 dark:text-violet-300">(blind day revealed)</span>}
              </div>
              <div className="text-sm text-slate-600 dark:text-slate-300 mt-1">
                {stats.trades === 0
                  ? 'No trades taken this session.'
                  : `${stats.trades} trade${stats.trades === 1 ? '' : 's'} · ${stats.wins}W/${stats.losses}L · ${stats.rCount === 0 ? '—R' : fmtR(stats.totalR)} · ${fmtUsd(stats.totalDollars)}`}
              </div>
            </div>
            <button onClick={() => loadRandomDay()}
              className="inline-flex items-center gap-2 bg-violet-600 hover:bg-violet-500 text-white px-4 py-2 rounded-xl text-sm font-bold transition-colors">
              <Shuffle size={15}/> Another random day
            </button>
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
    </div>
  )
}
