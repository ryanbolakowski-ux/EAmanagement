import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  FlaskConical, Play, RotateCcw, Copy, Check, History,
  TrendingUp, AlertTriangle, ChevronRight,
} from 'lucide-react'
import { strategiesApi, backtestsApi } from '../api/endpoints'
import type { Strategy, BacktestRun } from '../types'

// ─────────────────────────────────────────────────────────────────────────────
// Backtest Lab — set up the same way as the Replay page: hero, one controls
// row (instrument → date range → strategy → big Start button), a live running
// card, then a results panel.
//
// Engine: the EXISTING non-blocking backtest API. POST /api/v1/backtests/
// returns 202 with the run row; progress is polled off the list endpoint
// (redis progress merged in) and metrics land once status = completed.
// ─────────────────────────────────────────────────────────────────────────────

const INSTRUMENTS = ['ES', 'NQ', 'YM', 'RTY']

// candle_cache coverage starts here (same lower bound the Replay page reports).
const DATA_START = '2023-04-30'

const LOG_KEY = 'theta_backtest_lab_log'
const LOG_MAX = 10

type LogEntry = {
  run_id: string
  strategy_name: string
  instrument: string
  start_date: string
  end_date: string
  win_rate: number | null
  profit_factor: number | null
  net_profit: number | null
  total_trades: number | null
  finished_at: string
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

function writeLog(entries: LogEntry[]) {
  try {
    localStorage.setItem(LOG_KEY, JSON.stringify(entries.slice(0, LOG_MAX)))
  } catch {
    // Quota/serialization failures shouldn't break the page.
  }
}

// ── date helpers (yyyy-mm-dd strings) ────────────────────────────────────────

const iso = (d: Date) => d.toISOString().split('T')[0]
const yesterday = () => { const d = new Date(); d.setDate(d.getDate() - 1); return iso(d) }
const monthsBack = (from: string, months: number) => {
  const d = new Date(from + 'T00:00:00Z')
  d.setUTCMonth(d.getUTCMonth() - months)
  return iso(d)
}
const clampStart = (s: string, end: string) => (s < DATA_START ? DATA_START : s > end ? end : s)

// "All" is bounded by data start AND the backend's 3-year range cap.
const allStart = (end: string) => {
  const d = new Date(end + 'T00:00:00Z')
  d.setUTCFullYear(d.getUTCFullYear() - 3)
  d.setUTCDate(d.getUTCDate() + 1)
  const threeYr = iso(d)
  return threeYr > DATA_START ? threeYr : DATA_START
}

const PRESETS: { label: string; start: (end: string) => string }[] = [
  { label: '1M', start: (e) => clampStart(monthsBack(e, 1), e) },
  { label: '3M', start: (e) => clampStart(monthsBack(e, 3), e) },
  { label: '6M', start: (e) => clampStart(monthsBack(e, 6), e) },
  { label: '1Y', start: (e) => clampStart(monthsBack(e, 12), e) },
  { label: 'All', start: (e) => allStart(e) },
]

// ── small display helpers ────────────────────────────────────────────────────

const fmtMoney = (v: number) =>
  `${v < 0 ? '−' : ''}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`
const fmtPct = (v: number) => `${(v * 100).toFixed(1)}%`
const fmtTime = (t: string | null) => {
  if (!t) return '—'
  const d = new Date(t)
  if (isNaN(d.getTime())) return t.slice(0, 16).replace('T', ' ')
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
const fmtElapsed = (s: number) => {
  const m = Math.floor(s / 60), sec = s % 60
  return m > 0 ? `${m}m ${sec.toString().padStart(2, '0')}s` : `${sec}s`
}

const isActive = (status?: string) => {
  const s = (status || '').toLowerCase()
  return s === 'running' || s === 'queued' || s === 'pending'
}

// Same stat-tile pattern as the Replay page's Stat component.
function Stat({ label, value, sub, tone }: {
  label: string; value: string; sub?: string; tone?: 'pos' | 'neg' | 'warn' | 'accent'
}) {
  const color = tone === 'pos' ? 'text-green-600 dark:text-green-400'
    : tone === 'neg' ? 'text-red-500'
    : tone === 'warn' ? 'text-amber-600 dark:text-amber-400'
    : tone === 'accent' ? 'text-violet-600 dark:text-violet-400'
    : 'text-slate-900 dark:text-slate-100'
  return (
    <div className="bg-slate-50 rounded-xl border border-slate-200 p-3 dark:bg-slate-900 dark:border-slate-700">
      <div className="text-[10px] text-slate-400 uppercase tracking-wider font-medium mb-1 dark:text-slate-500">{label}</div>
      <div className={`text-lg font-extrabold tabular-nums ${color}`}>{value}</div>
      {sub && <div className="text-[10px] text-slate-400 dark:text-slate-500 mt-0.5">{sub}</div>}
    </div>
  )
}

/** Dependency-free equity line (client-rendered from metrics.equity_curve). */
function EquityLine({ data, height = 130 }: { data: number[]; height?: number }) {
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

// ── page ─────────────────────────────────────────────────────────────────────

export default function BacktestLab() {
  const endBound = useMemo(() => yesterday(), [])

  // Controls
  const [instrument, setInstrument] = useState('ES')
  const [startDate, setStartDate] = useState(() => PRESETS[2].start(yesterday())) // 6M default
  const [endDate, setEndDate] = useState(endBound)
  const [strategyId, setStrategyId] = useState('')
  const [activePreset, setActivePreset] = useState<string | null>('6M')

  // Run
  const [runId, setRunId] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  const [startedAt, setStartedAt] = useState<number | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const [copied, setCopied] = useState(false)
  const [log, setLog] = useState<LogEntry[]>(() => readLog())
  const loggedRef = useRef<Set<string>>(new Set())

  const { data: strategies = [] } = useQuery({
    queryKey: ['strategies'],
    queryFn: () => strategiesApi.list().then(r => r.data as Strategy[]),
  })

  // Poll the runs list while our run is active (redis progress is merged in).
  const { data: runs = [] } = useQuery({
    queryKey: ['backtest-lab-runs'],
    queryFn: () => backtestsApi.list().then(r => r.data as BacktestRun[]),
    enabled: !!runId,
    refetchInterval: (q: any) => {
      const rows: BacktestRun[] = (q?.state?.data as BacktestRun[]) || []
      const mine = rows.find(r => r.id === runId)
      // Keep polling until our run reaches a terminal state (or first appears).
      return !mine || isActive(mine.status) ? 2000 : false
    },
  })

  const run = runs.find(r => r.id === runId)
  const runStatus = (run?.status || (runId ? 'queued' : '')).toLowerCase()
  const running = !!runId && (runStatus === '' || isActive(runStatus))
  const failed = runStatus === 'failed' || runStatus === 'cancelled'
  const completed = runStatus === 'completed'

  const { data: metrics, isLoading: metricsLoading }: any = useQuery({
    queryKey: ['backtest-lab-metrics', runId],
    queryFn: () => backtestsApi.getMetrics(runId!).then(r => r.data),
    enabled: !!runId && completed,
    // The metrics row can trail the status flip by a beat — retry until it lands.
    refetchInterval: (q: any) => (q?.state?.data ? false : 2000),
    retry: 3,
    retryDelay: 1500,
  })

  const { data: trades = [] }: any = useQuery({
    queryKey: ['backtest-lab-trades', runId],
    queryFn: () => backtestsApi.getTrades(runId!).then(r => r.data),
    enabled: !!runId && completed && !!metrics,
  })

  // Elapsed ticker while running
  useEffect(() => {
    if (!running || startedAt == null) return
    const t = setInterval(() => setElapsed(Math.floor((Date.now() - startedAt) / 1000)), 1000)
    return () => clearInterval(t)
  }, [running, startedAt])

  // Log completed runs (once per run id)
  useEffect(() => {
    if (!runId || !completed || !metrics || loggedRef.current.has(runId)) return
    loggedRef.current.add(runId)
    const entry: LogEntry = {
      run_id: runId,
      strategy_name: run?.strategy_name || strategies.find(s => s.id === strategyId)?.name || 'Strategy',
      instrument: run?.instrument || instrument,
      start_date: (run?.start_date || startDate).slice(0, 10),
      end_date: (run?.end_date || endDate).slice(0, 10),
      win_rate: metrics.win_rate ?? null,
      profit_factor: metrics.profit_factor ?? null,
      net_profit: metrics.net_profit ?? null,
      total_trades: metrics.total_trades ?? null,
      finished_at: new Date().toISOString(),
    }
    setLog(prev => {
      const next = [entry, ...prev.filter(e => e.run_id !== runId)].slice(0, LOG_MAX)
      writeLog(next)
      return next
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, completed, metrics])

  async function start() {
    if (!strategyId || starting) return
    setStarting(true); setStartError(null); setCopied(false)
    try {
      const res = await backtestsApi.run({
        strategy_id: strategyId,
        instrument,
        start_date: startDate,
        end_date: endDate,
        initial_capital: 100000,
        commission_per_side: 2.25,
        slippage_ticks: 1,
      })
      setRunId((res.data as BacktestRun).id)
      setStartedAt(Date.now())
      setElapsed(0)
    } catch (e: any) {
      setStartError(e?.response?.data?.detail || e?.message || 'Failed to start backtest')
    } finally {
      setStarting(false)
    }
  }

  function applyPreset(label: string) {
    const p = PRESETS.find(x => x.label === label)
    if (!p) return
    setEndDate(endBound)
    setStartDate(p.start(endBound))
    setActivePreset(label)
  }

  function copySummary() {
    if (!metrics) return
    const name = run?.strategy_name || 'Strategy'
    const lines = [
      `Backtest — ${name} · ${run?.instrument || instrument} · ${(run?.start_date || startDate).slice(0, 10)} → ${(run?.end_date || endDate).slice(0, 10)}`,
      `Trades: ${metrics.total_trades}  (W ${metrics.winning_trades ?? '—'} / L ${metrics.losing_trades ?? '—'}${(metrics.breakeven_trades ?? 0) > 0 ? ` / BE ${metrics.breakeven_trades}` : ''})`,
      `Win rate: ${fmtPct(metrics.win_rate)}${(metrics.breakeven_trades ?? 0) > 0 && metrics.effective_win_rate != null ? `  (effective ${fmtPct(metrics.effective_win_rate)})` : ''}`,
      `Profit factor: ${metrics.profit_factor?.toFixed(2)}`,
      `Net P&L: ${fmtMoney(metrics.net_profit)}`,
      `Max drawdown: ${metrics.max_drawdown_pct?.toFixed(1)}%`,
      `Avg R:R: ${metrics.avg_rr?.toFixed(2)}`,
      metrics.expectancy != null ? `Expectancy: ${fmtMoney(metrics.expectancy)}/trade` : null,
      metrics.sharpe_ratio != null ? `Sharpe: ${metrics.sharpe_ratio.toFixed(2)}` : null,
    ].filter(Boolean)
    navigator.clipboard?.writeText(lines.join('\n')).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }).catch(() => {})
  }

  function viewLogEntry(e: LogEntry) {
    setRunId(e.run_id)
    setStartedAt(null)
    setStartError(null)
    setCopied(false)
  }

  // Strategy grouping (V2 engine vs V1)
  const v2Strats = strategies.filter(s => s.engine_version === 'v2')
  const v1Strats = strategies.filter(s => s.engine_version !== 'v2')
  const grouped = v2Strats.length > 0 && v1Strats.length > 0

  // Same input classes as the Replay controls row.
  const inputCls = 'rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-slate-800 dark:text-slate-100 text-sm px-2 py-1.5'

  const equitySeries: number[] = (metrics?.equity_curve || []).map((p: any) => p.equity)

  return (
    <div className="p-4 sm:p-8 max-w-6xl">
      {/* HERO */}
      <div className="rounded-3xl bg-gradient-to-br from-slate-900 via-slate-900 to-violet-950 dark:from-slate-950 dark:via-slate-950 dark:to-violet-950 text-white p-6 md:p-8 shadow-xl mb-6">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="min-w-0 flex-1">
            <div className="text-[10px] uppercase tracking-[0.2em] text-violet-300 font-bold mb-1">Research</div>
            <h1 className="text-2xl md:text-3xl font-extrabold text-white flex items-center gap-2.5">
              <FlaskConical size={26}/> Backtest Lab
            </h1>
            <p className="text-sm text-slate-400 mt-1">Pick an instrument, a window and a strategy — the engine replays every session and reports the truth</p>
          </div>
          <Link to="/app/backtests"
            className="inline-flex items-center gap-1.5 bg-white/10 hover:bg-white/20 text-white px-4 py-2 rounded-xl text-sm font-bold transition-colors">
            All past runs <ChevronRight size={14}/>
          </Link>
        </div>
      </div>

      {/* CONTROLS ROW — same setup as Replay */}
      <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 dark:bg-slate-900 dark:border-slate-700 mb-4">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">Instrument</span>
            <select value={instrument} onChange={(e) => setInstrument(e.target.value)} className={inputCls}>
              {INSTRUMENTS.map((i) => <option key={i} value={i}>{i}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">From</span>
            <input type="date" value={startDate} min={DATA_START} max={endDate}
              onChange={(e) => { setStartDate(clampStart(e.target.value, endDate)); setActivePreset(null) }}
              className={inputCls}/>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">To</span>
            <input type="date" value={endDate} min={startDate} max={endBound}
              onChange={(e) => { const v = e.target.value > endBound ? endBound : e.target.value; setEndDate(v); setActivePreset(null) }}
              className={inputCls}/>
          </label>
          <div className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">Range</span>
            <div className="inline-flex rounded-lg bg-slate-100 dark:bg-slate-800 p-0.5 border border-slate-200 dark:border-slate-700">
              {PRESETS.map((p) => (
                <button key={p.label} onClick={() => applyPreset(p.label)}
                  className={`px-2.5 py-1 rounded-md text-xs font-bold transition-all ${activePreset === p.label ? 'bg-white dark:bg-slate-700 text-violet-700 dark:text-violet-300 shadow-sm' : 'text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200'}`}>
                  {p.label}
                </button>
              ))}
            </div>
          </div>
          <label className="flex flex-col gap-1 min-w-[180px] flex-1 sm:flex-none sm:w-56">
            <span className="text-[10px] uppercase tracking-wider font-semibold text-slate-400 dark:text-slate-500">Strategy</span>
            <select value={strategyId} onChange={(e) => setStrategyId(e.target.value)} className={inputCls}>
              <option value="">Select a strategy…</option>
              {grouped ? (
                <>
                  <optgroup label="V2 engine">
                    {v2Strats.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                  </optgroup>
                  <optgroup label="V1">
                    {v1Strats.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                  </optgroup>
                </>
              ) : strategies.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </label>
          <button onClick={start} disabled={!strategyId || starting || running}
            className="inline-flex items-center gap-2 bg-violet-600 hover:bg-violet-500 disabled:opacity-40 disabled:cursor-not-allowed text-white px-5 py-2 rounded-xl text-sm font-bold transition-colors shadow-lg shadow-violet-300/40 dark:shadow-violet-900/30 ml-auto">
            <Play size={15}/> {starting ? 'Starting…' : 'Start Backtest'}
          </button>
        </div>
        <p className="text-[10.5px] text-slate-400 dark:text-slate-500 mt-2.5">
          Data from {DATA_START} · timeframes come from the strategy's own configuration · $100k sim account, $2.25/side commission, 1 tick slippage
        </p>
        {startError && (
          <div className="mt-3 rounded-lg border border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-950/30 px-3 py-2 text-xs text-red-700 dark:text-red-300 flex items-center gap-2">
            <AlertTriangle size={14} className="flex-shrink-0"/> {startError}
          </div>
        )}
      </div>

      {/* RUNNING CARD */}
      {running && (
        <div className="rounded-2xl border border-violet-300 dark:border-violet-700 bg-violet-50 dark:bg-violet-950/30 p-5 mb-4">
          <div className="flex items-center gap-3 mb-2 flex-wrap">
            <div className="inline-block w-5 h-5 border-2 border-violet-600 border-r-transparent rounded-full animate-spin flex-shrink-0"/>
            <div className="font-bold text-slate-900 dark:text-slate-100">
              {runStatus === 'queued' || runStatus === '' ? 'Queued…' : 'Backtesting…'}{' '}
              <span className="font-mono tabular-nums">{(run?.progress ?? 0).toFixed(0)}%</span>
            </div>
            {startedAt != null && (
              <div className="text-xs text-slate-500 dark:text-slate-400 font-mono tabular-nums ml-auto">{fmtElapsed(elapsed)} elapsed</div>
            )}
          </div>
          <div className="h-2 bg-slate-200 dark:bg-slate-700 rounded-full overflow-hidden mb-2">
            <div className="h-full bg-gradient-to-r from-violet-500 to-fuchsia-500 transition-all duration-700 ease-out"
              style={{ width: `${Math.max(2, run?.progress ?? 0)}%` }}/>
          </div>
          <div className="text-[11px] text-slate-500 dark:text-slate-400">
            {run?.strategy_name || strategies.find(s => s.id === strategyId)?.name || 'Strategy'} · {run?.instrument || instrument} · {(run?.start_date || startDate).slice(0, 10)} → {(run?.end_date || endDate).slice(0, 10)}
            <span className="mx-1.5">·</span>
            {(run?.progress ?? 0) < 20 ? 'Loading historical data'
              : (run?.progress ?? 0) < 40 ? 'Preparing the strategy engine'
              : (run?.progress ?? 0) < 80 ? 'Iterating bars and simulating trades'
              : 'Computing metrics + writing trades'}
          </div>
        </div>
      )}

      {/* FAILED CARD */}
      {failed && (
        <div className="rounded-2xl border border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-950/30 p-5 mb-4">
          <div className="font-bold text-red-800 dark:text-red-200 mb-1">Backtest {runStatus}</div>
          <div className="text-xs text-red-700 dark:text-red-300 mb-3">
            {(run as any)?.error_message || 'The run did not finish. Try a shorter date range or a different strategy.'}
          </div>
          <button onClick={start} disabled={!strategyId || starting}
            className="bg-red-600 hover:bg-red-500 disabled:opacity-40 text-white text-xs font-bold px-3 py-1.5 rounded-lg inline-flex items-center gap-1.5">
            <RotateCcw size={12}/> Run again
          </button>
        </div>
      )}

      {/* RESULTS */}
      {completed && !metrics && metricsLoading && (
        <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-6 text-center mb-4">
          <div className="inline-block w-6 h-6 border-2 border-violet-500 border-r-transparent rounded-full animate-spin mb-2"/>
          <div className="text-sm text-slate-600 dark:text-slate-300">Loading results…</div>
        </div>
      )}

      {completed && metrics && (
        <div className="space-y-4 mb-4">
          {/* Result header + actions */}
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div className="text-sm font-bold text-slate-900 dark:text-slate-100 min-w-0 truncate">
              {run?.strategy_name || 'Strategy'}
              <span className="text-slate-400 dark:text-slate-500 font-medium"> · {run?.instrument || instrument} · {(run?.start_date || startDate).slice(0, 10)} → {(run?.end_date || endDate).slice(0, 10)}</span>
            </div>
            <div className="flex items-center gap-2 flex-shrink-0">
              <button onClick={copySummary}
                className="inline-flex items-center gap-1.5 text-xs font-bold px-3 py-1.5 rounded-lg bg-slate-200 hover:bg-slate-300 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-200 transition-colors">
                {copied ? <Check size={12} className="text-green-500"/> : <Copy size={12}/>} {copied ? 'Copied' : 'Copy summary'}
              </button>
              <button onClick={start} disabled={!strategyId || starting}
                className="inline-flex items-center gap-1.5 text-xs font-bold px-3 py-1.5 rounded-lg bg-violet-600 hover:bg-violet-500 disabled:opacity-40 text-white transition-colors">
                <RotateCcw size={12}/> Run again
              </button>
            </div>
          </div>

          {/* Stat tiles — only stats the API actually returns */}
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
            <Stat label="Win rate" value={fmtPct(metrics.win_rate)}
              sub={(metrics.breakeven_trades ?? 0) > 0 ? `${metrics.breakeven_trades} BE counted as wins` : undefined}
              tone={metrics.win_rate >= 0.5 ? 'pos' : 'neg'}/>
            {(metrics.breakeven_trades ?? 0) > 0 && metrics.effective_win_rate != null && (
              <Stat label="Effective WR" value={fmtPct(metrics.effective_win_rate)} sub="Excludes break-evens" tone="warn"/>
            )}
            <Stat label="Profit factor" value={metrics.profit_factor?.toFixed(2) ?? '—'}
              tone={metrics.profit_factor >= 1.5 ? 'pos' : metrics.profit_factor >= 1 ? 'warn' : 'neg'}/>
            <Stat label="Net P&L" value={fmtMoney(metrics.net_profit)}
              tone={metrics.net_profit >= 0 ? 'pos' : 'neg'}/>
            <Stat label="Trades" value={String(metrics.total_trades)}
              sub={`W ${metrics.winning_trades ?? '—'} / L ${metrics.losing_trades ?? '—'}${(metrics.breakeven_trades ?? 0) > 0 ? ` / BE ${metrics.breakeven_trades}` : ''}`}/>
            <Stat label="Max drawdown" value={`${metrics.max_drawdown_pct?.toFixed(1)}%`} sub="Peak-to-trough" tone="neg"/>
            <Stat label="Avg R:R" value={metrics.avg_rr?.toFixed(2) ?? '—'} tone="accent"/>
            {metrics.expectancy != null && (
              <Stat label="Expectancy" value={`${fmtMoney(metrics.expectancy)}/trade`}
                tone={metrics.expectancy > 0 ? 'pos' : 'neg'}/>
            )}
            {metrics.sharpe_ratio != null && (
              <Stat label="Sharpe" value={metrics.sharpe_ratio.toFixed(2)}
                tone={metrics.sharpe_ratio >= 1 ? 'pos' : undefined}/>
            )}
            {metrics.avg_win != null && metrics.avg_loss != null && (
              <Stat label="Avg win / loss" value={`${fmtMoney(metrics.avg_win)} / ${fmtMoney(metrics.avg_loss)}`}/>
            )}
          </div>

          {/* Equity curve */}
          {equitySeries.length > 1 && (
            <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 sm:p-5 dark:bg-slate-900 dark:border-slate-700">
              <div className="flex items-center justify-between mb-3">
                <div className="text-sm font-semibold text-slate-700 dark:text-slate-200 flex items-center gap-2">
                  <TrendingUp size={15} className="text-slate-400"/> Equity curve
                </div>
                <div className="text-[11px] text-slate-400 dark:text-slate-500 font-mono tabular-nums">
                  {fmtMoney(equitySeries[0])} → {fmtMoney(equitySeries[equitySeries.length - 1])}
                </div>
              </div>
              <EquityLine data={equitySeries}/>
            </div>
          )}

          {/* Trades table */}
          <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 sm:p-5 dark:bg-slate-900 dark:border-slate-700">
            <div className="text-sm font-semibold text-slate-700 dark:text-slate-200 mb-3">
              Trades <span className="text-slate-400 dark:text-slate-500 font-normal">({trades.length})</span>
            </div>
            {trades.length === 0 ? (
              <div className="text-center text-sm text-slate-400 dark:text-slate-500 py-8 border border-dashed rounded-xl border-slate-300 dark:border-slate-700">
                No trades in this window — the strategy's conditions never lined up.
              </div>
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
                    {trades.map((t: any, i: number) => (
                      <tr key={t.id ?? i} className="border-b border-slate-100 dark:border-slate-800 last:border-0">
                        <td className="py-2 pr-3 text-slate-400 dark:text-slate-500 tabular-nums">{i + 1}</td>
                        <td className="py-2 pr-3">
                          <span className={`font-bold uppercase ${t.direction === 'long' ? 'text-green-600 dark:text-green-400' : 'text-red-500'}`}>
                            {t.direction}
                          </span>
                        </td>
                        <td className="py-2 pr-3 font-mono tabular-nums text-slate-700 dark:text-slate-300">{t.entry_price ?? '—'}</td>
                        <td className="py-2 pr-3 font-mono tabular-nums text-slate-700 dark:text-slate-300">{t.exit_price ?? '—'}</td>
                        <td className="py-2 pr-3 text-slate-500 dark:text-slate-400 whitespace-nowrap">{fmtTime(t.entry_time)}</td>
                        <td className="py-2 pr-3 text-slate-500 dark:text-slate-400 whitespace-nowrap">{fmtTime(t.exit_time)}</td>
                        <td className={`py-2 pr-3 font-mono tabular-nums font-bold text-right ${((t.net_pnl ?? t.pnl) ?? 0) >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-500'}`}>
                          {fmtMoney((t.net_pnl ?? t.pnl) ?? 0)}
                        </td>
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
            <div className="text-xs text-slate-400 dark:text-slate-500 mb-4">Build one first, then come back and test it against history.</div>
            <Link to="/app/strategies" className="inline-flex items-center gap-1.5 bg-violet-600 hover:bg-violet-500 text-white text-xs font-bold px-4 py-2 rounded-lg">
              Go to Strategies <ChevronRight size={13}/>
            </Link>
          </div>
        ) : (
          <div className="text-center py-16 border border-dashed rounded-2xl border-slate-300 dark:border-slate-700 mb-4">
            <Play size={22} className="mx-auto text-slate-300 dark:text-slate-600 mb-2"/>
            <div className="text-sm font-semibold text-slate-600 dark:text-slate-300 mb-1">Ready when you are</div>
            <div className="text-xs text-slate-400 dark:text-slate-500">Pick a strategy above and hit Start Backtest — results land right here.</div>
          </div>
        )
      )}

      {/* RECENT RUNS (localStorage log) */}
      {log.length > 0 && (
        <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 sm:p-5 dark:bg-slate-900 dark:border-slate-700">
          <div className="text-sm font-semibold text-slate-700 dark:text-slate-200 mb-3 flex items-center gap-2">
            <History size={15} className="text-slate-400"/> Recent runs
            <span className="text-[10px] text-slate-400 dark:text-slate-500 font-normal">(this browser, last {LOG_MAX})</span>
          </div>
          <div className="space-y-1.5">
            {log.map((e) => (
              <button key={e.run_id} onClick={() => viewLogEntry(e)}
                className={`w-full text-left rounded-lg border px-3 py-2 transition-colors flex items-center gap-3 flex-wrap ${
                  runId === e.run_id
                    ? 'border-violet-400 dark:border-violet-600 bg-violet-50 dark:bg-violet-950/30'
                    : 'border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 hover:border-violet-300 dark:hover:border-violet-700'
                }`}>
                <span className="text-xs font-bold text-slate-800 dark:text-slate-100 truncate min-w-0 flex-1">{e.strategy_name}</span>
                <span className="text-[10.5px] text-slate-400 dark:text-slate-500 flex-shrink-0">{e.instrument} · {e.start_date} → {e.end_date}</span>
                <span className="text-[11px] font-mono tabular-nums flex-shrink-0">
                  {e.win_rate != null && <span className={e.win_rate >= 0.5 ? 'text-green-600 dark:text-green-400' : 'text-red-500'}>WR {fmtPct(e.win_rate)}</span>}
                  {e.profit_factor != null && <span className="text-slate-500 dark:text-slate-400"> · PF {e.profit_factor.toFixed(2)}</span>}
                  {e.total_trades != null && <span className="text-slate-400 dark:text-slate-500"> · {e.total_trades}t</span>}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
