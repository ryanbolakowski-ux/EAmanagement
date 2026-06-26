import { useState } from 'react'
import { Search, TrendingUp, TrendingDown, AlertCircle, Loader2 } from 'lucide-react'
import { scannerApi } from '../api/endpoints'

// On-demand ticker analysis: structure levels (long + short) + the scanner's
// quality-gate verdict + whether it's an actual pick candidate. Read-only;
// explicitly NOT a price prediction.

const fmt = (n: any, d = 2) =>
  n === null || n === undefined || isNaN(Number(n))
    ? '—'
    : Number(n).toLocaleString(undefined, { maximumFractionDigits: d })

// Decision tone -> colors. tone comes from the backend decision_obj.
const DEC_BG: Record<string, string> = {
  buy: 'border-emerald-300 bg-emerald-50 dark:bg-emerald-900/20 dark:border-emerald-800',
  wait: 'border-amber-300 bg-amber-50 dark:bg-amber-900/20 dark:border-amber-800',
  watch: 'border-sky-300 bg-sky-50 dark:bg-sky-900/20 dark:border-sky-800',
  avoid: 'border-red-300 bg-red-50 dark:bg-red-900/20 dark:border-red-800',
  none: 'border-slate-300 bg-slate-50 dark:bg-slate-800/40 dark:border-slate-700',
}
const DEC_TX: Record<string, string> = {
  buy: 'text-emerald-700 dark:text-emerald-300',
  wait: 'text-amber-700 dark:text-amber-300',
  watch: 'text-sky-700 dark:text-sky-300',
  avoid: 'text-red-700 dark:text-red-300',
  none: 'text-slate-600 dark:text-slate-300',
}

function Badge({ tone, children }: { tone: 'green' | 'amber' | 'red' | 'slate'; children: any }) {
  const m: Record<string, string> = {
    green: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300',
    amber: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
    red: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
    slate: 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300',
  }
  return <span className={`px-2 py-0.5 rounded-full text-[11px] font-bold ${m[tone]}`}>{children}</span>
}

function LevelsCard({ label, dir, lv }: { label: string; dir: 'long' | 'short'; lv: any }) {
  if (!lv) return null
  const Icon = dir === 'long' ? TrendingUp : TrendingDown
  const accent = dir === 'long' ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'
  return (
    <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-3">
      <div className="flex items-center justify-between mb-2">
        <div className={`flex items-center gap-1.5 text-xs font-bold ${accent}`}>
          <Icon size={14} /> {label}
        </div>
        <Badge tone="slate">R:R {fmt(lv.rr, 1)} · {fmt(lv.projected_move_pct, 1)}%</Badge>
      </div>
      <div className="space-y-1 text-xs text-slate-700 dark:text-slate-200">
        <div className="flex justify-between"><span className="text-slate-500 dark:text-slate-400">Entry</span><span className="font-bold">${fmt(lv.entry)}</span></div>
        <div className="flex justify-between gap-2"><span className="text-slate-500 dark:text-slate-400">Stop</span><span className="font-bold text-rose-600 dark:text-rose-400 text-right">${fmt(lv.stop)} <span className="text-slate-400 font-normal">({lv.stop_reason})</span></span></div>
        <div className="flex justify-between gap-2"><span className="text-slate-500 dark:text-slate-400">Target</span><span className="font-bold text-emerald-600 dark:text-emerald-400 text-right">${fmt(lv.target)} <span className="text-slate-400 font-normal">({lv.target_reason})</span></span></div>
        <div className="flex justify-between"><span className="text-slate-500 dark:text-slate-400">Basis</span><span className="font-semibold">{lv.basis === 'structure' ? 'structure' : 'ATR fallback'}</span></div>
      </div>
    </div>
  )
}

