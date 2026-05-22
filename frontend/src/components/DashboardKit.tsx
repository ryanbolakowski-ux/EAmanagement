/* Shared "Theta Algos pro" design-system primitives.
   These pull the LiveTrading V2 hero/card/sparkline patterns into one
   import so every page can ship the same look without copy-paste. */
import { ReactNode } from 'react'
import { RefreshCw } from 'lucide-react'

// ── Formatting helpers ─────────────────────────────────────────────
export const fmt = (n: number, digits = 2) =>
  n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
export const fmtUsd = (n: number, digits = 2) => `$${fmt(Math.abs(n), digits)}`
export const pnlColor = (n: number) =>
  n > 0 ? 'text-emerald-600 dark:text-emerald-400'
  : n < 0 ? 'text-rose-600 dark:text-rose-400'
  : 'text-slate-500'
export const pnlSign = (n: number) => (n >= 0 ? '+' : '−')

// ── Sparkline ─────────────────────────────────────────────────────
export function Sparkline({ data, color = '#a78bfa', height = 36 }: {
  data: number[]; color?: string; height?: number
}) {
  if (!data || data.length < 2) return <div style={{ height }} className="w-full"/>
  const min = Math.min(...data)
  const max = Math.max(...data)
  const range = max - min || 1
  const step = 100 / (data.length - 1)
  const points = data.map((v, i) => `${i * step},${100 - ((v - min) / range) * 100}`).join(' ')
  const area = `0,100 ${points} 100,100`
  return (
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="w-full" style={{ height }}>
      <polygon points={area} fill={color} opacity="0.10"/>
      <polyline points={points} fill="none" stroke={color} strokeWidth="2"/>
    </svg>
  )
}

// ── HeroHeader: the dark gradient banner ──────────────────────────
export function HeroHeader({
  eyebrow, title, primaryMetric, primaryLabel = 'Primary',
  rightMetric, rightLabel, rightColor,
  meta, sparkline, sparklineColor = '#a78bfa', children,
}: {
  eyebrow?: string
  title: ReactNode
  primaryMetric?: string
  primaryLabel?: string
  rightMetric?: string
  rightLabel?: string
  rightColor?: string
  meta?: string
  sparkline?: number[]
  sparklineColor?: string
  children?: ReactNode
}) {
  return (
    <div className="rounded-3xl bg-gradient-to-br from-slate-900 via-slate-900 to-violet-950 dark:from-slate-950 dark:via-slate-950 dark:to-violet-950 text-white p-6 md:p-8 shadow-xl">
      <div className="flex items-start justify-between mb-4 gap-4">
        <div className="min-w-0 flex-1">
          {eyebrow && (
            <div className="text-[10px] uppercase tracking-[0.2em] text-violet-300 font-bold mb-1">{eyebrow}</div>
          )}
          <div className="text-xl md:text-2xl font-extrabold text-white mb-1">{title}</div>
          {primaryMetric && (
            <div className="text-3xl md:text-4xl font-extrabold tabular-nums mt-1">{primaryMetric}</div>
          )}
          {meta && <div className="text-xs text-slate-400 mt-1">{meta}</div>}
        </div>
        {rightMetric !== undefined && (
          <div className="text-right flex-shrink-0">
            {rightLabel && (
              <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold mb-1">{rightLabel}</div>
            )}
            <div className={`text-2xl md:text-3xl font-extrabold tabular-nums ${rightColor || 'text-white'}`}>
              {rightMetric}
            </div>
          </div>
        )}
      </div>
      {sparkline && sparkline.length > 1 && (
        <div className="mt-3 -mx-2 opacity-80">
          <Sparkline data={sparkline} color={sparklineColor} height={48}/>
        </div>
      )}
      {children && <div className="mt-5 pt-5 border-t border-white/10">{children}</div>}
    </div>
  )
}

// ── MetricRow: a 4-up grid of mini-stats inside the hero ──────────
export function MetricRow({ items }: { items: { label: string; value: string; color?: string }[] }) {
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
      {items.map((it) => (
        <div key={it.label}>
          <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">{it.label}</div>
          <div className={`text-lg font-bold tabular-nums ${it.color || 'text-white'}`}>{it.value}</div>
        </div>
      ))}
    </div>
  )
}

