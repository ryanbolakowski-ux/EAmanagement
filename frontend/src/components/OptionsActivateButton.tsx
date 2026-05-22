/**
 * OptionsActivateButton — start or stop a paper/live options session for one
 * strategy. Renders a single button that toggles between Activate and Stop
 * based on whether the strategy already has an active session.
 *
 * Paper mode: spins up the BS-priced runner, no broker connection.
 * Live mode:  routes through a Tradier broker account (user picks from a
 *             dropdown of their connected Tradier accounts).
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Play, Square, AlertCircle } from 'lucide-react'
import { optionsApi, liveTradingApi } from '../api/endpoints'
import LegalGate from './LegalGate'

interface Props {
  strategyId: string
  underlyings: string[]
}

export default function OptionsActivateButton({ strategyId, underlyings }: Props) {
  const qc = useQueryClient()
  const [showPicker, setShowPicker] = useState(false)
  const [mode, setMode] = useState<'paper' | 'live'>('paper')
  const [brokerAccountId, setBrokerAccountId] = useState<string>('')
  const [startingBalance, setStartingBalance] = useState<number>(10_000)
  const [legalPending, setLegalPending] = useState(false)

  // Find an active session for this strategy
  const { data: sessionsData } = useQuery({
    queryKey: ['options-sessions'],
    queryFn: () => optionsApi.listSessions().then(r => r.data),
    refetchInterval: 15_000,
  })
  const active = sessionsData?.sessions.find(s => s.strategy_id === strategyId && s.is_active)

  // Pull broker accounts for the Tradier dropdown when live mode is selected
  const { data: brokerAccountsData } = useQuery({
    queryKey: ['broker-accounts'],
    queryFn: () => liveTradingApi.listAccounts().then(r => r.data),
    enabled: showPicker && mode === 'live',
  })
  const tradierAccounts = (brokerAccountsData || []).filter((a: any) => (a.broker || '').toLowerCase() === 'tradier')

  const startMutation = useMutation({
    mutationFn: () => optionsApi.startSession({
      strategy_id: strategyId,
      underlyings,
      starting_balance: startingBalance,
      mode,
      ...(mode === 'live' ? { broker_account_id: brokerAccountId } : {}),
    } as any),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['options-sessions'] })
      setShowPicker(false)
    },
    onError: (e: any) => {
      const detail = e?.response?.data?.detail || ''
      if (typeof detail === 'string' && detail.startsWith('acknowledgment_required:')) {
        // The legal gate will handle it
        setLegalPending(true)
      }
    },
  })

  const stopMutation = useMutation({
    mutationFn: () => optionsApi.stopSession(active!.session_id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['options-sessions'] }),
  })

  if (active) {
    return (
      <button onClick={() => stopMutation.mutate()}
        disabled={stopMutation.isPending}
        title={`Stop session — ${active.total_trades} trades, $${active.net_pnl.toFixed(2)} P&L`}
        className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[11px] font-bold bg-red-600 hover:bg-red-700 disabled:opacity-50 text-white transition-colors">
        <Square size={11} fill="white"/> {stopMutation.isPending ? 'Stopping…' : 'Stop'}
      </button>
    )
  }

  return (
    <>
      <button onClick={() => setShowPicker(true)}
        className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[11px] font-bold bg-green-600 hover:bg-green-700 text-white transition-colors">
        <Play size={11} fill="white"/> Activate
      </button>

      {showPicker && !legalPending && (
        <div className="fixed inset-0 z-[90] bg-black/60 flex items-center justify-center p-4" onClick={() => setShowPicker(false)}>
          <div onClick={e => e.stopPropagation()}
            className="w-full max-w-md bg-white dark:bg-slate-900 rounded-2xl shadow-2xl p-6">
            <h3 className="text-base font-extrabold text-slate-900 dark:text-slate-100 mb-1">Activate options session</h3>
            <p className="text-xs text-slate-500 dark:text-slate-400 mb-4">
              {underlyings.join(', ')} · strike picked automatically per signal
            </p>

            <div className="space-y-3 mb-4">
              <div>
                <label className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 block mb-1.5">Mode</label>
                <div className="flex gap-2">
                  <button onClick={() => setMode('paper')}
                    className={`flex-1 px-3 py-2 rounded-lg text-xs font-bold border ${mode === 'paper'
                      ? 'bg-blue-50 border-blue-300 text-blue-700 dark:bg-blue-900/30 dark:border-blue-700 dark:text-blue-300'
                      : 'border-slate-300 dark:border-slate-700 text-slate-600 dark:text-slate-300'}`}>
                    Paper
                  </button>
                  <button onClick={() => setMode('live')}
                    className={`flex-1 px-3 py-2 rounded-lg text-xs font-bold border ${mode === 'live'
                      ? 'bg-red-50 border-red-300 text-red-700 dark:bg-red-900/30 dark:border-red-700 dark:text-red-300'
                      : 'border-slate-300 dark:border-slate-700 text-slate-600 dark:text-slate-300'}`}>
                    Live (Tradier)
                  </button>
                </div>
              </div>

              {mode === 'live' && (
                <div>
                  <label className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 block mb-1.5">Tradier account</label>
                  {tradierAccounts.length === 0 ? (
                    <div className="flex items-start gap-2 p-3 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-900/40 text-xs text-amber-800 dark:text-amber-200">
                      <AlertCircle size={13} className="flex-shrink-0 mt-0.5"/>
                      <span>No Tradier accounts connected. Add one in <strong>Live Trading → Connect Account</strong> first.</span>
                    </div>
                  ) : (
                    <select value={brokerAccountId} onChange={e => setBrokerAccountId(e.target.value)}
                      className="w-full px-3 py-2 rounded-lg border border-slate-300 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 text-sm">
                      <option value="">Select an account…</option>
                      {tradierAccounts.map((a: any) => (
                        <option key={a.id} value={a.id}>
                          {a.account_name} · {a.sandbox_mode ? 'Sandbox' : 'LIVE'}
                        </option>
                      ))}
                    </select>
                  )}
                </div>
              )}

              <div>
                <label className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 block mb-1.5">
                  {mode === 'live' ? 'Starting equity used for sizing' : 'Paper starting balance'}
                </label>
                <input type="number" min={1000} step={1000}
                  value={startingBalance}
                  onChange={e => setStartingBalance(Number(e.target.value || 10000))}
                  className="w-full px-3 py-2 rounded-lg border border-slate-300 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 text-sm tabular-nums"/>
              </div>
            </div>

            {startMutation.isError && !legalPending && (
              <div className="rounded-lg p-2.5 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-900/40 text-[11px] text-red-800 dark:text-red-300 mb-3">
                {(startMutation.error as any)?.response?.data?.detail || 'Could not start session.'}
              </div>
            )}

            <div className="flex gap-2 justify-end">
              <button onClick={() => setShowPicker(false)}
                className="px-4 py-2 rounded-lg border border-slate-300 dark:border-slate-700 text-sm font-semibold">Cancel</button>
              <button onClick={() => startMutation.mutate()}
                disabled={startMutation.isPending || (mode === 'live' && (!brokerAccountId || tradierAccounts.length === 0))}
                className="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-bold inline-flex items-center gap-1.5">
                <Play size={12} fill="white"/> {startMutation.isPending ? 'Starting…' : 'Start'}
              </button>
            </div>
          </div>
        </div>
      )}

      {legalPending && (
        <LegalGate
          kinds={mode === 'live'
            ? ['risk_disclosure', 'live_trading_consent', 'options_trading_consent']
            : ['options_trading_consent']}
          onComplete={() => { setLegalPending(false); startMutation.mutate() }}
          onCancel={() => setLegalPending(false)}
        />
      )}
    </>
  )
}
