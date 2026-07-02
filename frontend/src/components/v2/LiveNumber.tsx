import { useEffect, useRef, useState } from 'react'

export interface LiveNumberProps {
  value: number
  /** Formats every rendered frame (default: en-US locale, ≤ 2 decimals).
   *  Keep it cheap — it runs ~once per animation frame during a tween. */
  format?: (v: number) => string
  /** Tween length in ms (default 320 = --v2-dur-slow). 0 disables. */
  duration?: number
  className?: string
}

const defaultFormat = (v: number) =>
  v.toLocaleString('en-US', { maximumFractionDigits: 2 })

/** ease-out cubic — fast start, gentle landing; the JS twin of --v2-ease */
const easeOut = (t: number) => 1 - Math.pow(1 - t, 3)

/**
 * Count-tween for live values (P&L, equity, prices). When `value` changes
 * the displayed number glides from the old value over `duration` ms via
 * requestAnimationFrame. Respects prefers-reduced-motion (snaps instantly),
 * and always renders with tabular figures so the width doesn't jitter.
 */
export default function LiveNumber({
  value,
  format = defaultFormat,
  duration = 320,
  className,
}: LiveNumberProps) {
  const [display, setDisplay] = useState(value)
  // Where the tween currently is — a ref so an interrupted tween restarts
  // from the on-screen number, not the stale target
  const displayRef = useRef(value)
  const rafRef = useRef<number | null>(null)

  useEffect(() => {
    const from = displayRef.current
    const reduced =
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches

    if (
      reduced ||
      duration <= 0 ||
      from === value ||
      !Number.isFinite(from) ||
      !Number.isFinite(value)
    ) {
      displayRef.current = value
      setDisplay(value)
      return
    }

    const start = performance.now()
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / duration)
      const current = t >= 1 ? value : from + (value - from) * easeOut(t)
      displayRef.current = current
      setDisplay(current)
      if (t < 1) rafRef.current = requestAnimationFrame(step)
    }
    rafRef.current = requestAnimationFrame(step)

    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current)
    }
  }, [value, duration])

  return <span className={`v2-num${className ? ` ${className}` : ''}`}>{format(display)}</span>
}
