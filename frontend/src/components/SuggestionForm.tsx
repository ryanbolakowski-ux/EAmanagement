import { useState } from 'react'
import { Lightbulb, Send, X, Bug, Palette, MessageSquare } from 'lucide-react'
import api from '../api/client'

type Category = 'feature' | 'bug' | 'ux' | 'other'

/**
 * SuggestionForm — a floating "Leave a suggestion" widget mounted alongside
 * the ChatBubble. Sends to the platform owner via /api/v1/support/suggestion (the inbox is configured via ADMIN_NOTIFY_EMAIL).
 */
export default function SuggestionForm() {
  const [open, setOpen] = useState(false)
  const [category, setCategory] = useState<Category>('feature')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function send() {
    const msg = message.trim()
    if (msg.length < 5) { setError('Just a bit more — at least a few words.'); return }
    setBusy(true); setError(null)
    try {
      // Route through the shared axios client (correct baseURL + auth
      // interceptor + working CORS). The previous raw fetch resolved
      // VITE_API_URL differently and its requests never reached the backend.
      await api.post('/api/v1/support/suggestion', { message: msg, category })
      // success path — axios throws on non-2xx, so reaching here means sent
      setDone(true)
      setMessage('')
      setTimeout(() => { setDone(false); setOpen(false) }, 2200)
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Failed to send')
    } finally { setBusy(false) }
  }

  const CATS: { id: Category; label: string; icon: any }[] = [
    { id: 'feature', label: 'Feature', icon: Lightbulb },
    { id: 'bug',     label: 'Bug',     icon: Bug },
    { id: 'ux',      label: 'UX',      icon: Palette },
    { id: 'other',   label: 'Other',   icon: MessageSquare },
  ]

  return (
    <>
      {!open && (
        <button
          onClick={() => setOpen(true)}
          aria-label="Leave a suggestion"
          title="Leave a suggestion"
          className="fixed bottom-5 right-[5.5rem] z-[149] bg-white dark:bg-slate-800 hover:bg-violet-50 dark:hover:bg-violet-900/30 border border-violet-300 dark:border-violet-700 text-violet-700 dark:text-violet-300 rounded-full w-11 h-11 flex items-center justify-center shadow-lg shadow-violet-900/20 transition-all hover:scale-105"
        >
          <Lightbulb size={18} />
        </button>
      )}

      {open && (
        <div className="fixed bottom-5 right-5 z-[149] w-[360px] max-w-[95vw] bg-white dark:bg-slate-900 rounded-2xl shadow-2xl shadow-purple-900/30 border border-slate-200 dark:border-slate-700 overflow-hidden">
          <div className="flex items-center justify-between px-4 py-3 bg-gradient-to-r from-amber-500 to-yellow-500 text-white">
            <div className="flex items-center gap-2">
              <Lightbulb size={18} />
              <div>
                <div className="font-bold text-sm">Leave a suggestion</div>
                <div className="text-[10px] opacity-90">Goes straight to the Theta Algos team</div>
              </div>
            </div>
            <button onClick={() => setOpen(false)} className="opacity-80 hover:opacity-100" aria-label="Close">
              <X size={18} />
            </button>
          </div>

          {done ? (
            <div className="p-6 text-center">
              <div className="text-4xl mb-2">✅</div>
              <div className="font-bold text-slate-900 dark:text-slate-100 mb-1">Sent — thank you</div>
              <div className="text-xs text-slate-500 dark:text-slate-400">We read every one.</div>
            </div>
          ) : (
            <div className="p-4 space-y-3">
              <div>
                <label className="text-[10px] uppercase tracking-wider font-bold text-slate-500 dark:text-slate-400 block mb-1.5">Category</label>
                <div className="grid grid-cols-4 gap-1.5">
                  {CATS.map(c => (
                    <button
                      key={c.id}
                      onClick={() => setCategory(c.id)}
                      className={`flex flex-col items-center gap-1 px-2 py-2 rounded-lg border text-[11px] font-semibold transition-colors ${
                        category === c.id
                          ? 'border-violet-500 bg-violet-50 dark:bg-violet-900/30 text-violet-700 dark:text-violet-300'
                          : 'border-slate-300 dark:border-slate-700 text-slate-600 dark:text-slate-400 hover:border-violet-300'
                      }`}
                    >
                      <c.icon size={14} />
                      {c.label}
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <label className="text-[10px] uppercase tracking-wider font-bold text-slate-500 dark:text-slate-400 block mb-1.5">What's on your mind?</label>
                <textarea
                  value={message}
                  onChange={(e) => setMessage(e.target.value)}
                  placeholder="e.g. would love a strategy that fires only between 9:45 and 10:30 ET..."
                  rows={5}
                  className="w-full resize-none rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-2 text-sm text-slate-900 dark:text-slate-100 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-violet-500 focus:border-transparent"
                />
                <div className="text-[10px] text-slate-400 text-right mt-1">{message.length}/5000</div>
              </div>

              {error && <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 text-red-600 text-xs px-3 py-2 rounded-lg">{error}</div>}

              <button
                onClick={send}
                disabled={busy || message.trim().length < 5}
                className="w-full bg-gradient-to-r from-amber-500 to-yellow-500 hover:from-amber-400 hover:to-yellow-400 disabled:from-slate-300 disabled:to-slate-300 dark:disabled:from-slate-700 dark:disabled:to-slate-700 text-white font-bold py-2.5 rounded-lg flex items-center justify-center gap-2 transition-colors"
              >
                <Send size={14} />
                {busy ? 'Sending...' : 'Send to Theta team'}
              </button>
            </div>
          )}
        </div>
      )}
    </>
  )
}
