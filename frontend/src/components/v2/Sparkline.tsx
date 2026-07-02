export type SparklineTone = 'auto' | 'up' | 'down' | 'accent'

export interface SparklineProps {
  data: number[]
  width?: number
  height?: number
  strokeWidth?: number
  /** 'auto' (default) colors by last-vs-first; or force up/down/accent */
  tone?: SparklineTone
  /** Soft area fill under the line (default true) */
  fill?: boolean
  className?: string
}

/**
 * Dependency-free inline sparkline. Deliberately NOT recharts: a stat row
 * can render dozens of these, and a raw <svg> path costs ~nothing. Colors
 * come from the v2 tokens so light/dark just work. Decorative — hidden
 * from screen readers; pair with a .v2-num text value for the real data.
 */
export default function Sparkline({
  data,
  width = 96,
  height = 28,
  strokeWidth = 1.5,
  tone = 'auto',
  fill = true,
  className,
}: SparklineProps) {
  if (data.length < 2) return null

  const min = Math.min(...data)
  const max = Math.max(...data)
  const span = max - min || 1 // flat series still draws a midline
  const pad = strokeWidth
  const stepX = (width - pad * 2) / (data.length - 1)

  const points = data.map((v, i) => {
    const x = pad + i * stepX
    const y = pad + (1 - (v - min) / span) * (height - pad * 2)
    return `${x.toFixed(2)} ${y.toFixed(2)}`
  })
  const line = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${p}`).join(' ')
  const area = `${line} L${(pad + (data.length - 1) * stepX).toFixed(2)} ${height} L${pad} ${height} Z`

  const resolved =
    tone === 'auto' ? (data[data.length - 1] >= data[0] ? 'up' : 'down') : tone
  const stroke =
    resolved === 'up' ? 'var(--v2-up)' : resolved === 'down' ? 'var(--v2-down)' : 'var(--v2-accent)'

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      {fill && <path d={area} fill={stroke} opacity={0.08} stroke="none" />}
      <path
        d={line}
        fill="none"
        stroke={stroke}
        strokeWidth={strokeWidth}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}
