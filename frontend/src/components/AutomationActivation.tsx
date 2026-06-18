/**
 * AutomationActivation — tier_5 fully-automated-trading control (Phase G),
 * embedded in PlanAccessModal.
 *
 * Enabling automation is a gated action: PATCH .../trading-enabled {true} can
 * return 403 with either
 *   - "acknowledgment_required:{kind}:{version}"  → walk the legal acks, then retry
 *   - "verification_required:enable_automation"   → email-code step, then retry
 * A single click may need acks THEN a code THEN the enable, so we loop: each
 * gate, once satisfied, re-fires the enable mutation and the next 403 (if any)
 * drives the next gate. Mirrors OptionsActivateButton's 403-driven approach.
 */
import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Bot, ShieldCheck, AlertCircle, CheckCircle2, Building2, Power, Loader2 } from 'lucide-react'
import { liveTradingApi } from '../api/endpoints'
import { useMyAccess, useInvalidateMyAccess } from '../hooks/useMyAccess'
import LegalGate from './LegalGate'
import CodeVerifyModal from './CodeVerifyModal'
import type { LegalKind } from '../api/endpoints'

const AUTOMATION_KINDS: LegalKind[] = ['risk_disclosure', 'live_trading_consent', 'fully_automated_trading']

interface Props {
  onClose?: () => void
}

