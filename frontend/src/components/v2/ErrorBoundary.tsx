import { Component, type ErrorInfo, type ReactNode } from 'react'
import { AlertTriangle, RotateCcw } from 'lucide-react'

export interface ErrorBoundaryProps {
  children: ReactNode
  /** Section name used in the fallback copy, e.g. "Live P&L" */
  title?: string
  /** Fully custom fallback — replaces the default card when provided */
  fallback?: ReactNode
  /** Called when the user clicks Retry (after internal error state resets),
   *  e.g. to invalidate the react-query cache for the crashed section */
  onRetry?: () => void
}

interface ErrorBoundaryState {
  error: Error | null
}

/**
 * V2 error boundary. Wrap each dashboard section so a render crash in one
 * widget degrades to a card instead of white-screening the whole app.
 * Class component because React only exposes error boundaries through the
 * class lifecycle (getDerivedStateFromError / componentDidCatch).
 */
export default class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // No frontend error pipeline yet — console keeps it visible in devtools
    // without adding a dependency. A reporting hook can slot in here later.
    console.error('[v2:ErrorBoundary]', this.props.title ?? 'section', error, info.componentStack)
  }

  private handleRetry = () => {
    this.setState({ error: null })
    this.props.onRetry?.()
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    if (this.props.fallback !== undefined) return this.props.fallback
    return (
      <div className="v2-card v2-error-card" role="alert">
        <div className="v2-error-card__icon">
          <AlertTriangle size={18} />
        </div>
        <div className="v2-error-card__body">
          <div className="v2-type-heading">
            {this.props.title ? `${this.props.title} failed to render` : 'Something went wrong'}
          </div>
          <div className="v2-type-caption v2-error-card__message">
            {error.message || 'Unexpected render error'}
          </div>
        </div>
        <button type="button" className="v2-btn v2-btn--ghost v2-btn--sm" onClick={this.handleRetry}>
          <RotateCcw size={14} />
          Retry
        </button>
      </div>
    )
  }
}
