import { useState, useRef, useEffect } from 'react'
import { X, Send, Sparkles } from 'lucide-react'
import api from '../api/client'
import ThetaLogo from './ThetaLogo'

/**
 * ChatBubble — AI assistant powered by Claude.
 *
 * Streams responses from /api/v1/support/chat. Maintains full conversation
 * context (last 20 turns) so follow-ups work naturally. Falls back to a
 * "contact support" prompt if the chat endpoint is unavailable.
 */
type Msg = { role: 'user' | 'assistant'; content: string }

const SUGGESTIONS = [
  'What is the morning email?',
  'How do I connect Tradier?',
  'Explain FVG Inversion Tap',
  'What does the Theta Scanner do?',
  "What's the difference between paper and live?",
]

const GREETING: Msg = {
  role: 'assistant',
  content: "I'm the Theta Algos assistant. Ask me anything about the platform, your strategies, brokers, scanner setups, or trading concepts. I can also walk you through any feature step by step.",
}

export default function ChatBubble() {
  const [open, setOpen] = useState(false)
  const [msgs, setMsgs] = useState<Msg[]>([GREETING])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [showSuggestions, setShowSuggestions] = useState(true)
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  // Auto-scroll on new message
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [msgs, busy])

  // Auto-focus when opened
  useEffect(() => { if (open) setTimeout(() => inputRef.current?.focus(), 100) }, [open])

  async function send(text?: string) {
    const content = (text ?? input).trim()
    if (!content || busy) return
    setInput('')
    setShowSuggestions(false)
    const next: Msg[] = [...msgs, { role: 'user', content }]
    setMsgs(next)
    setBusy(true)
    // Optimistically append empty assistant message that we'll stream into
    setMsgs(m => [...m, { role: 'assistant', content: '' }])

    try {
      const token = localStorage.getItem('access_token')
      const apiBase = (import.meta as any).env?.VITE_API_URL || ''
      const r = await fetch(`${apiBase}/api/v1/support/chat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ messages: next }),
      })

      if (!r.ok) {
        const errBody = await r.json().catch(() => ({}))
        setMsgs(m => {
          const copy = [...m]
          copy[copy.length - 1] = {
            role: 'assistant',
            content: errBody.detail || `I'm having trouble right now (HTTP ${r.status}). Email theta.algos@yahoo.com and a human will help.`,
          }
          return copy
        })
        setBusy(false)
        return
      }

      // SSE-style stream parser
      const reader = r.body!.getReader()
      const dec = new TextDecoder()
      let buffer = ''
      let assistantText = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += dec.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const payload = JSON.parse(line.slice(6))
            if (payload.delta) {
              assistantText += payload.delta
              setMsgs(m => {
                const copy = [...m]
                copy[copy.length - 1] = { role: 'assistant', content: assistantText }
                return copy
              })
            } else if (payload.error) {
              assistantText = `Sorry — ${payload.error}. Try again, or email theta.algos@yahoo.com.`
              setMsgs(m => {
                const copy = [...m]
                copy[copy.length - 1] = { role: 'assistant', content: assistantText }
                return copy
              })
            }
          } catch { /* ignore malformed SSE line */ }
        }
      }
    } catch (e: any) {
      setMsgs(m => {
        const copy = [...m]
        copy[copy.length - 1] = {
          role: 'assistant',
          content: `Connection failed (${e?.message || 'unknown'}). Email theta.algos@yahoo.com.`,
        }
        return copy
      })
    } finally {
      setBusy(false)
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
  }

  return (
    <>
      {/* Bubble button — Theta logo stays purple. Background flips white in
          light mode / near-black in dark mode. Always glows purple. Logo
          stays visible regardless of theme. */}
      {!open && (
        <button
          onClick={() => setOpen(true)}
          aria-label="Open Theta Assistant"
          className="fixed bottom-5 right-5 z-[150] bg-white dark:bg-slate-950 hover:bg-violet-50 dark:hover:bg-slate-900 rounded-full w-14 h-14 flex items-center justify-center transition-transform hover:scale-105 ring-1 ring-violet-300 dark:ring-violet-700"
          style={{
            // Always-on purple underglow, stronger in dark mode
            boxShadow: '0 0 20px rgba(124, 58, 237, 0.45), 0 0 40px rgba(124, 58, 237, 0.20), 0 4px 12px rgba(0, 0, 0, 0.15)',
          }}
        >
          <ThetaLogo size={32} />
        </button>
      )}

      {/* Chat panel */}
      {open && (
        <div className="fixed bottom-5 right-5 z-[150] w-[380px] max-w-[95vw] h-[560px] max-h-[80vh] flex flex-col bg-white dark:bg-slate-900 rounded-2xl shadow-2xl shadow-violet-900/30 border border-slate-200 dark:border-slate-700 overflow-hidden">
          {/* Header — deep purple gradient with Theta logo */}
          <div className="flex items-center justify-between px-4 py-3 bg-gradient-to-r from-purple-700 via-violet-700 to-indigo-800 text-white">
            <div className="flex items-center gap-2.5">
              <ThetaLogo size={22} />
              <div>
                <div className="font-bold text-sm">Theta Assistant</div>
                <div className="text-[10px] opacity-80">Trained on every Theta Algos feature</div>
              </div>
            </div>
            <button onClick={() => setOpen(false)} className="opacity-80 hover:opacity-100" aria-label="Close chat">
              <X size={18} />
            </button>
          </div>

          {/* Messages */}
          <div ref={scrollRef} className="flex-1 overflow-y-auto p-4 space-y-3 bg-slate-50 dark:bg-slate-950">
            {msgs.map((m, i) => (
              <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                <div
                  className={`max-w-[85%] rounded-2xl px-3.5 py-2 text-[13.5px] leading-relaxed ${
                    m.role === 'user'
                      ? 'bg-violet-600 text-white rounded-br-md'
                      : 'bg-white dark:bg-slate-800 text-slate-900 dark:text-slate-100 border border-slate-200 dark:border-slate-700 rounded-bl-md'
                  }`}
                >
                  {m.content || (m.role === 'assistant' && busy && i === msgs.length - 1 ? (
                    <span className="inline-flex gap-1 items-center text-slate-400">
                      <span className="w-1.5 h-1.5 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }}/>
                      <span className="w-1.5 h-1.5 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }}/>
                      <span className="w-1.5 h-1.5 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }}/>
                    </span>
                  ) : m.content)}
                </div>
              </div>
            ))}

            {/* Starter suggestions */}
            {showSuggestions && msgs.length === 1 && (
              <div className="space-y-1.5 pt-2">
                <div className="text-[10px] uppercase tracking-wider font-bold text-slate-500 dark:text-slate-400 mb-1">Try asking</div>
                {SUGGESTIONS.map(s => (
                  <button
                    key={s}
                    onClick={() => send(s)}
                    className="block w-full text-left text-[12px] px-3 py-2 rounded-xl bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 hover:border-violet-400 hover:bg-violet-50 dark:hover:bg-violet-900/20 text-slate-700 dark:text-slate-200 transition-colors"
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Input */}
          <div className="border-t border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-3">
            <div className="flex items-end gap-2">
              <textarea
                ref={inputRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder="Ask anything..."
                disabled={busy}
                rows={1}
                className="flex-1 resize-none rounded-xl border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-2 text-sm text-slate-900 dark:text-slate-100 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-violet-500 focus:border-transparent max-h-32"
                style={{ minHeight: '36px' }}
              />
              <button
                onClick={() => send()}
                disabled={busy || !input.trim()}
                className="bg-violet-600 hover:bg-violet-500 disabled:bg-slate-300 disabled:dark:bg-slate-700 text-white p-2 rounded-xl transition-colors flex-shrink-0"
                aria-label="Send"
              >
                <Send size={16} />
              </button>
            </div>
            <div className="text-[9px] text-slate-400 mt-1.5 text-center">
              AI can make mistakes. For account-specific issues, email theta.algos@yahoo.com.
            </div>
          </div>
        </div>
      )}
    </>
  )
}
