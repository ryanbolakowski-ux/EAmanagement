import type { ReactNode } from 'react'
import { ArrowDownRight, ArrowUpRight, Minus } from 'lucide-react'
import Sparkline from './Sparkline'

export interface StatCardProps {
  label: string
  /** Preformatted value (use the caller's fmtMoney/fmt helpers).
   *  Rendered with tabular figures so grids of StatCards align. */
  value: ReactNode
  /** Signed change — drives the semantic up/down color + arrow.
   *  null/undefined hides the delta chip entirely; exactly 0 renders flat. */
  delta?: number | null
  /** Formats the delta text (default: signed percent, 2dp) */
  formatDelta?: (delta: number) => string
  /** Small print in the footer, e.g. "vs prior session" */
  hint?: string
  /** Optional series for the inline sparkline (needs ≥ 2 points) */
  sparkline?: number[]
  className?: string
}

const defaultFormatDelta = (d: number) =>
  `${d > 0 ? '+' : d < 0 ? '−' : ''}${Math.abs(d).toFixed(2)}%`

/**
 * V2 stat card — the dense-dashboard workhorse: micro label, big tabular
 * value, semantic delta, optional hint + sparkline footer.
 */
export default function StatCard({
  label,
  value,
  delta,
  formatDelta = defaultFormatDelta,
  hint,
  sparkline,
  className,
}: StatCardProps) {
  const dir = delta == null ? null : delta > 0 ? 'up' : delta < 0 ? 'down' : 'flat'
  const DeltaIcon = dir === 'up' ? ArrowUpRight : dir === 'down' ? ArrowDownRight : Minus

  return (
    <div className={`v2-card v2-stat${className ? ` ${className}` : ''}`}>
      <div className="v2-stat__top">
        <span className="v2-type-micro">{label}</span>
        {dir !== null && delta != null && (
          <span
            className={`v2-stat__delta v2-num ${
              dir === 'up' ? 'v2-up' : dir === 'down' ? 'v2-down' : 'v2-flat'
            }`}
          >
            <DeltaIcon size={12} />
            {formatDelta(delta)}
          </span>
        )}
      </div>
      <div className="v2-stat__value v2-num">{value}</div>
      {(hint || (sparkline && sparkline.length > 1)) && (
        <div className="v2-stat__foot">
          {hint && <span className="v2-stat__hint">{hint}</span>}
          {sparkline && sparkline.length > 1 && (
            <Sparkline data={sparkline} width={88} height={24} />
          )}
        </div>
      )}
    </div>
  )
}
