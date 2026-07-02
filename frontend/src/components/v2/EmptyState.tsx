import type { ReactNode } from 'react'
import { Inbox, type LucideIcon } from 'lucide-react'

export interface EmptyStateAction {
  label: string
  onClick: () => void
}

export interface EmptyStateProps {
  /** Any lucide icon component; defaults to Inbox */
  icon?: LucideIcon
  title: string
  /** One-line explanation of why it's empty / what will fill it */
  hint?: string
  /** Primary CTA rendered as a v2 button */
  action?: EmptyStateAction
  /** Extra content below the action (e.g. a docs link) */
  children?: ReactNode
  className?: string
}

/**
 * V2 empty state — dashed card with icon, title, hint and optional CTA.
 * Use instead of bare "No data" strings so blank sections still explain
 * themselves (see the equity-curve empty copy in TradeMetrics for tone).
 */
export default function EmptyState({
  icon: Icon = Inbox,
  title,
  hint,
  action,
  children,
  className,
}: EmptyStateProps) {
  return (
    <div className={`v2-empty${className ? ` ${className}` : ''}`}>
      <div className="v2-empty__icon">
        <Icon size={20} />
      </div>
      <div className="v2-empty__title">{title}</div>
      {hint && <div className="v2-empty__hint">{hint}</div>}
      {action && (
        <button
          type="button"
          className="v2-btn v2-btn--primary v2-btn--sm v2-empty__action"
          onClick={action.onClick}
        >
          {action.label}
        </button>
      )}
      {children}
    </div>
  )
}
