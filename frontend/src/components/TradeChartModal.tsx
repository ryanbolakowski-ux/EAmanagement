import { useEffect, useState } from 'react'
import { paperTradingApi } from '../api/endpoints'
import Candlestick, { type Candle, type FVG } from './Candlestick'
const _API_BASE = ((import.meta as any).env?.VITE_API_URL || '');

type TradeComment = { id: string; body: string; mark_x: number | null; mark_y: number | null; mark_label: string | null; created_at: string }

type ChartData = {
  id: string
  instrument: string
  direction: string
  entry_price: number | null
  exit_price: number | null
  stop_loss: number | null
  take_profit: number | null
  entry_time: string | null
  exit_time: string | null
  exit_reason: string | null
  net_pnl: number | null
  bias: string | null
  fvg_type: string | null
  primary_tf: string | null
  candles: Candle[]
  candles_by_tf?: Record<string, Candle[]>
  fvgs: FVG[]
}

type StepKey = 'BIAS' | 'STRUCTURE' | 'SWEEP' | 'DISPLACE' | 'FVG' | 'ENTRY' | 'MANAGE' | 'EXIT'

interface Step {
  key: StepKey
  n: number
  label: string
  tf: '4h' | '1h' | '15m' | '5m' | '1m'
  title: string
  detail: string
  showFvgs?: boolean
  showRR?: boolean
  showEntry?: boolean
  showExit?: boolean
}

const STEPS: Step[] = [
  { n: 1, key: 'BIAS', label: 'BIAS', tf: '4h',
    title: 'HTF Bias — what side am I trading?',
    detail: 'Check the 4-hour chart. Are we above or below the 9/21 EMA? The strategy only takes setups in the direction of higher-timeframe momentum — so if the 4h is bullish, we only look for longs today.',
    showFvgs: false, showRR: false, showEntry: false, showExit: false },
  { n: 2, key: 'STRUCTURE', label: 'STRUCTURE', tf: '1h',
    title: 'HTF Structure — where is the draw on liquidity?',
    detail: 'Zoom into 1-hour. Identify the swing highs and lows. Where would buy-stops sit (above swing highs) and sell-stops sit (below swing lows)? That\'s where price wants to go — the "draw on liquidity".',
    showFvgs: false, showRR: false, showEntry: false, showExit: false },
  { n: 3, key: 'SWEEP', label: 'SWEEP', tf: '15m',
    title: 'Wait for the liquidity sweep',
    detail: 'On 15-min, wait for price to take out a recent swing low (longs) or high (shorts). This is the "stop hunt" — smart money grabs retail stops before reversing. We do NOT enter on the sweep itself — we wait for confirmation.',
    showFvgs: false, showRR: false, showEntry: false, showExit: false },
  { n: 4, key: 'DISPLACE', label: 'DISPLACEMENT', tf: '15m',
    title: 'Confirm displacement (institutional momentum)',
    detail: 'After the sweep, the next 15m candle must close with a strong body and full range in the OPPOSITE direction. That\'s institutional displacement — the actual reversal. No displacement = no trade.',
    showFvgs: true, showRR: false, showEntry: false, showExit: false },
  { n: 5, key: 'FVG', label: 'FVG', tf: '5m',
    title: 'Mark the Fair Value Gap',
    detail: 'Drop to 5-min. The displacement leaves a 3-candle imbalance — bar1 high < bar3 low (bullish FVG) or bar1 low > bar3 high (bearish FVG). That\'s our entry zone. Price almost always returns to "tap" the FVG before continuing.',
    showFvgs: true, showRR: false, showEntry: false, showExit: false },
  { n: 6, key: 'ENTRY', label: 'ENTRY', tf: '1m',
    title: 'Entry — 1m FVG tap',
    detail: 'Zoom to 1-min for execution. Enter on the candle that taps the FVG and inverts it. Stop goes just past the sweep low/high. Target = the next untapped HTF FVG or session liquidity.',
    showFvgs: true, showRR: true, showEntry: true, showExit: false },
  { n: 7, key: 'MANAGE', label: 'MANAGE', tf: '1m',
    title: 'Risk management',
    detail: 'After entry, the strategy holds with a fixed stop and target. No moving stop to break-even — research shows BE stops kill the edge. Either the target hits, or the stop does. Time stop at 60 minutes if neither hits.',
    showFvgs: true, showRR: true, showEntry: true, showExit: false },
  { n: 8, key: 'EXIT', label: 'EXIT', tf: '5m',
    title: 'Result — how did the trade resolve?',
    detail: 'Zoom back to 5-min to see the full move from entry to exit. Did target hit cleanly? Did the stop get tapped? Was there a fakeout? Use this to evaluate whether the strategy made the right call.',
    showFvgs: true, showRR: true, showEntry: true, showExit: true },
]

