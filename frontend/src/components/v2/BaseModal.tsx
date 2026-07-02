import { useEffect, useRef, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'

export type ModalSize = 'sm' | 'md' | 'lg' | 'full'

export interface BaseModalProps {
  open: boolean
  onClose: () => void
  title?: ReactNode
  children: ReactNode
  /** Right-aligned action row pinned below the scrollable body */
  footer?: ReactNode
  /** sm 400px / md 560px (default) / lg 760px / full (viewport minus gutter) */
  size?: ModalSize
  /** Clicking the dimmed backdrop closes the modal (default true) */
  closeOnBackdrop?: boolean
  /** Element to focus on open; defaults to the first focusable in the panel */
  initialFocusRef?: RefObject<HTMLElement>
  /** Hide the header X button (escape / backdrop still close) */
  hideClose?: boolean
  className?: string
}

// Everything the Tab key can land on inside the panel
const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), ' +
  'input:not([disabled]):not([type="hidden"]), select:not([disabled]), ' +
  '[tabindex]:not([tabindex="-1"])'

/**
 * V2 modal shell: body-level portal (the overlay element carries .v2-root
 * itself so tokens resolve — see styles/v2.css section 16), focus trap,
 * escape-close, body scroll-lock, and focus restoration on close.
 * Composition: BaseModal owns the chrome, callers own body + footer.
 */
export default function BaseModal({
  open,
  onClose,
  title,
  children,
  footer,
  size = 'md',
  closeOnBackdrop = true,
  initialFocusRef,
  hideClose = false,
  className,
}: BaseModalProps) {
  const panelRef = useRef<HTMLDivElement>(null)
  // Keep the latest onClose reachable from the keydown listener without
  // re-running the whole open/lock/focus effect on every render
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const initialFocusRefRef = useRef(initialFocusRef)
  initialFocusRefRef.current = initialFocusRef

  useEffect(() => {
    if (!open) return
    const previouslyFocused = document.activeElement as HTMLElement | null

    // Scroll-lock <body>, remembering the previous inline value so we
    // restore whatever another component may have set.
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    const focusables = (): HTMLElement[] => {
      const panel = panelRef.current
      if (!panel) return []
      return Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE))
        // offsetParent is null for display:none subtrees — skip them
        .filter(el => el.offsetParent !== null || el === document.activeElement)
    }

    // Initial focus: caller's pick, else first focusable, else the panel
    const target = initialFocusRefRef.current?.current ?? focusables()[0] ?? panelRef.current
    target?.focus()

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onCloseRef.current()
        return
      }
      if (e.key !== 'Tab') return
      // Trap: wrap Tab / Shift+Tab at the panel edges
      const els = focusables()
      if (els.length === 0) {
        e.preventDefault()
        panelRef.current?.focus()
        return
      }
      const first = els[0]
      const last = els[els.length - 1]
      const active = document.activeElement as HTMLElement | null
      const inside = !!active && !!panelRef.current?.contains(active)
      if (e.shiftKey && (active === first || !inside)) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && (active === last || !inside)) {
        e.preventDefault()
        first.focus()
      }
    }
    // Capture phase so the trap wins over listeners deeper in the tree
    document.addEventListener('keydown', onKeyDown, true)

    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      document.body.style.overflow = prevOverflow
      previouslyFocused?.focus?.()
    }
  }, [open])

  if (!open) return null

  return createPortal(
    <div
      className="v2-root v2-modal-overlay"
      onMouseDown={e => {
        // Only a click that STARTS on the backdrop closes — dragging out of
        // an input and releasing over the backdrop must not nuke the form
        if (closeOnBackdrop && e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === 'string' ? title : undefined}
        tabIndex={-1}
        className={`v2-modal v2-modal--${size}${className ? ` ${className}` : ''}`}
      >
        {(title !== undefined || !hideClose) && (
          <div className="v2-modal__header">
            <div className="v2-modal__title">{title}</div>
            {!hideClose && (
              <button
                type="button"
                className="v2-modal__close"
                aria-label="Close dialog"
                onClick={onClose}
              >
                <X size={16} />
              </button>
            )}
          </div>
        )}
        <div className="v2-modal__body">{children}</div>
        {footer && <div className="v2-modal__footer">{footer}</div>}
      </div>
    </div>,
    document.body,
  )
}
