/**
 * AutomationActivation — tier_5 fully-automated-trading control (Phase G).
 *
 * Server-driven flow (frontend state is advisory; the backend enforces the
 * same gates so nothing here can bypass them):
 *  - No broker account        -> prompt to connect one.
 *  - Agreement NOT signed      -> show the risk-disclosure summary + a
 *                                 "Review & Sign Agreement" button that opens
 *                                 the full Fully Automated Trading Agreement.
 *  - Agreement signed          -> show "signed on <date> (vX)" (never re-prompt
 *                                 the same version) + an Enable/Disable toggle.
 *                                 Enabling may require an emailed code; it does
 *                                 NOT require re-signing once the current
 *                                 version is accepted.
 * Signed status + on/off state come from the server (my-access + legal/status),
 * so re-opening the modal always reflects the true state.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Bot, ShieldCheck, AlertCircle, Building2, Power, Loader2, FileText } from 'lucide-react'
import { liveTradingApi, legalApi } from '../api/endpoints'
import { useMyAccess, useInvalidateMyAccess } from '../hooks/useMyAccess'
import LegalGate from './LegalGate'
import CodeVerifyModal from './CodeVerifyModal'
import type { LegalKind } from '../api/endpoints'

const AUTOMATION_KINDS: LegalKind[] = ['risk_disclosure', 'live_trading_consent', 'fully_automated_trading']

const DISCLOSURE_POINTS = [
  'Automated trading can place, manage, and close trades without further manual approval.',
  'It may place entries, stop-losses, targets, trailing stops, break-even adjustments, and closing orders per your strategy settings.',
  'Trading involves risk and losses are possible. Theta Algos does not guarantee profits, and this is not financial advice.',
  'You are responsible for your broker account, your settings, your risk limits, and enabling or disabling automation.',
  'You can disable automation at any time.',
  'By signing, you authorize Theta Algos to automate trades according to your selected settings.',
]

interface Props { onClose?: () => void }

export default function AutomationActivation({ onClose }: Props) {
  const { data: access } = useMyAccess()
  const invalidateAccess = useInvalidateMyAccess()
  const qc = useQueryClient()

  // Agreement signed status + when + which version (for the "signed on ..." line).
  const { data: legal } = useQuery({
    queryKey: ['legal-status'],
    queryFn: () => legalApi.status().then((r: any) => r.data),
    staleTime: 15_000,
  })
  const fat = legal?.acknowledgments?.fully_automated_trading as
    | { current_version?: string; accepted?: boolean; accepted_at?: string | null }
    | undefined

  const [legalPending, setLegalPending] = useState(false)
  const [codePending, setCodePending] = useState(false)
  const [confirmOff, setConfirmOff] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const { data: accounts, isLoading: accountsLoading, isError: accountsError } = useQuery({
    queryKey: ['broker-accounts'],
    queryFn: () => liveTradingApi.listAccounts().then(r => r.data),
    enabled: !!access?.has_broker_account,
  })
  const accountId = accounts?.[0]?.id

  const refetchAll = () => {
    invalidateAccess()
    qc.invalidateQueries({ queryKey: ['legal-status'] })
  }

  const enableMutation = useMutation({
    mutationFn: () => {
      if (!accountId) return Promise.reject(new Error('no_account'))
      return liveTradingApi.setTradingEnabled(accountId, true)
    },
    onSuccess: () => { setError(null); setLegalPending(false); setCodePending(false); refetchAll() },
    onError: (e: any) => {
      const detail = e?.response?.data?.detail
      if (typeof detail === 'string' && detail.startsWith('acknowledgment_required:')) { setError(null); setLegalPending(true); return }
      if (typeof detail === 'string' && detail === 'verification_required:enable_automation') { setError(null); setCodePending(true); return }
      if (e?.message === 'no_account') { setError('No brokerage account found. Connect a broker first.'); return }
      setError(typeof detail === 'string' ? detail : 'Could not enable automation. Try again.')
    },
  })

  const disableMutation = useMutation({
    mutationFn: () => {
      if (!accountId) return Promise.reject(new Error('no_account'))
      return liveTradingApi.setTradingEnabled(accountId, false)
    },
    onSuccess: () => { setError(null); setConfirmOff(false); refetchAll() },
    onError: (e: any) => {
      const detail = e?.response?.data?.detail
      setError(typeof detail === 'string' ? detail : 'Could not turn off automation. Try again.')
    },
  })

  if (!access) return null

  // automation_status pending/disabled/enabled all imply the agreement is
  // already accepted (server logic), so use it as the primary "signed" signal —
  // the legal-status query just supplies the timestamp/version for display.
  const statusSigned = ['pending', 'disabled', 'enabled'].includes(access.automation_status)
  const agreementSigned = statusSigned || !!fat?.accepted
  const signedAt = fat?.accepted_at || null
  const signedVersion = fat?.current_version
  const isOn = access.automation_status === 'enabled'
  const busy = enableMutation.isPending || accountsLoading

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
          <Link to="/app/live" onClick={onClose}
            className="inline-flex items-center gap-1.5 mt-2 bg-blue-600 hover:bg-blue-700 text-white rounded-xl px-4 py-2 text-xs font-semibold">
            <Building2 size={14}/> Connect a broker
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {/* ── Agreement status ── */}
      {agreementSigned ? (
        <div className="rounded-xl border border-emerald-200 dark:border-emerald-900/40 bg-emerald-50 dark:bg-emerald-900/20 p-4 flex items-start gap-3">
          <ShieldCheck size={18} className="text-emerald-600 dark:text-emerald-300 flex-shrink-0 mt-0.5"/>
          <div className="min-w-0">
            <div className="text-sm font-semibold text-emerald-900 dark:text-emerald-100">Automation agreement signed</div>
            <p className="text-xs text-emerald-700 dark:text-emerald-200 mt-1">
              Fully Automated Trading Agreement
              {signedAt ? ` · signed ${new Date(signedAt).toLocaleString()}` : ''}
              {signedVersion ? ` · version ${signedVersion}` : ''}
            </p>
          </div>
        </div>
      ) : (
        <div className="rounded-xl border border-violet-200 dark:border-violet-900/40 bg-violet-50 dark:bg-violet-900/20 p-4 flex items-start gap-3">
          <Bot size={18} className="text-violet-600 dark:text-violet-300 flex-shrink-0 mt-0.5"/>
          <div className="min-w-0">
            <div className="text-sm font-semibold text-violet-900 dark:text-violet-100">Review &amp; sign before enabling automation</div>
            <ul className="mt-2 space-y-1">
              {DISCLOSURE_POINTS.map((p, i) => (
                <li key={i} className="text-xs text-violet-800 dark:text-violet-200 flex items-start gap-1.5">
                  <span className="mt-1.5 w-1 h-1 rounded-full bg-violet-500 dark:bg-violet-400 flex-shrink-0"/>
                  <span>{p}</span>
                </li>
              ))}
            </ul>
            <button onClick={() => { setError(null); setLegalPending(true) }}
              className="inline-flex items-center gap-1.5 mt-3 bg-blue-600 hover:bg-blue-700 text-white rounded-xl px-5 py-2.5 text-sm font-semibold">
              <FileText size={14}/> Review &amp; Sign Agreement
            </button>
          </div>
        </div>
      )}

      {/* ── Errors ── */}
      {error && (
        <div className="rounded-xl border border-red-200 dark:border-red-900/40 bg-red-50 dark:bg-red-900/20 p-3 flex items-start gap-3 text-xs text-red-800 dark:text-red-300">
          <AlertCircle size={16} className="flex-shrink-0 mt-0.5"/><span>{error}</span>
        </div>
      )}
      {!accountsLoading && (accountsError || !accountId) && (
        <div className="rounded-xl border border-amber-200 dark:border-amber-900/40 bg-amber-50 dark:bg-amber-900/20 p-3 text-xs text-amber-800 dark:text-amber-200">
          We couldn’t load your linked broker account. Reconnect it in{' '}
          <Link to="/app/live" onClick={onClose} className="font-semibold underline">Live Trading</Link>.
        </div>
      )}

      {/* ── Automation on/off toggle (only once the agreement is signed) ── */}
      {agreementSigned && (
        isOn ? (
          <div className="rounded-xl border border-emerald-200 dark:border-emerald-900/40 bg-emerald-50 dark:bg-emerald-900/20 p-4">
            <span className="badge badge-green inline-flex items-center gap-1"><Bot size={12}/> Automation ON</span>
            <p className="text-xs text-emerald-700 dark:text-emerald-200 mt-2">
              Qualifying signals are placed automatically through your linked broker. You can turn this off at any time.
            </p>
            <div className="mt-3 flex justify-end gap-2">
              {confirmOff && !disableMutation.isPending && (
                <button onClick={() => setConfirmOff(false)}
                  className="border border-slate-300 dark:border-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 rounded-xl px-5 py-2.5 text-sm font-semibold">
                  Cancel
                </button>
              )}
              <button
                onClick={() => { if (confirmOff) { setConfirmOff(false); disableMutation.mutate() } else { setConfirmOff(true) } }}
                disabled={disableMutation.isPending || !accountId}
                className="inline-flex items-center gap-1.5 border border-slate-300 dark:border-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 rounded-xl px-5 py-2.5 text-sm font-semibold disabled:opacity-50">
                <Power size={14}/> {disableMutation.isPending ? 'Turning off…' : confirmOff ? 'Confirm: turn off automation' : 'Turn off automation'}
              </button>
            </div>
          </div>
        ) : (
          <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm font-semibold text-slate-900 dark:text-slate-100">Automation is off</div>
                <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5">
                  Turn it on to let qualifying signals place automatically. We’ll verify it’s you by email — no re-signing needed.
                </p>
              </div>
              <button onClick={() => { setError(null); enableMutation.mutate() }}
                disabled={busy || !accountId}
                className="inline-flex items-center gap-1.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white rounded-xl px-5 py-2.5 text-sm font-semibold flex-shrink-0">
                {busy ? <Loader2 size={14} className="animate-spin"/> : <ShieldCheck size={14}/>}
                {busy ? 'Working…' : 'Enable automation'}
              </button>
            </div>
          </div>
        )
      )}

      {/* ── Gates ── */}
      {legalPending && (
        <LegalGate kinds={AUTOMATION_KINDS}
          onComplete={() => { setLegalPending(false); refetchAll() }}
          onCancel={() => setLegalPending(false)} />
      )}
      {codePending && (
        <CodeVerifyModal purpose="enable_automation" title="Verify to enable automation"
          subtitle="Enter the 6-digit code we emailed to authorize automated trading."
          onVerified={() => { setCodePending(false); enableMutation.mutate() }}
          onCancel={() => setCodePending(false)} />
      )}
    </div>
  )
}
