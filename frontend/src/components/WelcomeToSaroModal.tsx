/**
 * WelcomeToSaroModal — one-time onboarding modal shown when a user becomes
 * Saro-eligible (tier_3+ / Options Scanner or higher) but has never activated
 * the daily Saro STOCK pick. Offers a single Activate button (arms the daily
 * pick email) or "Maybe later" (dismissed locally so it never nags again).
 *
 * Trigger source of truth = GET /api/v1/account-signals/my-access
 * (saro_stock_eligible === true && saro_signals_activated === false). Dismissal
 * is persisted in localStorage per user so a reload / re-entry doesn't re-open
 * it; once the user activates (the server flag flips), the modal never qualifies
 * again anyway, so the local flag is only for the "maybe later" path.
 *
 * APP PUSH: the copy flags push as "coming with the app". No push is registered
 * here — activation only arms the daily EMAIL (accountSignalsApi.saroActivate).
 */
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Sparkles, X, Mail, Bell, CheckCircle2 } from 'lucide-react'
import { accountSignalsApi } from '../api/endpoints'
import { useMyAccess, MY_ACCESS_KEY } from '../hooks/useMyAccess'
import { useAuthStore } from '../stores/authStore'

// Shared react-query key for the Saro status read. The Saro Stock Picker card
// (LiveTradingV2) and this modal both invalidate it on toggle so activation
// state stays in lockstep across the page.
export const SARO_STATUS_KEY = ['saro-status'] as const

// localStorage dismissal is keyed per user so two accounts on one browser get
// their own "maybe later" state.
const DISMISS_PREFIX = 'saro_welcome_dismissed_v1'
const dismissKey = (uid: string) => `${DISMISS_PREFIX}:${uid}`

function isDismissed(uid: string): boolean {
  try { return localStorage.getItem(dismissKey(uid)) === '1' } catch { return false }
}
function persistDismiss(uid: string) {
  try { localStorage.setItem(dismissKey(uid), '1') } catch { /* quota — ignore */ }
}

export default function WelcomeToSaroModal() {
  const qc = useQueryClient()
  const { user } = useAuthStore()
  const { data: access } = useMyAccess()
  const uid = String((user as any)?.id || (user as any)?.email || 'anon')

  // Instant-close flag so Activate / Maybe-later dismisses the modal without
  // waiting for the my-access refetch to round-trip.
  const [closed, setClosed] = useState(false)

  const activate = useMutation({
    mutationFn: () => accountSignalsApi.saroActivate().then(r => r.data),
    onSuccess: () => {
      persistDismiss(uid) // an activated user never needs the welcome again
      qc.invalidateQueries({ queryKey: MY_ACCESS_KEY })
      qc.invalidateQueries({ queryKey: SARO_STATUS_KEY })
      setClosed(true)
    },
  })

  const eligible = access?.saro_stock_eligible === true
  const activated = access?.saro_signals_activated === true
  const shouldShow = eligible && !activated && !closed && !isDismissed(uid)
  if (!shouldShow) return null

  const dismiss = () => { persistDismiss(uid); setClosed(true) }

  return (
    <div className="fixed inset-0 z-[110] bg-black/70 flex items-center justify-center p-4" onClick={dismiss}>
      <div onClick={e => e.stopPropagation()}
        className="w-full max-w-md bg-white dark:bg-slate-900 rounded-2xl shadow-2xl overflow-hidden">
        {/* Violet hero header */}
        <div className="relative bg-gradient-to-br from-violet-600 to-indigo-700 text-white px-6 pt-6 pb-7">
          <button onClick={dismiss} aria-label="Dismiss"
            className="absolute top-3 right-3 p-1.5 rounded-lg text-white/70 hover:text-white hover:bg-white/10">
            <X size={18}/>
          </button>
          <div className="w-11 h-11 rounded-xl bg-white/15 flex items-center justify-center mb-3">
            <Sparkles size={22}/>
          </div>
          <div className="text-[10px] uppercase tracking-[0.2em] font-bold opacity-80">Welcome to Saro</div>
          <h2 className="text-2xl font-extrabold mt-1 leading-tight">Your daily stock pick is ready</h2>
          <p className="text-sm opacity-90 mt-1.5 leading-snug">
            Saro scans the market every morning and sends you one high-conviction stock pick — with entry, stop, and target already worked out.
          </p>
        </div>

        {/* Body */}
        <div className="px-6 py-5 space-y-3">
          <div className="flex items-start gap-3">
            <div className="w-8 h-8 rounded-lg bg-violet-100 dark:bg-violet-900/40 text-violet-700 dark:text-violet-300 flex items-center justify-center flex-shrink-0">
              <Mail size={15}/>
            </div>
            <div className="min-w-0">
              <div className="text-sm font-bold text-slate-900 dark:text-slate-100">Delivered by email</div>
              <div className="text-xs text-slate-500 dark:text-slate-400">One pick per trading day, around 9:36 AM ET.</div>
            </div>
          </div>
          <div className="flex items-start gap-3">
            <div className="w-8 h-8 rounded-lg bg-slate-100 dark:bg-slate-800 text-slate-400 dark:text-slate-500 flex items-center justify-center flex-shrink-0">
              <Bell size={15}/>
            </div>
            <div className="min-w-0">
              <div className="text-sm font-bold text-slate-500 dark:text-slate-400">
                Push notifications
                <span className="text-[10px] uppercase tracking-wider font-extrabold text-violet-600 dark:text-violet-300 ml-1.5">coming with the app</span>
              </div>
              <div className="text-xs text-slate-500 dark:text-slate-400">Get the pick on your phone the moment it fires — arriving with the iOS app.</div>
            </div>
          </div>

          {activate.isError && (
            <div className="rounded-lg p-2.5 bg-rose-50 dark:bg-rose-900/20 border border-rose-200 dark:border-rose-900/40 text-[11px] text-rose-700 dark:text-rose-300">
              {(activate.error as any)?.response?.data?.detail || 'Could not activate right now — please try again.'}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 pb-6 pt-1 flex flex-col-reverse sm:flex-row gap-2">
          <button onClick={dismiss}
            className="sm:flex-1 px-4 py-2.5 rounded-xl text-sm font-bold text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800">
            Maybe later
          </button>
          <button onClick={() => activate.mutate()} disabled={activate.isPending}
            className="sm:flex-[2] inline-flex items-center justify-center gap-1.5 px-4 py-2.5 rounded-xl text-sm font-extrabold bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white shadow-lg shadow-violet-900/20">
            <CheckCircle2 size={15}/> {activate.isPending ? 'Activating…' : 'Activate Saro picks'}
          </button>
        </div>
      </div>
    </div>
  )
}
