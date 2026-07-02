export type SkeletonVariant = 'line' | 'card' | 'table'

export interface SkeletonProps {
  variant?: SkeletonVariant
  /** line: number of stacked lines (default 1; the last line shortens to
   *  60% when there's more than one, so it reads as paragraph text) */
  lines?: number
  /** table: body rows (default 5) */
  rows?: number
  /** table: columns (default 4) */
  cols?: number
  /** Outer width/height overrides — numbers are px */
  width?: number | string
  height?: number | string
  className?: string
}

const px = (v: number | string | undefined): string | undefined =>
  typeof v === 'number' ? `${v}px` : v

/**
 * V2 loading placeholder. Shimmer comes from .v2-skeleton in styles/v2.css;
 * prefers-reduced-motion collapses it to a static block there, so this
 * component needs no motion logic of its own.
 */
export default function Skeleton({
  variant = 'line',
  lines = 1,
  rows = 5,
  cols = 4,
  width,
  height,
  className,
}: SkeletonProps) {
  const style = { width: px(width), height: px(height) }
  const extra = className ? ` ${className}` : ''

  if (variant === 'card') {
    return (
      <div className={`v2-card v2-skeleton-card${extra}`} style={style} role="status" aria-label="Loading">
        <div className="v2-skeleton v2-skeleton__line" style={{ width: '40%' }} />
        <div className="v2-skeleton v2-skeleton__block" />
        <div className="v2-skeleton v2-skeleton__line" style={{ width: '75%' }} />
      </div>
    )
  }

  if (variant === 'table') {
    const gridStyle = { gridTemplateColumns: `repeat(${cols}, 1fr)` }
    return (
      <div className={`v2-card v2-skeleton-table${extra}`} style={style} role="status" aria-label="Loading">
        <div className="v2-skeleton-table__row v2-skeleton-table__row--head" style={gridStyle}>
          {Array.from({ length: cols }, (_, c) => (
            <div key={c} className="v2-skeleton v2-skeleton__cell" />
          ))}
        </div>
        {Array.from({ length: rows }, (_, r) => (
          <div key={r} className="v2-skeleton-table__row" style={gridStyle}>
            {Array.from({ length: cols }, (_, c) => (
              <div key={c} className="v2-skeleton v2-skeleton__cell" />
            ))}
          </div>
        ))}
      </div>
    )
  }

  // 'line'
  return (
    <div className={`v2-skeleton-group${extra}`} style={style} role="status" aria-label="Loading">
      {Array.from({ length: lines }, (_, i) => (
        <div
          key={i}
          className="v2-skeleton v2-skeleton__line"
          style={lines > 1 && i === lines - 1 ? { width: '60%' } : undefined}
        />
      ))}
    </div>
  )
}
