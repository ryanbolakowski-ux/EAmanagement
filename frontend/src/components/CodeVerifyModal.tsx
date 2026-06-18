/**
 * CodeVerifyModal — email verification-code step (Phase G).
 *
 * Requests a one-time code for a given `purpose` (e.g. 'enable_automation'),
 * shows a 6-digit input, and confirms it. On success it calls onVerified(),
 * which authorizes the gated action server-side for ~10 minutes. Handles the
 * backend's 400 / 403 / 429 responses with inline messages and a cooldown-aware
 * "Resend code" button.
 */
import { useEffect, useRef, useState } from 'react'
import { Mail, X, ShieldCheck } from 'lucide-react'
import { securityApi, type VerifyPurpose } from '../api/endpoints'

interface Props {
  purpose: VerifyPurpose
  title?: string
  subtitle?: string
  onVerified: () => void
  onCancel: () => void
}

const RESEND_COOLDOWN_S = 30

export default function CodeVerifyModal({
  purpose, title = 'Verify it’s you', subtitle, onVerified, onCancel,
}: Props) {
  const [code, setCode] = useState('')
  const [requesting, setRequesting] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(null)
  const [cooldown, setCooldown] = useState(0)
  const requestedOnce = useRef(false)

  // Send the first code automatically when the modal mounts.
  useEffect(() => {
    if (requestedOnce.current) return
    requestedOnce.current = true
    void sendCode()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Tick the resend cooldown down to zero.
  useEffect(() => {
    if (cooldown <= 0) return
    const id = window.setInterval(() => setCooldown(c => (c > 0 ? c - 1 : 0)), 1000)
    return () => window.clearInterval(id)
  }, [cooldown])

  async function sendCode() {
    if (requesting || cooldown > 0) return
    setRequesting(true); setError(null); setInfo(null)
    try {
      const r = await securityApi.requestCode(purpose).then(res => res.data)
      setInfo(`We emailed a code. It expires in ${r.expires_in_min ?? 10} minutes.`)
      setCooldown(RESEND_COOLDOWN_S)
    } catch (e: any) {
      const status = e?.response?.status
      const detail = e?.response?.data?.detail
      if (status === 429) {
        setError(typeof detail === 'string' ? detail : 'Please wait before requesting another code.')
        setCooldown(RESEND_COOLDOWN_S)
      } else {
        setError(typeof detail === 'string' ? detail : 'Could not send a code. Try again.')
      }
    } finally {
      setRequesting(false)
    }
  }

  async function confirm() {
    if (confirming || code.length < 6) return
    setConfirming(true); setError(null)
    try {
      await securityApi.confirmCode(purpose, code).then(res => res.data)
      onVerified()
    } catch (e: any) {
      const detail = e?.response?.data?.detail
      setError(typeof detail === 'string' ? detail : 'That code didn’t work. Try again.')
    } finally {
      setConfirming(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[110] bg-black/70 flex items-center justify-center p-4">
      <div className="w-full max-w-lg bg-white dark:bg-slate-900 rounded-2xl shadow-2xl max-h-[90vh] overflow-y-auto">
        {/* Header */}
        <div className="sticky top-0 bg-white dark:bg-slate-900 px-6 py-4 border-b border-slate-200 dark:border-slate-700 flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 flex items-center justify-center flex-shrink-0">
            <Mail size={18}/>
          </div>
          <div className="flex-1 min-w-0">
            <h2 className="text-lg font-extrabold text-slate-900 dark:text-slate-100 truncate">{title}</h2>
            <p className="text-[11px] text-slate-500 dark:text-slate-400 mt-0.5">
              {subtitle || 'Enter the 6-digit code we emailed you.'}
            </p>
          </div>
          <button onClick={onCancel} className="p-1.5 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-400 hover:text-slate-700">
            <X size={18}/>
          </button>
        </div>

        {/* Body */}
        <div className="px-6 py-5 space-y-4">
          <input
            autoFocus
            inputMode="numeric"
            autoComplete="one-time-code"
            maxLength={6}
            value={code}
            onChange={e => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
            onKeyDown={e => { if (e.key === 'Enter') void confirm() }}
            placeholder="••••••"
            className="w-full text-center tracking-[0.6em] text-2xl font-bold tabular-nums px-4 py-3 rounded-xl border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-900 dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-blue-500"
          />

          {info && !error && (
            <div className="rounded-xl border border-blue-200 dark:border-blue-900/40 bg-blue-50 dark:bg-blue-900/20 p-3 flex items-start gap-3 text-xs text-blue-800 dark:text-blue-200">
              <ShieldCheck size={16} className="flex-shrink-0 mt-0.5"/>
              <span>{info}</span>
            </div>
          )}

          {error && (
            <div className="rounded-xl border border-red-200 dark:border-red-900/40 bg-red-50 dark:bg-red-900/20 p-3 text-xs text-red-800 dark:text-red-300">
              {error}
            </div>
          )}

          <button
            type="button"
            onClick={() => void sendCode()}
            disabled={requesting || cooldown > 0}
            className="text-xs font-semibold text-blue-600 dark:text-blue-400 hover:underline disabled:opacity-50 disabled:no-underline"
          >
            {requesting ? 'Sending…' : cooldown > 0 ? `Resend code in ${cooldown}s` : 'Resend code'}
          </button>
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-slate-200 dark:border-slate-700 flex gap-3 justify-end">
          <button
            onClick={onCancel}
            className="border border-slate-300 dark:border-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 rounded-xl px-5 py-2.5 text-sm font-semibold"
          >
            Cancel
          </button>
          <button
            onClick={() => void confirm()}
            disabled={confirming || code.length < 6}
            className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white rounded-xl px-5 py-2.5 text-sm font-semibold"
          >
            {confirming ? 'Verifying…' : 'Verify & continue'}
          </button>
        </div>
      </div>
    </div>
  )
}
