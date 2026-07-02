import type { ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'

export interface SectionHeaderProps {
  title: string
  subtitle?: string
  /** Any lucide icon component — rendered in an accent-tinted tile */
  icon?: LucideIcon
  /** Right-aligned slot for buttons / filters / RefreshButton */
  actions?: ReactNode
  className?: string
}

/**
 * V2 section header — title row with optional icon tile, subtitle and a
 * right-aligned actions slot. Gives every dashboard section the same
 * rhythm so stacked sections scan as one terminal.
 */
export default function SectionHeader({
  title,
  subtitle,
  icon: Icon,
  actions,
  className,
}: SectionHeaderProps) {
  return (
    <div className={`v2-section-header${className ? ` ${className}` : ''}`}>
      <div className="v2-section-header__main">
        {Icon && (
          <span className="v2-section-header__icon">
            <Icon size={16} />
          </span>
        )}
        <div>
          <h2 className="v2-section-header__title">{title}</h2>
          {subtitle && <p className="v2-section-header__subtitle">{subtitle}</p>}
        </div>
      </div>
      {actions && <div className="v2-section-header__actions">{actions}</div>}
    </div>
  )
}