// ── MetricCard: standalone numeric tile for grid layouts ──────────
export function MetricCard({
  label, value, sub, color, icon: Icon, accent = 'violet',
}: {
  label: string
  value: string
  sub?: string
  color?: string
  icon?: any
  accent?: 'violet' | 'emerald' | 'rose' | 'amber' | 'blue' | 'slate'
}) {
  const accents = {
    violet: 'from-violet-100 to-violet-200 text-violet-700 dark:from-violet-900/40 dark:to-violet-800/40 dark:text-violet-300',
    emerald: 'from-emerald-100 to-emerald-200 text-emerald-700 dark:from-emerald-900/40 dark:to-emerald-800/40 dark:text-emerald-300',
    rose: 'from-rose-100 to-rose-200 text-rose-700 dark:from-rose-900/40 dark:to-rose-800/40 dark:text-rose-300',
    amber: 'from-amber-100 to-amber-200 text-amber-700 dark:from-amber-900/40 dark:to-amber-800/40 dark:text-amber-300',
    blue: 'from-blue-100 to-blue-200 text-blue-700 dark:from-blue-900/40 dark:to-blue-800/40 dark:text-blue-300',
    slate: 'from-slate-100 to-slate-200 text-slate-700 dark:from-slate-800/40 dark:to-slate-700/40 dark:text-slate-300',
  }
  return (
    <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-5 hover:shadow-lg transition-shadow">
      {Icon && (
        <div className={`w-10 h-10 rounded-xl bg-gradient-to-br ${accents[accent]} flex items-center justify-center mb-3`}>
          <Icon size={18}/>
        </div>
      )}
      <div className="text-[10px] uppercase tracking-wider text-slate-400 dark:text-slate-500 font-bold mb-1">{label}</div>
      <div className={`text-2xl font-extrabold tabular-nums ${color || 'text-slate-900 dark:text-slate-100'}`}>{value}</div>
      {sub && <div className="text-[11px] text-slate-500 dark:text-slate-400 mt-1">{sub}</div>}
    </div>
  )
}

// ── PeriodTabs ─────────────────────────────────────────────────────
export function PeriodTabs<T extends string>({
  value, onChange, options,
}: { value: T; onChange: (v: T) => void; options: T[] }) {
  return (
    <div className="inline-flex rounded-lg bg-slate-100 dark:bg-slate-800 p-0.5">
      {options.map((o) => (
        <button key={o} onClick={() => onChange(o)}
          className={`px-3 py-1 text-[11px] font-bold rounded uppercase tracking-wider transition-colors ${
            value === o
              ? 'bg-white dark:bg-slate-900 text-violet-700 dark:text-violet-300 shadow-sm'
              : 'text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200'
          }`}>
          {o}
        </button>
      ))}
    </div>
  )
}

// ── RefreshButton ──────────────────────────────────────────────────
export function PageRefresh({ onClick, busy }: { onClick: () => void; busy?: boolean }) {
  return (
    <button onClick={onClick} disabled={busy}
      className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold text-violet-700 dark:text-violet-300 bg-violet-50 dark:bg-violet-900/30 hover:bg-violet-100 dark:hover:bg-violet-900/50 disabled:opacity-50">
      <RefreshCw size={11} className={busy ? 'animate-spin' : ''}/> Refresh
    </button>
  )
}

// ── EmptyState ─────────────────────────────────────────────────────
export function EmptyState({
  icon: Icon, title, body, action,
}: { icon?: any; title: string; body?: string; action?: ReactNode }) {
  return (
    <div className="rounded-2xl border border-dashed border-slate-300 dark:border-slate-700 p-10 text-center">
      {Icon && <Icon size={32} className="mx-auto text-slate-300 dark:text-slate-600 mb-3"/>}
      <p className="font-bold text-slate-600 dark:text-slate-300 mb-1">{title}</p>
      {body && <p className="text-xs text-slate-500 dark:text-slate-400 mb-3 max-w-sm mx-auto">{body}</p>}
      {action}
    </div>
  )
}

// ── SectionHeader ──────────────────────────────────────────────────
export function SectionHeader({
  title, right,
}: { title: string; right?: ReactNode }) {
  return (
    <div className="flex items-center justify-between mb-3">
      <h2 className="text-sm font-extrabold uppercase tracking-wider text-slate-700 dark:text-slate-200">{title}</h2>
      {right}
    </div>
  )
}
