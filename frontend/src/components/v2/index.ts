/**
 * V2 component kit — barrel. Stage 2/3 screens import from here:
 *   import { StatCard, useToast, BaseModal } from '../components/v2'
 * Design tokens + the classes these components use live in styles/v2.css.
 */
export { default as ErrorBoundary } from './ErrorBoundary'
export type { ErrorBoundaryProps } from './ErrorBoundary'

export { ToastProvider, useToast } from './Toast'
export type { ToastApi, ToastOptions, ToastVariant } from './Toast'

export { default as BaseModal } from './BaseModal'
export type { BaseModalProps, ModalSize } from './BaseModal'

export { default as Skeleton } from './Skeleton'
export type { SkeletonProps, SkeletonVariant } from './Skeleton'

export { default as EmptyState } from './EmptyState'
export type { EmptyStateAction, EmptyStateProps } from './EmptyState'

export { default as StatCard } from './StatCard'
export type { StatCardProps } from './StatCard'

export { default as LiveNumber } from './LiveNumber'
export type { LiveNumberProps } from './LiveNumber'

export { default as Sparkline } from './Sparkline'
export type { SparklineProps, SparklineTone } from './Sparkline'

export { default as SectionHeader } from './SectionHeader'
export type { SectionHeaderProps } from './SectionHeader'

export { sanitizeHtml } from './sanitizeHtml'

export { default as TickerTape } from './TickerTape'
export type { TickerTapeProps, TickerQuote } from './TickerTape'

export { default as EngineField } from './EngineField'
export type { EngineFieldProps } from './EngineField'