export function TradeChartModal({ tradeId, onClose }: { tradeId: string; onClose: () => void }) {
  const [data, setData] = useState<ChartData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [comments, setComments] = useState<TradeComment[]>([])
  const [newComment, setNewComment] = useState('')
  const [savingComment, setSavingComment] = useState(false)
  const [activeStep, setActiveStep] = useState(0)
  const [isPlaying, setIsPlaying] = useState(false)

  useEffect(() => {
    let cancelled = false
    paperTradingApi.getTradeChart(tradeId)
      .then((res) => { if (!cancelled) setData(res.data) })
      .then(() => {
        fetch(`${_API_BASE}/api/v1/paper-trading/trades/${tradeId}/comments`, {
          headers: { Authorization: `Bearer ${localStorage.getItem('access_token') || ''}` },
        }).then(r => r.ok ? r.json() : []).then((cs: any[]) => { if (!cancelled) setComments(cs || []) }).catch(() => {})
      })
      .catch((e) => {
        if (cancelled) return
        const status = e?.response?.status
        const detail = e?.response?.data?.detail
        let msg = 'Failed to load chart'
        if (status === 404) msg = `Trade not found (404). ID: ${tradeId}`
        else if (status === 500) msg = `Server error loading chart. ID: ${tradeId}. ${detail || ''}`
        else if (detail) msg = `${detail} (HTTP ${status || 'unknown'})`
        setError(msg)
      })
    return () => { cancelled = true }
  }, [tradeId])

  useEffect(() => {
    if (!isPlaying) return
    if (activeStep >= STEPS.length - 1) { setIsPlaying(false); return }
    const t = setTimeout(() => setActiveStep(s => s + 1), 2500)
    return () => clearTimeout(t)
  }, [isPlaying, activeStep])

  const step = STEPS[activeStep]
  const tfCandles = data?.candles_by_tf?.[step.tf] || data?.candles || []

  async function addComment() {
    if (!newComment.trim() || savingComment) return
    setSavingComment(true)
    try {
      const r = await fetch(`${_API_BASE}/api/v1/paper-trading/trades/${tradeId}/comments`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${localStorage.getItem('access_token') || ''}` },
        body: JSON.stringify({ body: newComment.trim(), mark_label: step.label }),
      })
      if (r.ok) {
        setNewComment('')
        const refreshed = await fetch(`${_API_BASE}/api/v1/paper-trading/trades/${tradeId}/comments`, {
          headers: { Authorization: `Bearer ${localStorage.getItem('access_token') || ''}` },
        }).then(r => r.json())
        setComments(refreshed || [])
      }
    } finally { setSavingComment(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onClick={onClose}>
      <div onClick={e => e.stopPropagation()}
        className="w-full max-w-6xl max-h-[92vh] bg-white dark:bg-slate-900 rounded-2xl shadow-2xl border border-slate-200 dark:border-slate-700 flex flex-col overflow-hidden">

        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-slate-200 dark:border-slate-700 flex-shrink-0">
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-violet-600 dark:text-violet-400 font-bold">Trade replay</div>
            {data && (
              <div className="text-base font-extrabold text-slate-900 dark:text-slate-100 flex items-center gap-2">
                {data.instrument}
                <span className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded font-bold ${data.direction === 'long' ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300' : 'bg-rose-100 text-rose-700 dark:bg-rose-900/40 dark:text-rose-300'}`}>{(data.direction || '?').toUpperCase()}</span>
                {data.net_pnl != null && (
                  <span className={`text-sm font-extrabold tabular-nums ${data.net_pnl >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>{data.net_pnl >= 0 ? '+' : '−'}${Math.abs(data.net_pnl).toFixed(2)}</span>
                )}
              </div>
            )}
          </div>
          <div className="flex items-center gap-2">
            {data && (
              <button onClick={() => { setActiveStep(0); setIsPlaying(true) }}
                className="px-3 py-1.5 rounded-md text-[11px] font-bold bg-violet-600 hover:bg-violet-700 text-white">
                {isPlaying ? '◼ Playing' : '▶ Auto-play'}
              </button>
            )}
            <button onClick={onClose} className="text-slate-400 hover:text-slate-700 dark:hover:text-slate-200 text-2xl leading-none">×</button>
          </div>
        </div>

        {/* SCROLLABLE BODY */}
        <div className="flex-1 overflow-y-auto">
          {error && <div className="p-8 text-center text-sm text-rose-500">{error}</div>}
          {!error && !data && <div className="p-12 text-center text-sm text-slate-500">Loading…</div>}

          {data && (
            <>
              {/* Step tabs */}
              <div className="border-b border-slate-200 dark:border-slate-700 px-4 py-2 bg-slate-50 dark:bg-slate-800/40">
                <div className="flex items-stretch gap-0 overflow-x-auto">
                  {STEPS.map((s, i) => (
                    <button key={s.n} onClick={() => { setActiveStep(i); setIsPlaying(false) }}
                      className={`flex-shrink-0 px-3 py-2 text-[10px] font-extrabold uppercase tracking-[0.1em] border-b-2 transition-all ${
                        activeStep === i
                          ? 'border-violet-600 text-violet-700 dark:text-violet-300 bg-white dark:bg-slate-900'
                          : i < activeStep
                            ? 'border-emerald-500/60 text-emerald-700 dark:text-emerald-400 opacity-80'
                            : 'border-transparent text-slate-400 hover:text-slate-700 dark:hover:text-slate-300'
                      }`}>
                      <span className="inline-block mr-1.5 w-5 h-5 rounded-full bg-slate-100 dark:bg-slate-800 text-center text-[10px] leading-5">{s.n}</span>
                      {s.label}
                      <span className="ml-1 text-[9px] text-slate-400 dark:text-slate-500">{s.tf}</span>
                    </button>
                  ))}
                </div>
              </div>

              {/* Active step explainer */}
              <div className="px-5 py-3 bg-violet-50/40 dark:bg-violet-900/10 border-b border-slate-200 dark:border-slate-700">
                <div className="flex items-baseline gap-2 mb-1">
                  <span className="text-[10px] uppercase tracking-widest font-extrabold text-violet-700 dark:text-violet-300">Step {step.n} · {step.tf} timeframe</span>
                </div>
                <h3 className="text-base font-extrabold text-slate-900 dark:text-slate-100 mb-1">{step.title}</h3>
                <p className="text-xs text-slate-600 dark:text-slate-300 leading-relaxed">{step.detail}</p>
              </div>

              {/* The chart */}
              <div className="p-4">
                <Candlestick
                  candles={tfCandles}
                  entry={data.entry_price}
                  stop={data.stop_loss}
                  target={data.take_profit}
                  direction={data.direction}
                  entryTime={data.entry_time}
                  exitTime={data.exit_time}
                  exitPrice={data.exit_price}
                  exitReason={data.exit_reason}
                  fvgs={data.fvgs}
                  showFvgs={!!step.showFvgs}
                  showRRTool={!!step.showRR}
                  showStopTarget={!!step.showRR}
                  showEntry={!!step.showEntry}
                  showExit={!!step.showExit}
                  height={400}
                  title={`${data.instrument} · ${step.tf} candles`}
                  annotations={(() => {
                    // Find entry / exit indices + locate REAL features (FVG, swings)
                    const candles = tfCandles
                    if (!candles || candles.length === 0) return []
                    const fvgs = data.fvgs || []
                    let entryIdx: number | null = null
                    if (data.entry_time) {
                      const tt = new Date(data.entry_time).getTime()
                      let best = Infinity
                      candles.forEach((c: any, i: number) => {
                        const d = Math.abs(new Date(c.t).getTime() - tt)
                        if (d < best) { best = d; entryIdx = i }
                      })
                    }
                    let exitIdxLocal: number | null = null
                    if (data.exit_time) {
                      const tt = new Date(data.exit_time).getTime()
                      let best = Infinity
                      candles.forEach((c: any, i: number) => {
                        const d = Math.abs(new Date(c.t).getTime() - tt)
                        if (d < best) { best = d; exitIdxLocal = i }
                      })
                    }
                    // Real entry FVG (or first FVG that price returned to)
                    const entryFvg = fvgs.find((f: any) => f.is_entry) || (fvgs.length > 0 ? fvgs[0] : null)
                    // Find the candle nearest in PRICE to the FVG's center
                    const fvgCandleIdx = entryFvg ? (() => {
                      let bestI = 0, bestD = Infinity
                      candles.forEach((c: any, i: number) => {
                        const inRange = c.l <= entryFvg.high && c.h >= entryFvg.low
                        const d = inRange ? 0 : Math.min(Math.abs(c.l - entryFvg.high), Math.abs(c.h - entryFvg.low))
                        // Prefer candles BEFORE entry (the FVG forms before the tap)
                        const beforeEntry = entryIdx !== null && i <= entryIdx
                        const score = d + (beforeEntry ? 0 : 0.5)
                        if (score < bestD) { bestD = score; bestI = i }
                      })
                      return bestI
                    })() : (entryIdx !== null ? Math.max(0, entryIdx - 2) : 0)
                    // Real swing low (for long sweep) or high (for short sweep): scan candles for local extreme
                    const lookback = candles.slice(Math.max(0, (entryIdx || 0) - 20), (entryIdx || candles.length))
                    const lookbackStart = Math.max(0, (entryIdx || 0) - 20)
                    let sweepIdx: number | null = null
                    if (lookback.length > 0) {
                      if (data.direction === 'long') {
                        let lowest = Infinity
                        lookback.forEach((c: any, i: number) => { if (c.l < lowest) { lowest = c.l; sweepIdx = lookbackStart + i } })
                      } else {
                        let highest = -Infinity
                        lookback.forEach((c: any, i: number) => { if (c.h > highest) { highest = c.h; sweepIdx = lookbackStart + i } })
                      }
                    }
                    // Recent swing high (above current) + swing low (below) on this TF
                    const N = candles.length
                    let swingHiIdx = 0, swingHi = -Infinity
                    let swingLoIdx = 0, swingLo = Infinity
                    candles.forEach((c: any, i: number) => {
                      if (c.h > swingHi) { swingHi = c.h; swingHiIdx = i }
                      if (c.l < swingLo) { swingLo = c.l; swingLoIdx = i }
                    })
                    const lastIdx = N - 1
                    const annotations: any[] = []
                    if (step.key === 'BIAS') {
                      annotations.push({ candleIdx: lastIdx, side: 'above',
                        label: `HTF bias: ${(data.bias || 'unknown').toUpperCase()}`,
                        color: data.bias === 'bullish' ? '#16a34a' : data.bias === 'bearish' ? '#dc2626' : '#475569',
                        emphasize: true })
                    } else if (step.key === 'STRUCTURE') {
                      annotations.push({ candleIdx: swingHiIdx, side: 'above',
                        priceLevel: swingHi,
                        label: `Swing high \u00b7 ${swingHi.toFixed(2)} (buy-stops above)`, color: '#0ea5e9' })
                      annotations.push({ candleIdx: swingLoIdx, side: 'below',
                        priceLevel: swingLo,
                        label: `Swing low \u00b7 ${swingLo.toFixed(2)} (sell-stops below)`, color: '#0ea5e9' })
                    } else if (step.key === 'SWEEP' && sweepIdx !== null) {
                      const sc = candles[sweepIdx]
                      annotations.push({ candleIdx: sweepIdx,
                        side: data.direction === 'long' ? 'below' : 'above',
                        priceLevel: data.direction === 'long' ? sc.l : sc.h,
                        label: `Liquidity swept here \u00b7 ${data.direction === 'long' ? sc.l.toFixed(2) : sc.h.toFixed(2)}`,
                        color: '#f59e0b', emphasize: true })
                    } else if (step.key === 'DISPLACE' && entryIdx !== null) {
                      // Displacement = largest body candle before entry
                      let dispIdx = Math.max(0, entryIdx - 1), dispScore = 0
                      for (let i = Math.max(0, entryIdx - 6); i <= entryIdx; i++) {
                        const c = candles[i]
                        if (!c) continue
                        const body = Math.abs(c.c - c.o)
                        if (body > dispScore) { dispScore = body; dispIdx = i }
                      }
                      annotations.push({ candleIdx: dispIdx, side: 'above',
                        label: 'Displacement \u2014 strongest body before entry',
                        color: '#7c3aed', emphasize: true })
                    } else if (step.key === 'FVG' && entryFvg) {
                      annotations.push({ candleIdx: fvgCandleIdx, side: 'above',
                        priceLevel: (entryFvg.high + entryFvg.low) / 2,
                        label: `FVG \u00b7 ${entryFvg.low.toFixed(2)}-${entryFvg.high.toFixed(2)} (${entryFvg.direction})`,
                        color: '#7c3aed', emphasize: true })
                    } else if (step.key === 'FVG' && !entryFvg) {
                      annotations.push({ candleIdx: entryIdx ?? lastIdx, side: 'above',
                        label: 'No FVG snapshot stored for this trade',
                        color: '#64748b' })
                    } else if (step.key === 'ENTRY' && entryIdx !== null) {
                      annotations.push({ candleIdx: entryIdx, side: 'above',
                        priceLevel: data.entry_price || undefined,
                        label: `ENTRY $${(data.entry_price || 0).toFixed(2)} \u2014 FVG tapped`,
                        color: '#7c3aed', emphasize: true })
                      if (data.stop_loss) {
                        annotations.push({ candleIdx: entryIdx, side: 'below',
                          priceLevel: data.stop_loss,
                          label: `Stop $${data.stop_loss.toFixed(2)} (past sweep)`, color: '#dc2626' })
                      }
                      if (data.take_profit) {
                        const tpIdx = data.direction === 'long' ? swingHiIdx : swingLoIdx
                        annotations.push({ candleIdx: tpIdx, side: 'above',
                          priceLevel: data.take_profit,
                          label: `Target $${data.take_profit.toFixed(2)} (next swing)`, color: '#16a34a' })
                      }
                    } else if (step.key === 'MANAGE' && entryIdx !== null) {
                      annotations.push({ candleIdx: entryIdx, side: 'above',
                        priceLevel: data.entry_price || undefined,
                        label: 'In position \u2014 fixed stop + target',
                        color: '#7c3aed' })
                    } else if (step.key === 'EXIT' && exitIdxLocal !== null) {
                      const winner = data.exit_reason === 'tp_hit'
                      annotations.push({ candleIdx: exitIdxLocal,
                        side: winner ? 'above' : 'below',
                        priceLevel: data.exit_price || undefined,
                        label: winner
                          ? `\u2705 Target hit at $${(data.exit_price || 0).toFixed(2)}`
                          : `\u274C Stop hit at $${(data.exit_price || 0).toFixed(2)}`,
                        color: winner ? '#16a34a' : '#dc2626', emphasize: true })
                    }
                    return annotations
                  })()}
                />
              </div>

              {/* Comments */}
              <div className="px-5 py-4 border-t border-slate-200 dark:border-slate-700">
                <div className="text-[10px] font-extrabold uppercase tracking-[0.18em] text-slate-500 dark:text-slate-400 mb-2">Notes &amp; coach-the-bot</div>
                <p className="text-[11px] text-slate-500 dark:text-slate-400 mb-3 leading-relaxed">
                  Saw the bot do something wrong? Add a note. Notes are tagged to the current step ({step.label}) so we can correlate complaints to specific stages.
                </p>
                <div className="space-y-2 mb-3 max-h-44 overflow-y-auto">
                  {comments.length === 0 && <p className="text-xs text-slate-400 italic">No notes yet.</p>}
                  {comments.map(c => (
                    <div key={c.id} className="rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/50 p-3">
                      <p className="text-sm text-slate-700 dark:text-slate-200 whitespace-pre-wrap">{c.body}</p>
                      {c.mark_label && <span className="inline-block mt-1 text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300">at step: {c.mark_label}</span>}
                      <div className="text-[10px] text-slate-400 mt-1">{new Date(c.created_at).toLocaleString()}</div>
                    </div>
                  ))}
                </div>
                <div className="flex gap-2 items-start">
                  <textarea value={newComment} onChange={e => setNewComment(e.target.value)}
                    placeholder={`At step "${step.label}", I noticed...`}
                    className="flex-1 px-3 py-2 text-sm rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 resize-none" rows={2}/>
                  <button onClick={addComment} disabled={!newComment.trim() || savingComment}
                    className="px-4 py-2 rounded-lg text-sm font-bold bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white whitespace-nowrap">
                    {savingComment ? 'Saving…' : 'Add note'}
                  </button>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
