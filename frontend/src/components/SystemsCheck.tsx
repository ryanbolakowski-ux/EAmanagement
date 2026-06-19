/**
 * Admin Systems Check dashboard.
 *
 * Renders a comprehensive live view of every backend subsystem (scanners,
 * emails, trading, integrations, infra, recent errors, running jobs,
 * metrics) plus three safe-action buttons:
 *
 *   1. Send Test Heartbeat
 *   2. Send Test Trade Email (stock / futures / options)
 *   3. Run Scanner Health Check
 *
 * The route is gated server-side: every fetch hits an is_admin check on the
 * backend. The frontend guard (parent Admin.tsx already requires admin) is
 * UX only — the real authorization is in the API.
 *
 * Auto-refreshes every 30s. Dark mode + mobile responsive.
 */
import { useEffect, useState, useCallback } from 'react'
import {
  Activity, RefreshCw, AlertTriangle, CheckCircle2, AlertCircle,
  Cpu, Mail, Briefcase, Plug, Database, Bell, ListTree, BarChart3,
  Send, Beaker, Stethoscope,
} from 'lucide-react'

const API = ((import.meta as any).env?.VITE_API_URL || '') + '/api/v1/admin'

type StatusColor = 'green' | 'yellow' | 'red' | 'unknown'
type SCError = {
  component: string; label: string; section: string
  severity: 'critical' | 'warning'; status: string; message: string
  at?: string; affected?: string | null; last_success?: string | null
  auto_fixable: boolean; fix_action?: string | null; manual_instructions?: string | null
}

interface SystemsPayload {
  overall: { status: StatusColor; summary: string; checked_at?: string; error_count?: number; critical_count?: number }
  errors?: SCError[]
  scanners: Record<string, any>
  emails: Record<string, any>
  trading: Record<string, any>
  integrations: Record<string, any>
  infra: Record<string, any> & { stuck_runs?: number }
  recent_errors: Array<{ at: string; logger: string; message: string; level: string }>
  jobs_running: Array<{ name: string; started_at: string | null; expected_completion: string | null }>
  metrics: Record<string, any>
}

// ── Status-to-color util ────────────────────────────────────────────────
function statusClasses(s: StatusColor | string | undefined): string {
  switch (s) {
    case 'green': return 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-400/40'
    case 'yellow': return 'bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-400/40'
    case 'red': return 'bg-red-500/10 text-red-600 dark:text-red-400 border-red-400/40'
    default: return 'bg-slate-500/10 text-slate-600 dark:text-slate-400 border-slate-400/30'
  }
}
function statusGlow(s: StatusColor | string | undefined): string {
  switch (s) {
    case 'green': return 'shadow-emerald-400/30'
    case 'yellow': return 'shadow-amber-400/30'
    case 'red': return 'shadow-red-400/40'
    default: return 'shadow-slate-400/20'
  }
}
function StatusDot({ status }: { status: StatusColor | string | undefined }) {
  const color = status === 'green' ? 'bg-emerald-500' :
                status === 'yellow' ? 'bg-amber-500' :
                status === 'red' ? 'bg-red-500' : 'bg-slate-400'
  return <span className={`inline-block w-2.5 h-2.5 rounded-full ${color} ring-2 ring-white dark:ring-slate-900`} />
}
function fmtTs(ts: string | null | undefined): string {
  if (!ts) return '—'
  try {
    const d = new Date(ts)
    return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch { return ts || '—' }
}

// ── Reusable subsystem card ─────────────────────────────────────────────
function CategoryCard({
  title, icon: Icon, status, children, defaultOpen = true,
}: { title: string; icon: any; status?: StatusColor | string; children: React.ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className={`bg-white dark:bg-slate-900 border ${statusClasses(status)} rounded-2xl overflow-hidden`}>
      <button onClick={() => setOpen(o => !o)} className="w-full flex items-center justify-between px-5 py-3 border-b border-slate-200 dark:border-slate-800 hover:bg-slate-50 dark:hover:bg-slate-800/40">
        <div className="flex items-center gap-3">
          <Icon size={16} className="text-slate-500 dark:text-slate-400" />
          <span className="font-extrabold text-slate-900 dark:text-slate-100 text-sm">{title}</span>
        </div>
        <div className="flex items-center gap-2">
          <StatusDot status={status} />
          <span className="text-[10px] uppercase font-bold tracking-wider text-slate-500 dark:text-slate-400">{open ? 'Hide' : 'Show'}</span>
        </div>
      </button>
      {open && <div className="px-5 py-4 space-y-2 text-xs text-slate-700 dark:text-slate-300">{children}</div>}
    </div>
  )
}

function KV({ k, v, status }: { k: string; v: React.ReactNode; status?: StatusColor | string }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1.5 border-b border-slate-100 dark:border-slate-800/60 last:border-0">
      <span className="text-slate-500 dark:text-slate-400 font-medium">{k}</span>
      <span className="flex items-center gap-2 font-mono text-slate-900 dark:text-slate-100 tabular-nums truncate">
        {status && <StatusDot status={status} />}
        {v}
      </span>
    </div>
  )
}