export default function AutomationActivation({ onClose }: Props) {
  const { data: access } = useMyAccess()
  const invalidateAccess = useInvalidateMyAccess()

  const [legalPending, setLegalPending] = useState(false)
  const [codePending, setCodePending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [enabledOk, setEnabledOk] = useState(false)
  const [confirmOff, setConfirmOff] = useState(false)

  // Resolve the broker account to act on (first one). Only fetched when the
  // user actually has a broker account, to avoid an empty round-trip.
  const { data: accounts, isLoading: accountsLoading, isError: accountsError } = useQuery({  // PHASE-G-POLISH
    queryKey: ['broker-accounts'],
    queryFn: () => liveTradingApi.listAccounts().then(r => r.data),
    enabled: !!access?.has_broker_account,
  })
  const accountId = accounts?.[0]?.id

  const enableMutation = useMutation({
    mutationFn: () => {
      if (!accountId) return Promise.reject(new Error('no_account'))
      return liveTradingApi.setTradingEnabled(accountId, true)
    },
    onSuccess: async () => {
      setError(null)
      setLegalPending(false)
      setCodePending(false)
      setEnabledOk(true)
      await invalidateAccess()
    },
    onError: (e: any) => {
      const detail = e?.response?.data?.detail
      if (typeof detail === 'string' && detail.startsWith('acknowledgment_required:')) {
        setError(null)
        setLegalPending(true)
        return
      }
      if (typeof detail === 'string' && detail === 'verification_required:enable_automation') {
        setError(null)
        setCodePending(true)
        return
      }
      if (e?.message === 'no_account') {
        setError('No brokerage account found. Connect a broker first.')
        return
      }
      setError(typeof detail === 'string' ? detail : 'Could not enable automation. Try again.')
    },
  })

  const disableMutation = useMutation({
    mutationFn: () => {
      if (!accountId) return Promise.reject(new Error('no_account'))
      return liveTradingApi.setTradingEnabled(accountId, false)
    },
    onSuccess: async () => {
      setError(null)
      setEnabledOk(false)
      await invalidateAccess()
    },
    onError: (e: any) => {
      const detail = e?.response?.data?.detail
      setError(typeof detail === 'string' ? detail : 'Could not turn off automation. Try again.')
    },
  })

  if (!access) return null

  const status = access.automation_status
  const isOn = enabledOk || status === 'enabled'

  // ── No broker account: must connect one first ──
  if (!access.has_broker_account) {
    return (
      <div className="rounded-xl border border-amber-200 dark:border-amber-900/40 bg-amber-50 dark:bg-amber-900/20 p-4 flex items-start gap-3">
        <Building2 size={18} className="text-amber-600 dark:text-amber-300 flex-shrink-0 mt-0.5"/>
        <div className="min-w-0">
          <div className="text-sm font-semibold text-amber-900 dark:text-amber-100">Connect a brokerage account first</div>
          <p className="text-xs text-amber-700 dark:text-amber-200 mt-1">
            Fully automated trading places live orders through your linked broker. Add one to continue.
          </p>
          <Link
            to="/app/live"
            onClick={onClose}
            className="inline-flex items-center gap-1.5 mt-2 bg-blue-600 hover:bg-blue-700 text-white rounded-xl px-4 py-2 text-xs font-semibold"
          >
            <Building2 size={14}/> Connect a broker
          </Link>
        </div>
      </div>
    )
  }

  // ── Automation already ON ──
  if (isOn) {
    return (
      <>
        <div className="rounded-xl border border-emerald-200 dark:border-emerald-900/40 bg-emerald-50 dark:bg-emerald-900/20 p-4 flex items-start gap-3">
          <CheckCircle2 size={18} className="text-emerald-600 dark:text-emerald-300 flex-shrink-0 mt-0.5"/>
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold text-emerald-900 dark:text-emerald-100">Automation is ON</div>
            <p className="text-xs text-emerald-700 dark:text-emerald-200 mt-1">
              Signals are placed automatically through your linked broker. You can turn this off at any time.
            </p>
          </div>
        </div>
        {error && (
          <div className="mt-3 rounded-xl border border-red-200 dark:border-red-900/40 bg-red-50 dark:bg-red-900/20 p-3 text-xs text-red-800 dark:text-red-300">
            {error}
          </div>
        )}
        <div className="mt-3 flex justify-end gap-2">
          {confirmOff && !disableMutation.isPending && (
            <button
              onClick={() => setConfirmOff(false)}
              className="border border-slate-300 dark:border-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 rounded-xl px-5 py-2.5 text-sm font-semibold"
            >
              Cancel
            </button>
          )}
          <button
            onClick={() => { if (confirmOff) { setConfirmOff(false); disableMutation.mutate() } else { setConfirmOff(true) } }}
            disabled={disableMutation.isPending || !accountId}
            className="inline-flex items-center gap-1.5 border border-slate-300 dark:border-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 rounded-xl px-5 py-2.5 text-sm font-semibold disabled:opacity-50"
          >
            <Power size={14}/> {disableMutation.isPending ? 'Turning off…' : confirmOff ? 'Confirm: turn off automation' : 'Turn off automation'}
          </button>
        </div>
      </>
    )
  }

  // ── Eligible but not yet enabled (agreement_required / pending / disabled) ──
  const busy = enableMutation.isPending || accountsLoading

  return (
    <>
      <div className="rounded-xl border border-violet-200 dark:border-violet-900/40 bg-violet-50 dark:bg-violet-900/20 p-4 flex items-start gap-3">
        <Bot size={18} className="text-violet-600 dark:text-violet-300 flex-shrink-0 mt-0.5"/>
        <div className="min-w-0">
          <div className="text-sm font-semibold text-violet-900 dark:text-violet-100">Enable fully automated trading</div>
          <p className="text-xs text-violet-700 dark:text-violet-200 mt-1">
            We’ll place qualifying signals automatically through your linked broker. You’ll review the required
            disclosures and verify it’s you before automation turns on.
          </p>
        </div>
      </div>

      {error && (
        <div className="mt-3 rounded-xl border border-red-200 dark:border-red-900/40 bg-red-50 dark:bg-red-900/20 p-3 flex items-start gap-3 text-xs text-red-800 dark:text-red-300">
          <AlertCircle size={16} className="flex-shrink-0 mt-0.5"/>
          <span>{error}</span>
        </div>
      )}

      {access.has_broker_account && !accountsLoading && (accountsError || !accountId) && (
        <div className="mt-3 rounded-xl border border-amber-200 dark:border-amber-900/40 bg-amber-50 dark:bg-amber-900/20 p-3 text-xs text-amber-800 dark:text-amber-200">
          We couldn’t load your linked broker account. Reconnect it in{' '}
          <Link to="/app/live" onClick={onClose} className="font-semibold underline">Live Trading</Link>.
        </div>
      )}

      <div className="mt-3 flex justify-end">
        <button
          onClick={() => { setError(null); enableMutation.mutate() }}
          disabled={busy || !accountId}
          className="inline-flex items-center gap-1.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white rounded-xl px-5 py-2.5 text-sm font-semibold"
        >
          {busy ? <Loader2 size={14} className="animate-spin"/> : <ShieldCheck size={14}/>}
          {busy ? 'Working…' : 'Enable automation'}
        </button>
      </div>

      {/* Legal acks gate — fires on acknowledgment_required:, retries enable on completion */}
      {legalPending && (
        <LegalGate
          kinds={AUTOMATION_KINDS}
          onComplete={() => { setLegalPending(false); enableMutation.mutate() }}
          onCancel={() => setLegalPending(false)}
        />
      )}

      {/* Email-code gate — fires on verification_required:enable_automation, retries enable on success */}
      {codePending && (
        <CodeVerifyModal
          purpose="enable_automation"
          title="Verify to enable automation"
          subtitle="Enter the 6-digit code we emailed to authorize automated trading."
          onVerified={() => { setCodePending(false); enableMutation.mutate() }}
          onCancel={() => setCodePending(false)}
        />
      )}
    </>
  )
}
