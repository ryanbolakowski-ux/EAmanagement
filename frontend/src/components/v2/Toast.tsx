import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
import { CheckCircle2, Info, X, XCircle } from 'lucide-react'

export type ToastVariant = 'success' | 'error' | 'info'

export interface ToastOptions {
  /** Bold first line above the message */
  title?: string
  /** ms before auto-dismiss. 0 = sticky until dismissed.
   *  Defaults: 4000, errors 6000 (people read failures more slowly). */
  duration?: number
}

export interface ToastApi {
  toast: (message: string, variant?: ToastVariant, opts?: ToastOptions) => string
  success: (message: string, opts?: ToastOptions) => string
  error: (message: string, opts?: ToastOptions) => string
  info: (message: string, opts?: ToastOptions) => string
  dismiss: (id: string) => void
}

interface ToastItem {
  id: string
  message: string
  variant: ToastVariant
  title?: string
  leaving: boolean
}

const ToastContext = createContext<ToastApi | null>(null)

const ICONS: Record<ToastVariant, typeof Info> = {
  success: CheckCircle2,
  error: XCircle,
  info: Info,
}

// Matches --v2-dur-med so the exit animation finishes before unmount
const EXIT_MS = 200
// Cap the stack so a burst of failures doesn't wallpaper the screen
const MAX_VISIBLE = 5

let toastSeq = 0

/**
 * V2 toast system. Mount <ToastProvider> once near the app root, then any
 * descendant calls `const { success, error } = useToast()`. Renders through
 * a body-level portal whose element carries .v2-root itself so the design
 * tokens resolve outside any page wrapper (see styles/v2.css section 15).
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  // Auto-dismiss timers keyed by toast id; exit timers keyed by `${id}:exit`
  const timersRef = useRef(new Map<string, ReturnType<typeof setTimeout>>())

  const dismiss = useCallback((id: string) => {
    const timers = timersRef.current
    const auto = timers.get(id)
    if (auto) {
      clearTimeout(auto)
      timers.delete(id)
    }
    if (timers.has(`${id}:exit`)) return // already animating out
    setToasts(prev => prev.map(t => (t.id === id ? { ...t, leaving: true } : t)))
    const exit = setTimeout(() => {
      timers.delete(`${id}:exit`)
      setToasts(prev => prev.filter(t => t.id !== id))
    }, EXIT_MS)
    timers.set(`${id}:exit`, exit)
  }, [])

  const push = useCallback(
    (message: string, variant: ToastVariant = 'info', opts?: ToastOptions): string => {
      const id = `t${Date.now().toString(36)}-${++toastSeq}`
      const duration = opts?.duration ?? (variant === 'error' ? 6000 : 4000)
      setToasts(prev => {
        const next = [...prev, { id, message, variant, title: opts?.title, leaving: false }]
        // Evict the oldest immediately (no exit animation) past the cap.
        // Its pending timer fires against a missing id, which is a no-op.
        return next.length > MAX_VISIBLE ? next.slice(next.length - MAX_VISIBLE) : next
      })
      if (duration > 0) {
        timersRef.current.set(id, setTimeout(() => dismiss(id), duration))
      }
      return id
    },
    [dismiss],
  )

  // Clear every pending timer if the provider unmounts mid-flight
  useEffect(() => {
    const timers = timersRef.current
    return () => {
      timers.forEach(clearTimeout)
      timers.clear()
    }
  }, [])

  const api = useMemo<ToastApi>(
    () => ({
      toast: push,
      success: (m, o) => push(m, 'success', o),
      error: (m, o) => push(m, 'error', o),
      info: (m, o) => push(m, 'info', o),
      dismiss,
    }),
    [push, dismiss],
  )

  return (
    <ToastContext.Provider value={api}>
      {children}
      {createPortal(
        <div className="v2-root v2-toast-viewport" aria-live="polite">
          {toasts.map(t => {
            const Icon = ICONS[t.variant]
            return (
              <div
                key={t.id}
                role="status"
                className={`v2-toast v2-toast--${t.variant}${t.leaving ? ' v2-toast--leaving' : ''}`}
              >
                <Icon size={16} className="v2-toast__icon" />
                <div className="v2-toast__content">
                  {t.title && <div className="v2-toast__title">{t.title}</div>}
                  <div className="v2-toast__message">{t.message}</div>
                </div>
                <button
                  type="button"
                  className="v2-toast__close"
                  aria-label="Dismiss notification"
                  onClick={() => dismiss(t.id)}
                >
                  <X size={14} />
                </button>
              </div>
            )
          })}
        </div>,
        document.body,
      )}
    </ToastContext.Provider>
  )
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used inside <ToastProvider>')
  return ctx
}