export default function TickerAnalyzer() {
  const [ticker, setTicker] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [res, setRes] = useState<any>(null)

  async function run() {
    const t = ticker.trim().toUpperCase()
    if (!t) return
    setLoading(true); setError(null); setRes(null)
    try {
      const r = await scannerApi.analyze(t)
      const d = r.data
      if (d?.error) { setError(`${t}: ${d.error}`); setRes(null) }
      else setRes(d)
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Could not analyze that ticker.')
    } finally {
      setLoading(false)
    }
  }

  const gate = res?.gate_long
  const gateTone = gate?.verdict === 'accept' ? 'green' : gate?.verdict === 'watch' ? 'amber' : gate?.verdict === 'reject' ? 'red' : 'slate'
  const sm = res?.scanner_match
  const matchTone = sm?.would_be_pick ? 'green' : sm?.is_candidate ? 'amber' : 'slate'
  const matchText = sm?.would_be_pick ? 'Would be a pick' : sm?.is_candidate ? 'Momentum candidate' : 'No active setup'

  return (
    <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4 shadow-sm">
      <div className="flex items-center gap-2 mb-1">
        <Search size={16} className="text-violet-600 dark:text-violet-400" />
        <h2 className="text-sm font-extrabold text-slate-900 dark:text-slate-100">Analyze any ticker</h2>
      </div>
      <p className="text-[11px] text-slate-500 dark:text-slate-400 mb-3">Live structure levels + scanner verdict. Read-only — not a prediction or guarantee.</p>

      <div className="flex gap-2">
        <input
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          onKeyDown={(e) => { if (e.key === 'Enter') run() }}
          placeholder="e.g. NVDA"
          maxLength={8}
          className="flex-1 px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100 font-bold uppercase tracking-wide focus:outline-none focus:ring-2 focus:ring-violet-400"
        />
        <button
          onClick={run}
          disabled={loading || !ticker.trim()}
          className="px-4 py-2 rounded-lg text-sm font-bold bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white inline-flex items-center gap-1.5">
          {loading ? <Loader2 size={14} className="animate-spin" /> : <Search size={14} />}
          {loading ? 'Analyzing…' : 'Analyze'}
        </button>
      </div>

      {error && (
        <div className="mt-3 rounded-lg p-2.5 text-xs bg-red-50 border border-red-200 text-red-700 dark:bg-red-900/20 dark:border-red-900 dark:text-red-300 flex items-start gap-2">
          <AlertCircle size={14} className="flex-shrink-0 mt-0.5" />{error}
        </div>
      )}

      {res && (
        <div className="mt-4 space-y-3">
          {/* DECISION — the call, front and center */}
          <div className={`rounded-xl border p-3 ${DEC_BG[res.decision?.tone] || DEC_BG.none}`}>
            <div className="flex items-center flex-wrap gap-2">
              <span className={`text-xl font-extrabold ${DEC_TX[res.decision?.tone] || DEC_TX.none}`}>{res.decision?.label || '—'}</span>
              <span className="text-sm font-bold text-slate-900 dark:text-slate-100">{res.ticker} ${fmt(res.price)}</span>
              <span className={`text-sm font-bold ${Number(res.gap_pct) >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                {Number(res.gap_pct) >= 0 ? '+' : ''}{fmt(res.gap_pct, 1)}%
              </span>
            </div>
            <div className="text-xs mt-1 text-slate-700 dark:text-slate-200 font-medium">{res.decision?.reason}</div>
            {res.decision?.tags?.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-2">
                {res.decision.tags.map((t: string) => (
                  <span key={t} className="px-2 py-0.5 rounded-full text-[10px] font-bold bg-white/70 dark:bg-slate-900/50 text-slate-600 dark:text-slate-300 border border-slate-200 dark:border-slate-700">{t}</span>
                ))}
              </div>
            )}
            <div className="flex flex-wrap gap-1.5 mt-2">
              <Badge tone={gateTone}>Gate: {gate?.verdict}</Badge>
              <Badge tone={matchTone}>{matchText}</Badge>
            </div>
          </div>

          {/* stats */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px]">
            <div><div className="text-slate-400">Prev close</div><div className="font-bold text-slate-800 dark:text-slate-100">${fmt(res.prev_close)}</div></div>
            <div><div className="text-slate-400">Rel-vol (vs prev day)</div><div className="font-bold text-slate-800 dark:text-slate-100">{fmt(res.rel_vol_vs_prev_day)}×</div></div>
            <div><div className="text-slate-400">Rel-vol (20d avg)</div><div className="font-bold text-slate-800 dark:text-slate-100">{fmt(res.rel_vol_vs_20d_avg)}×</div></div>
            <div><div className="text-slate-400">$-vol today</div><div className="font-bold text-slate-800 dark:text-slate-100">${fmt(res.today_dollar_vol_musd, 0)}M</div></div>
          </div>

          {/* gate reasons */}
          {gate?.reasons?.length > 0 && (
            <div className="text-[11px] text-slate-500 dark:text-slate-400">Gate: {gate.reasons.join(' · ')}</div>
          )}

          {/* levels both directions */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            <LevelsCard label="Long" dir="long" lv={res.levels_long} />
            <LevelsCard label="Short" dir="short" lv={res.levels_short} />
          </div>

          {/* scanner match detail */}
          {sm?.templates?.length > 0 && (
            <div className="text-[11px] text-slate-600 dark:text-slate-300 space-y-0.5">
              <div className="font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide text-[10px]">Scanner templates</div>
              {sm.templates.map((t: any) => (
                <div key={t.template} className="flex items-start gap-1.5">
                  <span className={t.passes ? 'text-emerald-600' : 'text-slate-400'}>{t.passes ? '✓' : '✕'}</span>
                  <span className="font-semibold">{t.template}</span>
                  {t.fail_reason && <span className="text-slate-400">— {t.fail_reason}</span>}
                </div>
              ))}
            </div>
          )}

          <div className="text-[10px] text-slate-400 dark:text-slate-500 border-t border-slate-100 dark:border-slate-800 pt-2">
            {res.note} · source: {res.price_source}
          </div>
        </div>
      )}
    </div>
  )
}