// ── Subcategory renderer: a {status: '...', ...} dict ───────────────────
function SubsystemBlock({ name, data }: { name: string; data: any }) {
  if (!data) return null
  const { status, ...rest } = data
  const fields = Object.entries(rest).filter(([k]) => k !== 'next_run_at' || rest[k])
  return (
    <div className="bg-slate-50 dark:bg-slate-800/40 border border-slate-200 dark:border-slate-800 rounded-xl p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-bold text-slate-700 dark:text-slate-200">{name}</span>
        <StatusDot status={status} />
      </div>
      <div className="space-y-1">
        {fields.map(([k, v]) => {
          let display: any = v
          if (v === null || v === undefined) display = '—'
          else if (typeof v === 'object') display = JSON.stringify(v)
          else if (typeof v === 'string' && /\d{4}-\d{2}-\d{2}T/.test(v)) display = fmtTs(v as string)
          else display = String(v)
          return (
            <div key={k} className="flex items-center justify-between gap-2 text-[11px]">
              <span className="text-slate-500 dark:text-slate-400">{k}</span>
              <span className="font-mono text-slate-900 dark:text-slate-100 tabular-nums truncate max-w-[180px]">{String(display)}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Action button helpers ───────────────────────────────────────────────
type ActionResult = { ok: boolean; text: string; at: number } | null

function ActionPanel({ token }: { token: string | null }) {
  const [busy, setBusy] = useState<string | null>(null)
  const [tradeAsset, setTradeAsset] = useState<'stock' | 'futures' | 'options'>('stock')
  const [results, setResults] = useState<Record<string, ActionResult>>({})

  const post = useCallback(async (key: string, path: string, body?: any) => {
    if (!token) return
    setBusy(key)
    try {
      const r = await fetch(API + path, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined,
      })
      const j: any = await r.json().catch(() => ({}))
      const ok = r.ok && (j.sent !== false)
      const text = ok
        ? (j.message_id ? `Sent · id=${String(j.message_id).slice(0, 14)}…` : (j.recipient ? `Sent to ${j.recipient}` : 'OK'))
        : (j.detail || j.error || `Failed (${r.status})`)
      setResults(prev => ({ ...prev, [key]: { ok, text, at: Date.now() } }))
    } catch (e: any) {
      setResults(prev => ({ ...prev, [key]: { ok: false, text: e?.message || 'Network error', at: Date.now() } }))
    } finally {
      setBusy(null)
    }
  }, [token])

  function Btn({ kkey, label, icon: Icon, onClick, tone = 'violet' }: any) {
    const r = results[kkey]
    const tones: Record<string, string> = {
      violet: 'bg-violet-600 hover:bg-violet-700',
      blue: 'bg-blue-600 hover:bg-blue-700',
      emerald: 'bg-emerald-600 hover:bg-emerald-700',
    }
    return (
      <div className="flex-1 min-w-[200px]">
        <button onClick={onClick} disabled={busy === kkey}
          className={`w-full inline-flex items-center justify-center gap-2 ${tones[tone]} disabled:opacity-50 text-white font-bold text-xs px-3 py-2.5 rounded-xl shadow-sm`}>
          <Icon size={14} />
          {busy === kkey ? 'Running…' : label}
        </button>
        {r && (
          <div className={`mt-2 text-[11px] font-mono px-2 py-1.5 rounded-md border ${
            r.ok ? 'bg-emerald-50 dark:bg-emerald-900/20 border-emerald-300 dark:border-emerald-700 text-emerald-700 dark:text-emerald-300'
                 : 'bg-red-50 dark:bg-red-900/20 border-red-300 dark:border-red-700 text-red-700 dark:text-red-300'
          }`}>{r.text}</div>
        )}
      </div>
    )
  }

  return (
    <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-5">
      <div className="flex items-center gap-2 mb-1">
        <Beaker size={16} className="text-violet-600" />
        <h3 className="font-extrabold text-slate-900 dark:text-slate-100 text-sm">Admin controls</h3>
      </div>
      <p className="text-xs text-slate-500 dark:text-slate-400 mb-4">
        Safe-actions only — none of these reach real subscribers. Test emails route to your admin inbox.
      </p>
      <div className="flex flex-wrap gap-3">
        <Btn kkey="hb" label="Send Test Heartbeat" icon={Bell} tone="violet"
             onClick={() => post('hb', '/send-test-heartbeat')} />
        <div className="flex-1 min-w-[260px]">
          <div className="flex items-center gap-2 mb-2">
            <select value={tradeAsset} onChange={e => setTradeAsset(e.target.value as any)}
              className="bg-white dark:bg-slate-800 border border-slate-300 dark:border-slate-700 rounded-lg text-xs px-2 py-1.5 font-semibold flex-shrink-0">
              <option value="stock">Stock</option>
              <option value="futures">Futures</option>
              <option value="options">Options</option>
            </select>
            <button onClick={() => post('te', '/send-test-trade-email', { asset_class: tradeAsset })} disabled={busy === 'te'}
              className="flex-1 inline-flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white font-bold text-xs px-3 py-2 rounded-lg">
              <Send size={14} />{busy === 'te' ? 'Sending…' : 'Send Test Trade Email'}
            </button>
          </div>
          {results['te'] && (
            <div className={`mt-1 text-[11px] font-mono px-2 py-1.5 rounded-md border ${
              results['te']!.ok ? 'bg-emerald-50 dark:bg-emerald-900/20 border-emerald-300 dark:border-emerald-700 text-emerald-700 dark:text-emerald-300'
                                : 'bg-red-50 dark:bg-red-900/20 border-red-300 dark:border-red-700 text-red-700 dark:text-red-300'
            }`}>{results['te']!.text}</div>
          )}
        </div>
        <Btn kkey="hc" label="Run Scanner Health Check" icon={Stethoscope} tone="emerald"
             onClick={() => post('hc', '/run-scanner-health-check')} />
      </div>
    </div>
  )
}

// ── Main page ───────────────────────────────────────────────────────────
export default function SystemsCheck({ token }: { token: string | null }) {
  const [data, setData] = useState<SystemsPayload | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [lastFetch, setLastFetch] = useState<Date | null>(null)
  const [running, setRunning] = useState(false)
  const [lastRun, setLastRun] = useState<{ at?: string; by?: string; overall?: string } | null>(null)
  const [showErrors, setShowErrors] = useState(false)
  const [fixBusy, setFixBusy] = useState<string | null>(null)
  const [fixMsg, setFixMsg] = useState<Record<string, { ok: boolean; message: string }>>({})

  const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {}

  const fetchData = useCallback(async () => {
    if (!token) return
    setLoading(true)
    try {
      const r = await fetch(API + '/systems-check', { headers })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const j: SystemsPayload = await r.json()
      setData(j); setErr(null); setLastFetch(new Date())
    } catch (e: any) {
      setErr(e?.message || 'fetch failed')
    } finally { setLoading(false) }
  }, [token])

  const runFull = useCallback(async () => {
    if (!token) return
    setRunning(true)
    try {
      const r = await fetch(API + '/systems-check/run', { method: 'POST', headers })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const j: any = await r.json()
      setData(j); setErr(null); setLastFetch(new Date())
      if (j.last_run) setLastRun(j.last_run)
    } catch (e: any) {
      setErr(e?.message || 'run failed')
    } finally { setRunning(false) }
  }, [token])

  const fetchLast = useCallback(async () => {
    if (!token) return
    try {
      const r = await fetch(API + '/systems-check/last', { headers })
      if (r.ok) { const j = await r.json(); if (j.last_run) setLastRun(j.last_run) }
    } catch { /* ignore */ }
  }, [token])

  const runFix = useCallback(async (action: string) => {
    if (!token) return
    setFixBusy(action)
    try {
      const r = await fetch(API + '/systems-check/fix', {
        method: 'POST', headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
      })
      const j = await r.json()
      setFixMsg(m => ({ ...m, [action]: { ok: !!j.ok, message: j.message || (r.ok ? 'Done' : `HTTP ${r.status}`) } }))
      fetchData()
    } catch (e: any) {
      setFixMsg(m => ({ ...m, [action]: { ok: false, message: e?.message || 'fix failed' } }))
    } finally { setFixBusy(null) }
  }, [token, fetchData])

  useEffect(() => {
    fetchData()
    fetchLast()
    const i = setInterval(fetchData, 30000)
    return () => clearInterval(i)
  }, [fetchData])

  const overallStatus = data?.overall?.status || 'unknown'
  const overallSummary = data?.overall?.summary || 'Loading…'

  return (
    <div className="space-y-6">
      {/* Overall card */}
      <div className={`bg-white dark:bg-slate-900 border-2 ${statusClasses(overallStatus)} rounded-2xl p-6 shadow-xl ${statusGlow(overallStatus)}`}>
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="flex items-center gap-4">
            <div className={`w-14 h-14 rounded-2xl flex items-center justify-center ${
              overallStatus === 'green' ? 'bg-emerald-500/20' :
              overallStatus === 'yellow' ? 'bg-amber-500/20' :
              overallStatus === 'red' ? 'bg-red-500/20' : 'bg-slate-500/20'
            }`}>
              {overallStatus === 'green' ? <CheckCircle2 className="text-emerald-600 dark:text-emerald-400" size={28}/>
                : overallStatus === 'red' ? <AlertCircle className="text-red-600 dark:text-red-400" size={28}/>
                : <AlertTriangle className="text-amber-600 dark:text-amber-400" size={28}/>}
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-[0.2em] font-extrabold text-slate-500 dark:text-slate-400">Overall status</div>
              <div className="text-3xl font-extrabold text-slate-900 dark:text-slate-100 capitalize leading-tight">{overallStatus}</div>
              <div className="text-sm text-slate-600 dark:text-slate-400 mt-1">{overallSummary}</div>
            </div>
          </div>
          <div className="flex flex-col items-end gap-2">
            <div className="text-[10px] uppercase tracking-wider font-bold text-slate-400">Last check</div>
            <div className="font-mono text-sm tabular-nums text-slate-700 dark:text-slate-300">
              {lastFetch ? lastFetch.toLocaleTimeString() : '—'}
            </div>
            {lastRun && (
              <div className="text-[10px] text-slate-500 dark:text-slate-400 text-right">
                Last full run: <span className="font-mono">{lastRun.at ? new Date(lastRun.at).toLocaleString() : '—'}</span>
                {lastRun.by ? <> · by <span className="font-semibold">{lastRun.by}</span></> : null}
              </div>
            )}
            <button onClick={runFull} disabled={running || loading}
              className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white font-extrabold text-xs px-4 py-2.5 rounded-xl">
              <Stethoscope size={14} className={running ? 'animate-pulse' : ''} />
              {running ? 'Running full check…' : 'Run Full Systems Check'}
            </button>
            <button onClick={fetchData} disabled={loading}
              className="inline-flex items-center gap-2 bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white font-bold text-xs px-3 py-2 rounded-xl">
              <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
              {loading ? 'Refreshing…' : 'Refresh now'}
            </button>
            {(data?.errors?.length ?? 0) > 0 && (
              <button onClick={() => setShowErrors(v => !v)}
                className="inline-flex items-center gap-2 bg-slate-200 dark:bg-slate-800 hover:bg-slate-300 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-200 font-bold text-xs px-3 py-2 rounded-xl">
                {showErrors ? 'Hide' : 'Show'} Errors ({data?.errors?.length})
              </button>
            )}
          </div>
        </div>
        {err && <div className="mt-3 text-xs text-red-600 dark:text-red-400 font-mono">Error: {err}</div>}
      </div>

      {/* Error detail / Fix panel */}
      {showErrors && data?.errors && data.errors.length > 0 && (
        <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-2xl p-4 space-y-3">
          <div className="text-xs font-extrabold uppercase tracking-wider text-slate-500 dark:text-slate-400">Flagged components ({data.errors.length})</div>
          {data.errors.map((e, idx) => (
            <div key={idx} className={`rounded-xl border p-3 ${e.severity === 'critical' ? 'border-red-300 dark:border-red-700 bg-red-50/50 dark:bg-red-900/10' : 'border-amber-300 dark:border-amber-700 bg-amber-50/50 dark:bg-amber-900/10'}`}>
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <div className="flex items-center gap-2">
                  <span className={`text-[10px] font-extrabold uppercase px-2 py-0.5 rounded ${e.severity === 'critical' ? 'bg-red-200 text-red-800 dark:bg-red-900/50 dark:text-red-300' : 'bg-amber-200 text-amber-800 dark:bg-amber-900/50 dark:text-amber-300'}`}>{e.severity}</span>
                  <span className="font-bold text-sm text-slate-800 dark:text-slate-100">{e.label}</span>
                  <span className="text-[10px] text-slate-400">({e.component})</span>
                </div>
                {e.auto_fixable && e.fix_action && (
                  <button onClick={() => runFix(e.fix_action!)} disabled={fixBusy === e.fix_action}
                    className="inline-flex items-center gap-1.5 bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white font-bold text-[11px] px-3 py-1.5 rounded-lg">
                    {fixBusy === e.fix_action ? 'Fixing…' : 'Fix'}
                  </button>
                )}
                {!(e.auto_fixable && e.fix_action) && (
                  <span className="text-[10px] font-bold uppercase tracking-wide px-2 py-1 rounded bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-300" title="This is a configuration item — it can't be auto-fixed. See the instructions below.">Not auto-fixable</span>
                )}
              </div>
              <div className="text-xs text-slate-600 dark:text-slate-300 mt-1.5">{e.message}</div>
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-500 dark:text-slate-400 mt-1.5">
                {e.affected && <span>Affected: {e.affected}</span>}
                {e.at && <span>Detected: {new Date(e.at).toLocaleTimeString()}</span>}
                {e.last_success && <span>Last OK: {new Date(e.last_success).toLocaleString()}</span>}
              </div>
              {e.manual_instructions && !e.auto_fixable && (
                <div className="text-[11px] text-slate-600 dark:text-slate-300 mt-1.5 bg-slate-100 dark:bg-slate-800 rounded p-2"><b>To fix:</b> {e.manual_instructions}</div>
              )}
              {e.fix_action && fixMsg[e.fix_action] && (
                <div className={`text-[11px] mt-1.5 font-mono ${fixMsg[e.fix_action].ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}`}>{fixMsg[e.fix_action].message}</div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Admin controls */}
      <ActionPanel token={token} />

      {data && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* SCANNERS */}
          <CategoryCard title="Scanners" icon={Activity}
                        status={worstOf(data.scanners)}>
            <div className="space-y-2">
              {Object.entries(data.scanners).map(([k, v]) => <SubsystemBlock key={k} name={k} data={v} />)}
            </div>
          </CategoryCard>

          {/* EMAILS */}
          <CategoryCard title="Emails" icon={Mail}
                        status={worstOfFlat(data.emails, ['trade_alert_status','futures_email_status','options_swing_status','heartbeat_status'])}>
            <KV k="sent today" v={String(data.emails.sent_today)} />
            <KV k="suppressed today" v={String(data.emails.suppressed_today)} />
            <KV k="trade alert" v={data.emails.trade_alert_status} status={data.emails.trade_alert_status} />
            <KV k="futures email" v={data.emails.futures_email_status} status={data.emails.futures_email_status} />
            <KV k="options swing" v={data.emails.options_swing_status} status={data.emails.options_swing_status} />
            <KV k="heartbeat" v={data.emails.heartbeat_status} status={data.emails.heartbeat_status} />
            {data.emails.last_successful && (
              <details className="mt-2 text-[11px]">
                <summary className="cursor-pointer text-emerald-600 dark:text-emerald-400 font-bold">Last successful send</summary>
                <pre className="bg-slate-50 dark:bg-slate-800 p-2 rounded mt-1 overflow-x-auto">{JSON.stringify(data.emails.last_successful, null, 2)}</pre>
              </details>
            )}
            {data.emails.last_failed && (
              <details className="text-[11px]">
                <summary className="cursor-pointer text-red-600 dark:text-red-400 font-bold">Last failed send</summary>
                <pre className="bg-slate-50 dark:bg-slate-800 p-2 rounded mt-1 overflow-x-auto">{JSON.stringify(data.emails.last_failed, null, 2)}</pre>
              </details>
            )}
          </CategoryCard>

          {/* TRADING */}
          <CategoryCard title="Trading" icon={Briefcase} status={worstOf(data.trading)}>
            <div className="space-y-2">
              {Object.entries(data.trading).map(([k, v]) => <SubsystemBlock key={k} name={k} data={v} />)}
            </div>
          </CategoryCard>

          {/* INTEGRATIONS */}
          <CategoryCard title="Integrations" icon={Plug} status={worstOf(data.integrations)}>
            <div className="space-y-2">
              {Object.entries(data.integrations).map(([k, v]) => <SubsystemBlock key={k} name={k} data={v} />)}
            </div>
          </CategoryCard>

          {/* INFRA */}
          <CategoryCard title="Infrastructure" icon={Database}
                        status={worstOfFlat(data.infra, ['database','redis','queue','scheduler'].map(k => k))}>
            {['database','redis','queue','scheduler'].map(k => data.infra[k] ? <SubsystemBlock key={k} name={k} data={data.infra[k]} /> : null)}
            <KV k="stuck runs" v={String(data.infra.stuck_runs ?? 0)} status={(data.infra.stuck_runs ?? 0) > 0 ? 'red' : 'green'} />
          </CategoryCard>

          {/* JOBS RUNNING */}
          <CategoryCard title="Jobs running" icon={ListTree}
                        status={data.jobs_running.length > 0 ? 'yellow' : 'green'}>
            {data.jobs_running.length === 0 && <div className="text-slate-500 text-[11px]">No jobs currently running.</div>}
            {data.jobs_running.map((j, i) => (
              <div key={i} className="bg-slate-50 dark:bg-slate-800/40 border border-slate-200 dark:border-slate-800 rounded-lg p-2 text-[11px]">
                <div className="font-mono font-bold text-slate-900 dark:text-slate-100 truncate">{j.name}</div>
                <div className="text-slate-500 dark:text-slate-400">started {fmtTs(j.started_at)}</div>
              </div>
            ))}
          </CategoryCard>

          {/* RECENT ERRORS — full-width */}
          <div className="lg:col-span-2">
            <CategoryCard title="Recent errors" icon={AlertTriangle}
                          status={data.recent_errors.length > 0 ? 'red' : 'green'}>
              {data.recent_errors.length === 0 && <div className="text-slate-500 text-[11px]">No errors in buffer.</div>}
              <div className="space-y-1">
                {data.recent_errors.map((e, i) => (
                  <div key={i} className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800/50 rounded-lg p-2 text-[11px]">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono font-bold text-red-700 dark:text-red-300">{e.level}</span>
                      <span className="font-mono text-slate-500 dark:text-slate-400">{fmtTs(e.at)}</span>
                    </div>
                    <div className="font-mono text-[10px] text-slate-500 dark:text-slate-400 truncate">{e.logger}</div>
                    <div className="text-slate-800 dark:text-slate-100 mt-0.5 break-words">{e.message}</div>
                  </div>
                ))}
              </div>
            </CategoryCard>
          </div>

          {/* METRICS */}
          <div className="lg:col-span-2">
            <CategoryCard title="Metrics" icon={BarChart3} status="green">
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                {Object.entries(data.metrics).map(([k, v]) => {
                  let display: any = v
                  if (v === null || v === undefined) display = '—'
                  else if (typeof v === 'string' && /\d{4}-\d{2}-\d{2}T/.test(v)) display = fmtTs(v as string)
                  else display = String(v)
                  return (
                    <div key={k} className="bg-slate-50 dark:bg-slate-800/40 border border-slate-200 dark:border-slate-800 rounded-lg p-3">
                      <div className="text-[10px] uppercase tracking-wider font-bold text-slate-500 dark:text-slate-400">{k.replace(/_/g, ' ')}</div>
                      <div className="text-base font-extrabold text-slate-900 dark:text-slate-100 mt-1 tabular-nums truncate">{String(display)}</div>
                    </div>
                  )
                })}
              </div>
            </CategoryCard>
          </div>
        </div>
      )}
    </div>
  )
}

// Pick worst status across nested {status} dicts inside a category.
function worstOf(obj: Record<string, any>): StatusColor {
  const order: Record<string, number> = { red: 3, yellow: 2, green: 1, unknown: 0 }
  let worst: StatusColor = 'unknown'
  for (const v of Object.values(obj)) {
    if (v && typeof v === 'object' && typeof v.status === 'string') {
      if ((order[v.status] || 0) > (order[worst] || 0)) worst = v.status as StatusColor
    }
  }
  return worst === 'unknown' ? 'green' : worst
}
function worstOfFlat(obj: Record<string, any>, keys: string[]): StatusColor {
  const order: Record<string, number> = { red: 3, yellow: 2, green: 1, unknown: 0 }
  let worst: StatusColor = 'unknown'
  for (const k of keys) {
    const v = obj[k]
    if (v && typeof v === 'object' && typeof v.status === 'string') {
      if ((order[v.status] || 0) > (order[worst] || 0)) worst = v.status as StatusColor
    } else if (typeof v === 'string' && order[v] !== undefined) {
      if (order[v] > (order[worst] || 0)) worst = v as StatusColor
    }
  }
  return worst === 'unknown' ? 'green' : worst
}
