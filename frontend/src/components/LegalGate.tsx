/**
 * LegalGate — drop-in wrapper that runs the user through a series of
 * required acknowledgments before invoking `onComplete()`.
 *
 *   <LegalGate
 *     kinds={['risk_disclosure', 'live_trading_consent']}
 *     onComplete={() => actuallyEnableLive()}
 *     onCancel={() => setShowGate(false)}
 *   />
 *
 * It fetches the user's existing ack status, skips any already-accepted
 * (current-version) kinds, and walks the user through the remaining ones
 * one modal at a time. After the final modal accepts, `onComplete` fires.
 *
 * Server-side enforcement on the gated action endpoint is still required —
 * this component improves UX by showing the right wall of text at the
 * right moment, but a missing ack must also be rejected by the API.
 */
import { useEffect, useState } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import AcknowledgmentModal from './AcknowledgmentModal'
import { legalApi, type LegalKind, type LegalDocument } from '../api/endpoints'

interface Props {
  kinds: LegalKind[]
  onComplete: () => void
  onCancel: () => void
  // Optional: caller's own pre-step gate (e.g. "make sure daily-loss-cap is set")
  preflightReady?: boolean
  preflightMessage?: string
}

export default function LegalGate({ kinds, onComplete, onCancel, preflightReady = true, preflightMessage }: Props) {
  const { data: status, isLoading: loadingStatus } = useQuery({
    queryKey: ['legal-status'],
    queryFn: () => legalApi.status().then(r => r.data),
    staleTime: 0,
  })

  // Figure out the queue of unmet acks once status loads
  const [queue, setQueue] = useState<LegalKind[] | null>(null)
  useEffect(() => {
    if (!status) return
    const todo = kinds.filter(k => !status.acknowledgments[k]?.accepted)
    setQueue(todo)
  }, [status, JSON.stringify(kinds)])

  // Fast-path: nothing to ack
  useEffect(() => {
    if (queue && queue.length === 0 && preflightReady) {
      onComplete()
    }
  }, [queue, preflightReady])

  const currentKind = queue?.[0]
  const { data: doc, isLoading: loadingDoc } = useQuery({
    queryKey: ['legal-doc', currentKind],
    queryFn: () => legalApi.document(currentKind!).then(r => r.data),
    enabled: !!currentKind,
  })

  if (!preflightReady) {
    return (
      <div className="fixed inset-0 z-[100] bg-black/70 flex items-center justify-center p-4">
        <div className="w-full max-w-md bg-white dark:bg-slate-900 rounded-2xl p-6">
          <h3 className="text-base font-extrabold text-slate-900 dark:text-slate-100 mb-2">One moment.</h3>
          <p className="text-sm text-slate-600 dark:text-slate-300 mb-5">{preflightMessage || 'Please complete the setup before continuing.'}</p>
          <div className="flex justify-end">
            <button onClick={onCancel} className="px-4 py-2 rounded-lg border border-slate-300 dark:border-slate-700 text-sm font-semibold">Close</button>
          </div>
        </div>
      </div>
    )
  }

  if (loadingStatus || queue === null) {
    return <BlockingSpinner label="Checking acknowledgments…"/>
  }

  // All done
  if (queue.length === 0) return null

  if (loadingDoc || !doc) return <BlockingSpinner label="Loading document…"/>

  return (
    <AcknowledgmentModal
      title={doc.title}
      bodyHtml={doc.html}
      kind={doc.kind}
      requireScroll={true}
      acceptLabel="I have read and agree"
      onAccept={() => {
        // Pop the head, continue
        setQueue(q => (q ? q.slice(1) : []))
      }}
      onDecline={onCancel}
    />
  )
}

function BlockingSpinner({ label }: { label: string }) {
  return (
    <div className="fixed inset-0 z-[100] bg-black/60 flex items-center justify-center">
      <div className="bg-white dark:bg-slate-900 rounded-xl px-5 py-3 shadow-xl text-sm text-slate-700 dark:text-slate-200">
        {label}
      </div>
    </div>
  )
}
