import { useEffect, useRef, useState } from 'react'
import type { DailyBias } from '../../api/endpoints'

/**
 * BiasLock — DashboardV2 "Directional bias" panel: a brief LOCKING
 * DIRECTIONAL BIAS computing sequence that steps through REAL factors from
 * the daily-bias payload (GET /api/v1/dashboard/bias — the ICT engine in
 * backend app/engines/ict_bias.py), then stamps the locked bias.
 *
 * Honesty contract (same as EngineField): every line rendered here comes
 * straight from fields the page already fetched — trend + EMA spread, PDH /
 * PDL and their sweep state, the Asian range, the current session and the
 * draw-on-liquidity target. Nothing is invented; factors whose fields are
 * missing are simply skipped.
 *
 * Behavior:
 *   • On mount — and again ONLY when the bias VALUE flips (last locked value
 *     kept in a ref) — plays a ~2.5s sequence: factor lines materialize one
 *     by one under a scanline sweep, then the BIAS LOCKED stamp lands in the
 *     direction color with the lock time in ET.
 *   • After the sequence the card stays locked, showing the factors as a
 *     compact list (bias refetches every 5 min; same value → no replay).
 *   • prefers-reduced-motion: skips straight to the locked state (and
 *     v2.css section 18 freezes the keyframes as a second belt-and-braces).
 *
 * CSS lives in styles/v2.css section 24 (.v2-biaslock__*) — transitions and
 * keyframes only, no canvas, no rAF loop.
 */

export interface BiasLockProps {
  /** One instrument's payload from GET /api/v1/dashboard/bias. Tolerant:
   *  every field is optional and missing factors are skipped. */
  data?: Partial<DailyBias> | null
  className?: string
}

const SESSION_LABEL: Record<string, string> = {
  asian: 'Asia', london: 'London', ny: 'NY', overnight: 'overnight',
}

/** Price levels formatted like the rest of the page (fmtPx convention). */
const fmtLvl = (v: number | null | undefined): string | null =>
  v == null
    ? null
    : v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

const fmtSpread = (v: number): string =>
  `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(2)}%`

/** Build the factor lines — REAL payload fields only, missing ones skipped. */
function buildFactors(d: Partial<DailyBias>): string[] {
  const lines: string[] = []

  const trend = d.trend ?? d.bias
  if (trend) {
    const spread = d.trend_strength_pct ?? d.strength_pct
    lines.push(spread != null
      ? `trend: ${trend.replace(/_/g, ' ')} (EMA spread ${fmtSpread(spread)})`
      : `trend: ${trend.replace(/_/g, ' ')}`)
  }

  const pdh = fmtLvl(d.pdh)
  if (pdh != null) lines.push(`PDH ${pdh} — ${d.pdh_swept ? 'swept' : 'intact'}`)
  const pdl = fmtLvl(d.pdl)
  if (pdl != null) lines.push(`PDL ${pdl} — ${d.pdl_swept ? 'swept' : 'intact'}`)

  const asianHi = fmtLvl(d.asian_high)
  const asianLo = fmtLvl(d.asian_low)
  if (asianHi != null && asianLo != null) lines.push(`Asia range ${asianLo}–${asianHi}`)

  if (d.current_session && d.current_session !== 'unknown') {
    lines.push(`session: ${SESSION_LABEL[d.current_session] ?? d.current_session}`)
  }

  if (d.draw_target) {
    const lvl = fmtLvl(d.draw_target.level)
    lines.push(lvl != null
      ? `draw: ${d.draw_target.label} ${lvl}`
      : `draw: ${d.draw_target.label}`)
  }

  return lines
}

const etFromIso = (iso: string): string | null => {
  try {
    const d = new Date(iso)
    if (isNaN(d.getTime())) return null
    return d.toLocaleTimeString('en-US', {
      hour12: false, hour: '2-digit', minute: '2-digit',
      timeZone: 'America/New_York',
    }) + ' ET'
  } catch { return null }
}

const etNow = (): string =>
  new Date().toLocaleTimeString('en-US', {
    timeZone: 'America/New_York', hour12: false,
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  }) + ' ET'

const LINE_STEP_MS = 340   // per-factor reveal cadence
const SEQ_LEAD_MS = 220    // beat before the first line
const STAMP_LAG_MS = 260   // beat between last line and the stamp

