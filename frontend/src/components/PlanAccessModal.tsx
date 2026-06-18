/**
 * PlanAccessModal — explains the signed-in user's plan and what it unlocks
 * (Phase G). Shows a capability matrix (trade-idea signals, approve-to-place,
 * fully automated), the current tier, an Upgrade CTA for missing capabilities,
 * and — for tier_5 — the embedded AutomationActivation control.
 */
import { Link } from 'react-router-dom'
import { Crown, X, CheckCircle2, Lock, ArrowUpRight, AlertCircle } from 'lucide-react'
import { useMyAccess } from '../hooks/useMyAccess'
import { useAuthStore } from '../stores/authStore'
import AutomationActivation from './AutomationActivation'

interface Props {
  onClose: () => void
}

const TIER_LABELS: Record<string, string> = {
  free_trial: 'Free trial',
  tier_1: 'Starter',
  tier_2: 'Signals',
  tier_3: 'Signals Pro',
  tier_4: 'Approve-to-place',
  tier_5: 'Fully automated',
}

function tierLabel(tier: string): string {
  return TIER_LABELS[tier] || tier.replace('tier_', 'Tier ').replace('_', ' ')
}

export default function PlanAccessModal({ onClose }: Props) {
  const { user } = useAuthStore()
  const { data: access, isLoading, isError, refetch } = useMyAccess()

  const capabilities = access
    ? [
        { label: 'Trade-idea signals', desc: 'Receive entry/stop/target alerts', has: access.gets_signals },
        { label: 'Approve-to-place', desc: 'We place the trade when you approve a signal', has: access.can_place_on_approval },
        { label: 'Fully automated', desc: 'Qualifying signals placed automatically', has: access.fully_automated },
      ]
    : []

  const hasMissing = capabilities.some(c => !c.has)
  const isTier5 = access?.tier === 'tier_5'

  return (
    <div className="fixed inset-0 z-[100] bg-black/70 flex items-center justify-center p-4" onClick={onClose}>
      <div
        className="w-full max-w-lg bg-white dark:bg-slate-900 rounded-2xl shadow-2xl max-h-[90vh] overflow-y-auto"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="sticky top-0 bg-white dark:bg-slate-900 px-6 py-4 border-b border-slate-200 dark:border-slate-700 flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-violet-100 dark:bg-violet-900/40 text-violet-700 dark:text-violet-300 flex items-center justify-center flex-shrink-0">
            <Crown size={18}/>
          </div>
          <div className="flex-1 min-w-0">
            <h2 className="text-lg font-extrabold text-slate-900 dark:text-slate-100 truncate">Your plan &amp; access</h2>
            <p className="text-[11px] text-slate-500 dark:text-slate-400 mt-0.5">
              {access ? tierLabel(access.tier) : 'Loading your plan…'}
            </p>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-400 hover:text-slate-700">
            <X size={18}/>
          </button>
        </div>

        {/* Body */}
        <div className="px-6 py-5 space-y-4">
          {!user || isLoading ? (
            <div className="space-y-2">
              <div className="h-12 rounded-xl bg-slate-100 dark:bg-slate-800 animate-pulse"/>
              <div className="h-12 rounded-xl bg-slate-100 dark:bg-slate-800 animate-pulse"/>
              <div className="h-12 rounded-xl bg-slate-100 dark:bg-slate-800 animate-pulse"/>
            </div>
          ) : isError || !access ? (
            <div className="rounded-xl border border-red-200 dark:border-red-900/40 bg-red-50 dark:bg-red-900/20 p-4 flex items-start gap-3">
              <AlertCircle size={18} className="text-red-600 dark:text-red-300 flex-shrink-0 mt-0.5"/>
              <div className="min-w-0">
                <div className="text-sm font-semibold text-red-900 dark:text-red-100">Could not load your plan</div>
                <button onClick={() => refetch()} className="text-xs font-semibold text-red-700 dark:text-red-300 hover:underline mt-1">
                  Try again
                </button>
              </div>
            </div>
          ) : (
            <>
              {/* Capability matrix */}
              <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 divide-y divide-slate-100 dark:divide-slate-800 overflow-hidden">
                {capabilities.map(cap => (
                  <div key={cap.label} className="flex items-center gap-3 px-4 py-3">
                    <div className={`w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0 ${
                      cap.has
                        ? 'bg-emerald-100 dark:bg-emerald-900/40 text-emerald-600 dark:text-emerald-300'
                        : 'bg-slate-100 dark:bg-slate-800 text-slate-400 dark:text-slate-500'
                    }`}>
                      {cap.has ? <CheckCircle2 size={16}/> : <Lock size={14}/>}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className={`text-sm font-semibold ${cap.has ? 'text-slate-900 dark:text-slate-100' : 'text-slate-500 dark:text-slate-400'}`}>
                        {cap.label}
                      </div>
                      <div className="text-[11px] text-slate-500 dark:text-slate-400">{cap.desc}</div>
                    </div>
                    <span className={`badge ${cap.has ? 'badge-green' : 'badge-grey'}`}>
                      {cap.has ? 'Included' : 'Locked'}
                    </span>
                  </div>
                ))}
              </div>

              {/* tier_5: automation activation */}
              {isTier5 && (
                <div className="pt-1">
                  <div className="text-[11px] uppercase tracking-wider text-slate-500 dark:text-slate-400 font-bold mb-2">
                    Automated trading
                  </div>
                  <AutomationActivation onClose={onClose}/>
                </div>
              )}

              {/* Upgrade CTA for missing capabilities */}
              {hasMissing && (
                <div className="rounded-xl border border-violet-200 dark:border-violet-900/40 bg-violet-50 dark:bg-violet-900/20 p-4 flex items-start gap-3">
                  <Crown size={18} className="text-violet-600 dark:text-violet-300 flex-shrink-0 mt-0.5"/>
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-semibold text-violet-900 dark:text-violet-100">Unlock more</div>
                    <p className="text-xs text-violet-700 dark:text-violet-200 mt-1">
                      Upgrade your plan to add the locked capabilities above.
                    </p>
                  </div>
                  <Link
                    to="/app/profile"
                    onClick={onClose}
                    className="inline-flex items-center gap-1.5 bg-blue-600 hover:bg-blue-700 text-white rounded-xl px-4 py-2 text-xs font-semibold flex-shrink-0 self-center"
                  >
                    Upgrade <ArrowUpRight size={14}/>
                  </Link>
                </div>
              )}
            </>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-slate-200 dark:border-slate-700 flex gap-3 justify-end">
          <button
            onClick={onClose}
            className="border border-slate-300 dark:border-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 rounded-xl px-5 py-2.5 text-sm font-semibold"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  )
}
