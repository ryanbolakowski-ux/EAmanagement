import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Play, Pause, StepForward, Shuffle, Rewind, Download,
  TrendingUp, TrendingDown, X, AlertTriangle, CalendarOff, Loader2, EyeOff,
} from 'lucide-react'
import { replayApi, type ReplayDay, type ReplayMeta } from '../api/endpoints'
import CandlestickChart from '../components/CandlestickChart'
import {
  POINT_VALUES, aggregateBars, checkExit, closePosition, openPosition,
  sessionStats, unrealized,
  type ClosedTrade, type Direction, type OpenPosition, type SimBar,
} from '../lib/replaySim'

const INSTRUMENTS = ['ES', 'NQ', 'YM', 'RTY']
const SPEEDS = [1, 2, 5, 10, 30]
const LOG_KEY = 'theta_replay_log'
// How many bars are pre-revealed when a day loads, so the trader has context
// instead of a single candle.
const INITIAL_REVEAL = 60

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
  r: number
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

// ── page ─────────────────────────────────────────────────────────────────────

export default function Replay() {
  // Day selection
  const [instrument, setInstrument] = useState('ES')
  const [date, setDate] = useState('')
  const [blind, setBlind] = useState(false)
  const [day, setDay] = useState<ReplayDay | null>(null)
  const [loadState, setLoadState] = useState<'idle' | 'loading' | 'holiday' | 'error'>('idle')
  const [loadError, setLoadError] = useState('')

  // Playback
  const [revealed, setRevealed] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)
  const [tf, setTf] = useState<1 | 5 | 15>(1)
  const [done, setDone] = useState(false)

  // Order ticket
  const [qtyStr, setQtyStr] = useState('1')
  const [stopStr, setStopStr] = useState('10')
  const [targetMode, setTargetMode] = useState<'r' | 'points'>('r')
  const [targetStr, setTargetStr] = useState('2')

  // Sim state
  const [pos, setPos] = useState<OpenPosition | null>(null)
  const [closed, setClosed] = useState<ClosedTrade[]>([])
  const sessionIdRef = useRef('')
  const [logVersion, setLogVersion] = useState(0)

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
    setPos(null); setClosed([]); setTf(1)
    sessionIdRef.current = ''
  }

  const loadDay = async (inst: string, dt: string, blindMode: boolean): Promise<'ok' | 'holiday' | 'error'> => {
    resetSession()
    setBlind(blindMode)
    setLoadState('loading')
    setLoadError('')
    try {
      const res = await replayApi.day(inst, dt)
      const d = res.data
      if (!d?.candles?.length) {
        setLoadState('holiday')
        return 'holiday'
      }
      setDay(d)
      setDate(dt)
      setRevealed(Math.min(INITIAL_REVEAL, d.candles.length))
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

  const randomWeekday = (): string => {
    const minMs = new Date(`${meta?.min_date || '2024-01-02'}T12:00:00Z`).getTime()
    const maxMs = new Date(`${meta?.max_date || new Date().toISOString().slice(0, 10)}T12:00:00Z`).getTime()
    for (let i = 0; i < 50; i++) {
      const d = new Date(minMs + Math.random() * Math.max(1, maxMs - minMs))
      const dow = d.getUTCDay()
      if (dow === 0 || dow === 6) continue
      return d.toISOString().slice(0, 10)
    }
    return meta?.max_date || new Date().toISOString().slice(0, 10)
  }

  const loadRandomDay = async () => {
    // Blind mode: the trader shouldn't know which day it is until the session
    // ends. Retry a few times to skip holidays (backend 404s those).
    for (let attempt = 0; attempt < 8; attempt++) {
      const result = await loadDay(instrument, randomWeekday(), true)
      if (result === 'ok') return
      if (result === 'error') return // real error — don't spin
    }
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

  const step = () => {
    if (!day || done) return
    if (revealed >= bars.length) {
      endSession()
      return
    }
    const bar = bars[revealed] // the bar about to be revealed
    let curPos = pos // local mirror — state won't update mid-function
    if (curPos) {
      const ex = checkExit(curPos, bar) // stop checked before target (conservative)
      if (ex) {
        recordTrade(closePosition(curPos, ex.price, bar.time, ex.reason, pointValue))
        setPos(null)
        curPos = null
      }
    }
    const next = revealed + 1
    setRevealed(next)
    if (next >= bars.length) {
      // Day is fully revealed: close out and show the summary.
      setPlaying(false)
      setDone(true)
      if (curPos) {
        recordTrade(closePosition(curPos, bar.close, bar.time, 'session_end', pointValue))
        setPos(null)
      }
    }
  }
  // The interval must always call the LATEST step (fresh state), so route it
  // through a ref that's reassigned every render.
  const stepRef = useRef(step)
  stepRef.current = step

  useEffect(() => {
    if (!playing) return
    const ms = Math.max(33, Math.round(1000 / speed))
    const id = setInterval(() => stepRef.current(), ms)
    return () => clearInterval(id)
  }, [playing, speed])

  // ── order actions ──────────────────────────────────────────────────────────

  const qty = Math.max(1, Math.floor(Number(qtyStr) || 0))
  const stopPts = Number(stopStr)
  const targetVal = Number(targetStr)
  const ticketValid = qty >= 1 && stopPts > 0 && targetVal > 0

  const placeOrder = (direction: Direction) => {
    if (!lastBar || pos || done || !ticketValid) return
    setPos(openPosition(direction, qty, lastBar, stopPts, { kind: targetMode, value: targetVal }))
  }

  const manualClose = () => {
    if (!pos || !lastBar) return
    recordTrade(closePosition(pos, lastBar.close, lastBar.time, 'manual', pointValue))
    setPos(null)
  }

  // ── overlays: PDH/PDL, London range, NY open (revealed bars only) ─────────

  const etMinutes = useMemo(() => {
    return bars.map((c) => {
      const [h, m] = ET_TIME_FMT.format(new Date(c.time * 1000)).split(':').map(Number)
      return ((h % 24) * 60) + m
    })
  }, [bars])

  const london = useMemo(() => {
    // London session range, 03:00–08:30 ET, built from revealed bars only so
    // the band grows as the session plays out.
    let hi = -Infinity, lo = Infinity
    for (let i = 0; i < revealed && i < etMinutes.length; i++) {
      const m = etMinutes[i]
      if (m >= 180 && m < 510) {
        if (bars[i].high > hi) hi = bars[i].high
        if (bars[i].low < lo) lo = bars[i].low
      }
    }
    return hi > -Infinity ? { hi, lo } : null
  }, [bars, etMinutes, revealed])

  const nyOpenTime = useMemo(() => {
    for (let i = 0; i < revealed && i < etMinutes.length; i++) {
      if (etMinutes[i] >= 570) return bars[i].time // first bar at/after 09:30 ET
    }
    return null
  }, [bars, etMinutes, revealed])

  const hlines = useMemo(() => {
    const out: { price: number; label?: string; color?: string; dash?: number[] }[] = []
    if (day?.pdh != null) out.push({ price: day.pdh, label: 'PDH', color: '#f59e0b', dash: [6, 4] })
    if (day?.pdl != null) out.push({ price: day.pdl, label: 'PDL', color: '#38bdf8', dash: [6, 4] })
    if (pos) {
      out.push({ price: pos.stopPrice, label: 'SL', color: '#ef4444', dash: [3, 3] })
      out.push({ price: pos.targetPrice, label: 'TP', color: '#22c55e', dash: [3, 3] })
    }
    return out
  }, [day, pos])

  const bands = useMemo(
    () => (london ? [{ from: london.lo, to: london.hi, color: 'rgba(139,92,246,0.10)' }] : []),
    [london],
  )
  const vlines = useMemo(
    () => (nyOpenTime != null ? [{ time: nyOpenTime, label: 'NY open', color: '#a78bfa' }] : []),
    [nyOpenTime],
  )

  // View timeframe: client-side aggregation of REVEALED bars only.
  const viewBars = useMemo(() => aggregateBars(revealedBars, tf), [revealedBars, tf])

  const markers = useMemo(() => {
    const out: { time: number; type: 'entry' | 'exit'; direction: string; price: number; is_winner: boolean }[] = []
    for (const t of closed) {
      out.push({ time: t.entryTime, type: 'entry', direction: t.direction, price: t.entryPrice, is_winner: t.dollars > 0 })
      out.push({ time: t.exitTime, type: 'exit', direction: t.direction, price: t.exitPrice, is_winner: t.dollars > 0 })
    }
    if (pos && lastBar) {
      out.push({
        time: pos.entryTime, type: 'entry', direction: pos.direction, price: pos.entryPrice,
        is_winner: unrealized(pos, lastBar.close, pointValue).dollars >= 0,
      })
    }
    return out
  }, [closed, pos, lastBar, pointValue])

  const stats = sessionStats(closed)
  const uPnl = pos && lastBar ? unrealized(pos, lastBar.close, pointValue) : null

  // Recent sessions summary from the persisted log.
  const recentSessions = useMemo(() => {
    const entries = readLog()
    const bySession = new Map<string, { instrument: string; date: string; trades: number; totalR: number; totalDollars: number; last: string }>()
    for (const e of entries) {
      const cur = bySession.get(e.session_id) || { instrument: e.instrument, date: e.date, trades: 0, totalR: 0, totalDollars: 0, last: e.logged_at }
      cur.trades += 1
      cur.totalR += e.r
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

  // ── render ────────────────────────────────────────────────────────────────

  return (
    <div className="p-4 sm:p-8 max-w-6xl">
      {/* HERO */}
      <div className="rounded-3xl bg-gradient-to-br from-slate-900 via-slate-900 to-violet-950 dark:from-slate-950 dark:via-slate-950 dark:to-violet-950 text-white p-6 md:p-8 shadow-xl mb-6">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="min-w-0 flex-1">
            <div className="text-[10px] uppercase tracking-[0.2em] text-violet-300 font-bold mb-1">Practice</div>
            <h1 className="text-2xl md:text-3xl font-extrabold text-white">Replay</h1>
            <p className="text-sm text-slate-400 mt-1">Bar-by-bar practice trading on historical futures days · stop checked before target, fills at level</p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={loadRandomDay} disabled={loadState === 'loading'}
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
          <div className="flex items-end gap-2">
            <button onClick={() => setPlaying((p) => !p)} disabled={!day || done}
              className="inline-flex items-center gap-1.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-white px-3 py-1.5 rounded-lg text-sm font-bold transition-colors">
              {playing ? <Pause size={14}/> : <Play size={14}/>}
              {playing ? 'Pause' : 'Play'}
            </button>
            <button onClick={() => stepRef.current()} disabled={!day || done || playing}
              className="inline-flex items-center gap-1.5 bg-slate-200 hover:bg-slate-300 dark:bg-slate-800 dark:hover:bg-slate-700 disabled:opacity-40 text-slate-700 dark:text-slate-200 px-3 py-1.5 rounded-lg text-sm font-bold transition-colors">
              <StepForward size={14}/> Step
            </button>
          </div>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">Speed</span>
            <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))}
              className="rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 text-sm px-2 py-1.5">
              {SPEEDS.map((s) => <option key={s} value={s}>{s}x</option>)}
            </select>
          </label>
          <div className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">View</span>
            <div className="inline-flex rounded-lg bg-slate-100 dark:bg-slate-800 p-0.5 border border-slate-200 dark:border-slate-700">
              {([1, 5, 15] as const).map((t) => (
                <button key={t} onClick={() => setTf(t)}
                  className={`px-2.5 py-1 rounded-md text-xs font-bold transition-all ${tf === t ? 'bg-white dark:bg-slate-700 text-violet-700 dark:text-violet-300 shadow-sm' : 'text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200'}`}>
                  {t}m
                </button>
              ))}
            </div>
          </div>
          {lastBar && (
            <div className="ml-auto text-right">
              <div className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">
                {dateHidden ? 'Hidden day' : date} · {etClock(lastBar.time)}
              </div>
              <div className="text-sm font-extrabold text-slate-800 dark:text-slate-100">
                {lastBar.close.toFixed(2)} <span className="text-[10px] font-semibold text-slate-400">({progressPct}% of day)</span>
              </div>
            </div>
          )}
        </div>
      </div>

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
        <div className="mb-4">
          <CandlestickChart
            candles={viewBars}
            markers={markers}
            hlines={hlines}
            vlines={vlines}
            bands={bands}
            windowMode="tail"
            maxBars={tf === 1 ? 180 : 200}
            height={420}
          />
        </div>
      )}

      {/* ORDER TICKET + POSITION */}
      {day && !done && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">
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
                <input type="number" min={0.25} step={0.25} value={stopStr} onChange={(e) => setStopStr(e.target.value)}
                  className="w-24 rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 text-sm px-2 py-1.5"/>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">Target</span>
                <div className="flex gap-1">
                  <input type="number" min={0.25} step={0.25} value={targetStr} onChange={(e) => setTargetStr(e.target.value)}
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
              {instrument} point value ${pointValue}/contract · one position at a time
            </div>
          </div>

          <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 dark:bg-slate-900 dark:border-slate-700">
            <div className="text-[11px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500 mb-3">Open position</div>
            {pos && uPnl ? (
              <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
                <span className={`inline-flex items-center gap-1 text-sm font-extrabold ${pos.direction === 'long' ? 'text-green-600' : 'text-red-500'}`}>
                  {pos.direction === 'long' ? <TrendingUp size={14}/> : <TrendingDown size={14}/>}
                  {pos.direction.toUpperCase()} ×{pos.qty}
                </span>
                <span className="text-xs text-slate-500 dark:text-slate-400">Entry <b className="text-slate-800 dark:text-slate-100">{pos.entryPrice.toFixed(2)}</b></span>
                <span className="text-xs text-slate-500 dark:text-slate-400">SL <b className="text-red-500">{pos.stopPrice.toFixed(2)}</b></span>
                <span className="text-xs text-slate-500 dark:text-slate-400">TP <b className="text-green-600">{pos.targetPrice.toFixed(2)}</b></span>
                <span className={`text-sm font-extrabold ${uPnl.dollars >= 0 ? 'text-green-600' : 'text-red-500'}`}>
                  {fmtPts(uPnl.points)} pts · {fmtR(uPnl.r)} · {fmtUsd(uPnl.dollars)}
                </span>
                <button onClick={manualClose}
                  className="inline-flex items-center gap-1 bg-slate-200 hover:bg-slate-300 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-200 px-3 py-1.5 rounded-lg text-xs font-bold transition-colors">
                  <X size={12}/> Close
                </button>
              </div>
            ) : (
              <div className="text-xs text-slate-400 dark:text-slate-500">Flat — place an order to open a position.</div>
            )}
          </div>
        </div>
      )}

      {/* SESSION STATS */}
      {day && (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 mb-4">
          <Stat label="Trades" value={String(stats.trades)} tone={stats.trades === 0 ? 'muted' : undefined}/>
          <Stat label="W / L" value={stats.trades === 0 ? '—' : `${stats.wins} / ${stats.losses}`} tone={stats.trades === 0 ? 'muted' : undefined}/>
          <Stat label="Win rate" value={stats.winRate == null ? '—' : `${stats.winRate.toFixed(0)}%`} tone={stats.winRate == null ? 'muted' : stats.winRate >= 50 ? 'pos' : undefined}/>
          <Stat label="Total R" value={stats.trades === 0 ? '—' : fmtR(stats.totalR)} tone={stats.trades === 0 ? 'muted' : stats.totalR >= 0 ? 'pos' : 'neg'}/>
          <Stat label="Total $" value={stats.trades === 0 ? '—' : fmtUsd(stats.totalDollars)} tone={stats.trades === 0 ? 'muted' : stats.totalDollars >= 0 ? 'pos' : 'neg'}/>
          <Stat label="Avg R" value={stats.avgR == null ? '—' : fmtR(stats.avgR)} tone={stats.avgR == null ? 'muted' : stats.avgR >= 0 ? 'pos' : 'neg'}/>
        </div>
      )}

      {/* DAY-END SUMMARY */}
      {done && day && (
        <div className="rounded-2xl border border-violet-200 dark:border-violet-900/50 bg-violet-50 dark:bg-violet-950/20 p-5 mb-4">
          <div className="flex items-start justify-between gap-4 flex-wrap">
            <div>
              <div className="text-[10px] uppercase tracking-[0.2em] text-violet-500 dark:text-violet-300 font-bold mb-1">Session complete</div>
              <div className="text-lg font-extrabold text-slate-900 dark:text-slate-100">
                {instrument} · {date}{blind && <span className="ml-2 text-xs font-bold text-violet-500 dark:text-violet-300">(blind day revealed)</span>}
              </div>
              <div className="text-sm text-slate-600 dark:text-slate-300 mt-1">
                {stats.trades === 0
                  ? 'No trades taken this session.'
                  : `${stats.trades} trade${stats.trades === 1 ? '' : 's'} · ${stats.wins}W/${stats.losses}L · ${fmtR(stats.totalR)} · ${fmtUsd(stats.totalDollars)}`}
              </div>
            </div>
            <button onClick={loadRandomDay}
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
                    <td className={`px-4 py-2 font-semibold ${t.r >= 0 ? 'text-green-600' : 'text-red-500'}`}>{fmtR(t.r)}</td>
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
                <span className="font-bold text-slate-700 dark:text-slate-200">{s.instrument} · {s.date}</span>
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