export default function BiasLock({ data, className = '' }: BiasLockProps) {
  const bias = data?.bias ?? null

  const [phase, setPhase] = useState<'locking' | 'locked'>('locked')
  const [visible, setVisible] = useState(0)
  const [lockedAt, setLockedAt] = useState<string | null>(null)
  const lastLockedRef = useRef<DailyBias['bias'] | null>(null)
  const timersRef = useRef<number[]>([])

  // The engine's own timestamp — the honest "as of" for the lock stamp
  // (the client render clock would imply the ENGINE decided at page load).
  const lockStamp = () => (data?.as_of ? etFromIso(data.as_of) : null)

  useEffect(() => {
    if (!bias) return
    if (lastLockedRef.current === bias) return // same value → no replay
    lastLockedRef.current = bias

    // Cancel any in-flight sequence before starting a new one.
    timersRef.current.forEach(t => window.clearTimeout(t))
    timersRef.current = []

    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      setPhase('locked')
      setLockedAt(lockStamp())
      return
    }

    const n = buildFactors(data ?? {}).length
    setPhase('locking')
    setVisible(0)
    for (let i = 1; i <= n; i++) {
      timersRef.current.push(window.setTimeout(
        () => setVisible(i), SEQ_LEAD_MS + (i - 1) * LINE_STEP_MS,
      ))
    }
    timersRef.current.push(window.setTimeout(() => {
      setPhase('locked')
      setLockedAt(lockStamp())
    }, SEQ_LEAD_MS + n * LINE_STEP_MS + STAMP_LAG_MS)) // 6 factors ≈ 2.5s
    // Cleanup belongs to THIS effect (StrictMode dev double-invoke runs
    // mount→cleanup→mount: a separate unmount-only cleanup cancelled the
    // timers while the ref guard blocked rescheduling — animation deadlock).
    // Resetting the ref lets the re-run schedule a fresh sequence.
    return () => {
      timersRef.current.forEach(t => window.clearTimeout(t))
      timersRef.current = []
      lastLockedRef.current = null
    }
    // Re-run only on a bias flip — factor levels updating under the same
    // bias must NOT replay the sequence.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bias])

  if (!data || !bias) {
    return (
      <div className={`v2-card p-4 h-full v2-biaslock ${className}`}>
        <div className="v2-biaslock__head">
          <span className="v2-biaslock__title">Directional bias</span>
        </div>
        <div className="v2-biaslock__idle">awaiting bias data</div>
      </div>
    )
  }

  const factors = buildFactors(data)
  const dir = bias.endsWith('bullish') ? 'bull' : bias.endsWith('bearish') ? 'bear' : 'flat'
  const locking = phase === 'locking'

  return (
    <div
      className={`v2-card p-4 h-full v2-biaslock ${locking ? 'v2-biaslock--locking' : 'v2-biaslock--locked'} ${className}`}
    >
      <div className="v2-biaslock__head">
        <span className="v2-biaslock__title">
          {locking ? 'Locking directional bias' : 'Directional bias'}
        </span>
        {data.instrument && <span className="v2-biaslock__inst v2-num">{data.instrument}</span>}
      </div>

      <div className="v2-biaslock__body">
        {locking && <div className="v2-biaslock__scanline" aria-hidden="true" />}

        <ul className="v2-biaslock__list">
          {factors.map((f, i) => (locking && i >= visible) ? null : (
            <li key={f} className={`v2-biaslock__line v2-num ${locking ? 'v2-biaslock__line--in' : ''}`}>
              <span className="v2-biaslock__tick" aria-hidden="true">▸</span>
              {f}
            </li>
          ))}
          {locking && visible < factors.length && (
            <li className="v2-biaslock__line v2-num" aria-hidden="true">
              <span className="v2-biaslock__cursor" />
            </li>
          )}
        </ul>

        {!locking && (
          <>
            <div className={`v2-biaslock__stamp v2-biaslock__stamp--${dir}`} role="status">
              <span>BIAS LOCKED — {bias.replace(/_/g, ' ').toUpperCase()}</span>
              {lockedAt && <span className="v2-biaslock__stamp-time v2-num">{lockedAt}</span>}
            </div>
            {data.narrative && <p className="v2-biaslock__narrative">{data.narrative}</p>}
          </>
        )}
      </div>
    </div>
  )
}
