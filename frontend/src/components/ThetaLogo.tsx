// Inline SVG of the Theta Algos mark — purple-violet ring with the
// tilde/theta wave inside. Renders crisp at any size, theme-aware by
// default (the gradient stops adjust on dark vs light backgrounds).

interface Props {
  size?: number
  showWordmark?: boolean
  className?: string
}

export default function ThetaLogo({ size = 36, showWordmark = false, className = '' }: Props) {
  const id = `theta-${Math.random().toString(36).slice(2, 9)}`
  return (
    <span className={`inline-flex items-center gap-2.5 ${className}`}>
      <svg
        width={size} height={size} viewBox="0 0 100 100"
        xmlns="http://www.w3.org/2000/svg"
        aria-label="Theta Algos"
        className="flex-shrink-0"
      >
        <defs>
          <linearGradient id={`${id}-ring`} x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%"  stopColor="#a855f7" />
            <stop offset="50%" stopColor="#7c3aed" />
            <stop offset="100%" stopColor="#4f46e5" />
          </linearGradient>
          <radialGradient id={`${id}-glow`} cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#7c3aed" stopOpacity="0.45" />
            <stop offset="70%" stopColor="#7c3aed" stopOpacity="0.0" />
          </radialGradient>
        </defs>

        {/* Soft outer halo */}
        <circle cx="50" cy="50" r="48" fill={`url(#${id}-glow)`} />

        {/* Primary ring */}
        <circle
          cx="50" cy="50" r="36"
          fill="none"
          stroke={`url(#${id}-ring)`}
          strokeWidth="4"
        />

        {/* Theta wave — a single sweeping curve through the centre */}
        <path
          d="M 30 52 Q 40 42, 50 52 T 70 52"
          fill="none"
          stroke={`url(#${id}-ring)`}
          strokeWidth="3"
          strokeLinecap="round"
        />
      </svg>
      {showWordmark && (
        <span className="font-extrabold tracking-tight text-slate-900 dark:text-slate-100 leading-none" style={{ fontSize: Math.round(size * 0.55) }}>
          Theta Algos
        </span>
      )}
    </span>
  )
}
