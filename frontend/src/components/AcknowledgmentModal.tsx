import { useState, useRef, useEffect } from 'react'
import { ShieldAlert, X } from 'lucide-react'
import api from '../api/client'

interface Props {
  title: string
  body?: string               // plain-text body (legacy callers)
  bodyHtml?: string           // HTML body (preferred for new disclosure docs)
  kind: string                // matches CURRENT_VERSIONS on the backend
  acceptLabel?: string
  declineLabel?: string
  detail?: string             // freeform extra context recorded with the acknowledgment
  requireScroll?: boolean     // user must scroll to bottom before button enables
  skipServerAck?: boolean     // skip the POST (used pre-registration before auth)
  onAccept: () => void
  onDecline: () => void
}

export default function AcknowledgmentModal({
  title, body, bodyHtml, kind, acceptLabel = 'I understand and agree',
  declineLabel = 'Cancel', detail, requireScroll = false,
  skipServerAck = false, onAccept, onDecline,
}: Props) {
  const [scrolledToEnd, setScrolledToEnd] = useState(!requireScroll)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  // If the doc fits in the modal without needing to scroll (short content
  // or tall screen), auto-mark as scrolled. Otherwise users get stuck with
  // a disabled "agree" button and no way to satisfy the scroll requirement.
  // We re-check after a microtask in case the HTML body renders late.
  useEffect(() => {
    if (!requireScroll || scrolledToEnd) return
    const check = () => {
      const el = scrollRef.current
      if (!el) return
      // 4-px slop accounts for sub-pixel rounding on retina screens
      if (el.scrollHeight - el.clientHeight <= 4) {
        setScrolledToEnd(true)
      }
    }
    // Run once now and again after the next paint (body may not be measured yet)
    check()
    const id = window.setTimeout(check, 50)
    return () => window.clearTimeout(id)
  }, [requireScroll, scrolledToEnd, body, bodyHtml])

  function handleScroll(e: React.UIEvent<HTMLDivElement>) {
    const el = e.currentTarget
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 16) {
      setScrolledToEnd(true)
    }
  }

  async function accept() {
    if (skipServerAck) {
      // Caller is responsible for logging later (e.g. pre-auth registration).
      onAccept()
      return
    }
    setSubmitting(true); setError(null)
    try {
      await api.post('/api/v1/legal/acknowledge', { kind, detail })
      onAccept()
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Could not record acknowledgment. Try again.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[100] bg-black/70 flex items-center justify-center p-4">
      <div className="w-full max-w-2xl bg-white dark:bg-slate-900 rounded-2xl shadow-2xl flex flex-col max-h-[90vh]">
        <div className="px-6 py-4 border-b border-slate-200 dark:border-slate-700 flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300 flex items-center justify-center flex-shrink-0">
            <ShieldAlert size={18}/>
          </div>
          <div className="flex-1 min-w-0">
            <h2 className="text-lg font-extrabold text-slate-900 dark:text-slate-100 truncate">{title}</h2>
            <p className="text-[11px] text-slate-500 dark:text-slate-400 mt-0.5">Please read carefully before accepting.</p>
          </div>
          <button onClick={onDecline} className="p-1.5 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-400">
            <X size={18}/>
          </button>
        </div>

        <div
          ref={scrollRef}
          onScroll={handleScroll}
          className="flex-1 overflow-y-auto px-6 py-5 text-sm leading-relaxed text-slate-700 dark:text-slate-200 font-[ui-serif,Georgia,serif]"
        >
          {bodyHtml
            ? <div
                className="ack-doc space-y-3 [&_strong]:font-bold [&_strong]:text-slate-900 dark:[&_strong]:text-slate-100 [&_ol]:list-decimal [&_ol]:pl-5 [&_ol]:space-y-2 [&_em]:italic"
                dangerouslySetInnerHTML={{ __html: bodyHtml }}
              />
            : <div className="whitespace-pre-wrap">{body}</div>
          }
          <div className="h-2"/>
        </div>

        {error && (
          <div className="mx-6 mb-3 rounded-lg p-3 bg-red-50 border border-red-200 text-red-700 text-xs">{error}</div>
        )}

        <div className="px-6 py-4 border-t border-slate-200 dark:border-slate-700 flex items-center justify-between gap-3 flex-wrap">
          {requireScroll && !scrolledToEnd && (
            <span className="text-[11px] text-slate-500 dark:text-slate-400">Scroll to the bottom to enable the agree button.</span>
          )}
          <div className="ml-auto flex gap-3">
            <button onClick={onDecline}
              className="px-5 py-2.5 rounded-xl border border-slate-300 dark:border-slate-700 text-slate-600 dark:text-slate-300 text-sm font-semibold">
              {declineLabel}
            </button>
            <button onClick={accept}
              disabled={!scrolledToEnd || submitting}
              className="px-5 py-2.5 rounded-xl bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-bold">
              {submitting ? 'Recording…' : acceptLabel}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
