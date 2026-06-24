import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { X, Wallet, TrendingUp, AlertCircle, RefreshCw, Lock, Unlock, Calculator, ShieldAlert } from 'lucide-react'
import { liveTradingApi, legalApi } from '../api/endpoints'
import CodeVerifyModal from './CodeVerifyModal'

interface Props {
  account: any   // BrokerAccount row
  onClose: () => void
}

type RiskMode = 'dollar' | 'percent'

export default function SizingModal({ account, onClose }: Props) {
  const qc = useQueryClient()
  const [accountType, setAccountType] = useState<'cash' | 'margin'>(account.account_type || 'cash')
  const [riskMode, setRiskMode] = useState<RiskMode>('dollar')
  const [riskUsd, setRiskUsd] = useState<string>('')
  const [riskPct, setRiskPct] = useState<string>('1.0')
  const [maxPos, setMaxPos] = useState<string>('')
  const [error, setError] = useState<string | null>(null)
  const [ackPending, setAckPending] = useState(false)
  const [codePending, setCodePending] = useState(false)

  // Load saved sizing settings
  const { data: sizing } = useQuery({
    queryKey: ['sizing', account.id],
    queryFn: () => liveTradingApi.getSizing(account.id).then(r => r.data),
  })

  // Live balance fetch
  const balanceQ = useQuery({
    queryKey: ['balance', account.id],
    queryFn: () => liveTradingApi.getBalance(account.id).then(r => r.data),
    retry: false,
  })

  useEffect(() => {
    if (sizing) {
      setAccountType(sizing.account_type || 'cash')
      if (sizing.risk_per_trade_usd != null) {
        setRiskMode('dollar')
        setRiskUsd(String(sizing.risk_per_trade_usd))
      } else if (sizing.risk_per_trade_pct != null) {
        setRiskMode('percent')
        setRiskPct(String(sizing.risk_per_trade_pct))
      }
      if (sizing.max_position_usd != null) setMaxPos(String(sizing.max_position_usd))
    }
  }, [sizing])

  const saveMutation = useMutation({
    mutationFn: () => liveTradingApi.saveSizing(account.id, {
      account_type: accountType,
      risk_per_trade_usd: riskMode === 'dollar' && riskUsd ? parseFloat(riskUsd) : null,
      risk_per_trade_pct: riskMode === 'percent' && riskPct ? parseFloat(riskPct) : null,
      max_position_usd: maxPos ? parseFloat(maxPos) : null,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['broker-accounts'] })
      qc.invalidateQueries({ queryKey: ['sizing', account.id] })
      onClose()
    },
    onError: (e: any) => {
      const detail = e?.response?.data?.detail
      // Raising a risk limit is gated server-side: first "I agree", then an
      // emailed code. Walk the user through each step, then retry the save.
      if (typeof detail === 'string' && detail.startsWith('acknowledgment_required')) {
        setError(null); setAckPending(true); return
      }
      if (typeof detail === 'string' && detail.startsWith('verification_required')) {
        setError(null); setCodePending(true); return
      }
      setError(typeof detail === 'string' ? detail : 'Could not save.')
    },
  })

  // "I agree to these changes" → record the risk_change acknowledgment, then
  // re-attempt the save (which then asks for the emailed code if still needed).
  const ackMutation = useMutation({
    mutationFn: () => legalApi.acknowledge('risk_change', 'Raised account risk/allocation settings'),
    onSuccess: () => { setAckPending(false); saveMutation.mutate() },
    onError: (e: any) => { setAckPending(false); setError(e?.response?.data?.detail || 'Could not record agreement.') },
  })

  const equity = balanceQ.data?.equity ?? sizing?.cached_equity ?? 0
  const buyingPower = balanceQ.data?.buying_power ?? sizing?.cached_buying_power ?? 0
  const detectedType = balanceQ.data?.account_type
  const inMarginCall = !!balanceQ.data?.margin_call

  // Live preview of what the bot WOULD risk per trade
  const previewRiskUsd = riskMode === 'dollar'
    ? parseFloat(riskUsd || '0')
    : (equity * parseFloat(riskPct || '0')) / 100
  const previewMaxPos = maxPos ? parseFloat(maxPos) : null

  return (
    <>
    <div className="fixed inset-0 z-[100] bg-black/70 flex items-center justify-center p-4">
      <div className="w-full max-w-lg bg-white dark:bg-slate-900 rounded-2xl shadow-2xl max-h-[90vh] overflow-y-auto">
        <div className="sticky top-0 bg-white dark:bg-slate-900 px-6 py-4 border-b border-slate-200 dark:border-slate-700 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-violet-100 dark:bg-violet-900/40 text-violet-700 dark:text-violet-300 flex items-center justify-center">
              <Calculator size={18}/>
            </div>
            <div>
              <h2 className="text-lg font-extrabold text-slate-900 dark:text-slate-100">Position Sizing</h2>
              <p className="text-xs text-slate-500 dark:text-slate-400">{account.account_name} · {account.broker}</p>
            </div>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-700 dark:hover:text-slate-200">
            <X size={20}/>
          </button>
        </div>

        <div className="p-6 space-y-5">
          {/* Live balance readout */}
          <div className="rounded-xl border border-slate-200 dark:border-slate-700 p-4 bg-slate-50 dark:bg-slate-800">
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-1.5 text-xs uppercase tracking-wider font-bold text-slate-600 dark:text-slate-300">
                <Wallet size={12}/> Account snapshot
              </div>
              <button
                onClick={() => balanceQ.refetch()}
                disabled={balanceQ.isFetching}
                className="text-[11px] inline-flex items-center gap-1 text-violet-600 dark:text-violet-400 hover:underline disabled:opacity-50">
                <RefreshCw size={11} className={balanceQ.isFetching ? 'animate-spin' : ''}/> Refresh
              </button>
            </div>
            {balanceQ.isError && (
              <div className="text-[11px] text-red-600 dark:text-red-400">
                Couldn't reach broker. Showing last cached values.
              </div>
            )}
            <div className="grid grid-cols-2 gap-3 text-sm">
              <div>
                <div className="text-[11px] text-slate-500 dark:text-slate-400">Equity</div>
                <div className="font-extrabold text-slate-900 dark:text-slate-100">${equity.toLocaleString(undefined, {maximumFractionDigits: 2})}</div>
              </div>
              <div>
                <div className="text-[11px] text-slate-500 dark:text-slate-400">Buying power</div>
                <div className="font-extrabold text-slate-900 dark:text-slate-100">${buyingPower.toLocaleString(undefined, {maximumFractionDigits: 2})}</div>
              </div>
            </div>
            {detectedType && detectedType !== accountType && (
              <div className="mt-2 text-[11px] text-amber-600 dark:text-amber-400">
                Broker reports <strong>{detectedType.toUpperCase()}</strong> — your setting will be overridden when fetched.
              </div>
            )}
            {inMarginCall && (
              <div className="mt-2 text-[11px] text-red-600 dark:text-red-400 font-semibold flex items-center gap-1">
                <AlertCircle size={11}/> MARGIN CALL — no new positions allowed.
              </div>
            )}
          </div>

          {/* Cash vs Margin */}
          <div>
            <div className="text-xs font-bold text-slate-700 dark:text-slate-200 mb-2">Account type</div>
            <div className="grid grid-cols-2 gap-2">
              <button
                onClick={() => setAccountType('cash')}
                className={`rounded-xl border-2 p-3 text-left transition-all ${
                  accountType === 'cash'
                    ? 'border-violet-500 bg-violet-50 dark:bg-violet-900/30'
                    : 'border-slate-200 dark:border-slate-700 hover:border-slate-300'
                }`}>
                <div className="flex items-center gap-2 mb-1">
                  <Lock size={14} className="text-slate-700 dark:text-slate-200"/>
                  <span className="font-bold text-sm text-slate-900 dark:text-slate-100">Cash</span>
                </div>
                <p className="text-[11px] text-slate-500 dark:text-slate-400 leading-snug">
                  Only your settled cash. No leverage. Safer; can't go negative.
                </p>
              </button>
              <button
                onClick={() => setAccountType('margin')}
                className={`rounded-xl border-2 p-3 text-left transition-all ${
                  accountType === 'margin'
                    ? 'border-violet-500 bg-violet-50 dark:bg-violet-900/30'
                    : 'border-slate-200 dark:border-slate-700 hover:border-slate-300'
                }`}>
                <div className="flex items-center gap-2 mb-1">
                  <Unlock size={14} className="text-violet-700 dark:text-violet-300"/>
                  <span className="font-bold text-sm text-slate-900 dark:text-slate-100">Margin</span>
                </div>
                <p className="text-[11px] text-slate-500 dark:text-slate-400 leading-snug">
                  Up to 2× buying power. PDT rules apply ≥ $25k. Owes interest.
                </p>
              </button>
            </div>
          </div>

          {/* Risk per trade */}
          <div>
            <div className="text-xs font-bold text-slate-700 dark:text-slate-200 mb-2">Risk per trade</div>
            <div className="flex gap-2 mb-3">
              <button
                onClick={() => setRiskMode('dollar')}
                className={`flex-1 rounded-lg px-3 py-1.5 text-xs font-bold transition-all ${
                  riskMode === 'dollar' ? 'bg-violet-600 text-white' : 'bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300'
                }`}>$ Fixed</button>
              <button
                onClick={() => setRiskMode('percent')}
                className={`flex-1 rounded-lg px-3 py-1.5 text-xs font-bold transition-all ${
                  riskMode === 'percent' ? 'bg-violet-600 text-white' : 'bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300'
                }`}>% of equity</button>
            </div>
            {riskMode === 'dollar' ? (
              <div>
                <div className="relative">
                  <span className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 font-bold">$</span>
                  <input
                    type="number"
                    placeholder="e.g. 1000"
                    value={riskUsd}
                    onChange={(e) => setRiskUsd(e.target.value)}
                    className="w-full pl-7 pr-3 py-2.5 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100 font-bold text-lg"
                  />
                </div>
                <p className="text-[10px] text-slate-500 dark:text-slate-400 mt-1">
                  On a 100k account, $1,000 = 1% risk. Conservative.
                </p>
              </div>
            ) : (
              <div>
                <div className="relative">
                  <input
                    type="number"
                    step="0.1"
                    placeholder="1.0"
                    value={riskPct}
                    onChange={(e) => setRiskPct(e.target.value)}
                    className="w-full pl-3 pr-8 py-2.5 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100 font-bold text-lg"
                  />
                  <span className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 font-bold">%</span>
                </div>
                <p className="text-[10px] text-slate-500 dark:text-slate-400 mt-1">
                  Scales with equity. 1% = ${(equity * 0.01).toFixed(0)} per trade today.
                </p>
              </div>
            )}
          </div>

          {/* Max position cap */}
          <div>
            <div className="text-xs font-bold text-slate-700 dark:text-slate-200 mb-2 flex items-center gap-1.5">
              Max position size <span className="text-slate-400 font-normal">(optional)</span>
            </div>
            <div className="relative">
              <span className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 font-bold">$</span>
              <input
                type="number"
                placeholder="e.g. 10000 (no more than $10k/trade)"
                value={maxPos}
                onChange={(e) => setMaxPos(e.target.value)}
                className="w-full pl-7 pr-3 py-2.5 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100"
              />
            </div>
            <p className="text-[10px] text-slate-500 dark:text-slate-400 mt-1">
              Hard cap. If left empty, only buying power limits the trade.
            </p>
          </div>

          {/* Live preview */}
          <div className="rounded-xl bg-gradient-to-br from-violet-50 to-violet-100 dark:from-violet-900/20 dark:to-violet-900/30 border border-violet-200 dark:border-violet-800 p-4">
            <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider font-bold text-violet-700 dark:text-violet-300 mb-2">
              <TrendingUp size={11}/> Preview · per trade
            </div>
            <div className="space-y-1.5 text-xs text-slate-700 dark:text-slate-200">
              <div className="flex justify-between items-baseline rounded-lg bg-rose-50 dark:bg-rose-900/20 border border-rose-200 dark:border-rose-900 px-3 py-2">
                <span className="text-rose-700 dark:text-rose-300 font-bold">Max LOSS if stop hits</span>
                <span className="font-extrabold text-rose-700 dark:text-rose-300 text-lg">${previewRiskUsd.toFixed(0)}</span>
              </div>
              <div className="flex justify-between pt-1"><span className="text-slate-600 dark:text-slate-300">Capital cap per position</span><span className="font-bold">{previewMaxPos ? `$${previewMaxPos.toFixed(0)}` : 'no cap (uses buying power)'}</span></div>
              <div className="flex justify-between"><span className="text-slate-600 dark:text-slate-300">Available buying power</span><span className="font-bold">${buyingPower.toLocaleString(undefined, {maximumFractionDigits: 0})}</span></div>
              <hr className="border-violet-200 dark:border-violet-800 my-1.5"/>
              <div className="text-[10px] uppercase tracking-wider text-violet-600 dark:text-violet-400 font-bold">Examples · 2% stop</div>
              {(() => {
                const stopPct = 0.02
                const ex5 = previewRiskUsd > 0 ? Math.floor(previewRiskUsd / (5 * stopPct)) : 0
                const ex50 = previewRiskUsd > 0 ? Math.floor(previewRiskUsd / (50 * stopPct)) : 0
                return (
                  <>
                    <div className="grid grid-cols-[60px_1fr] gap-2 items-baseline">
                      <span className="font-bold text-slate-500 dark:text-slate-400">$5 stock</span>
                      <span><strong>{ex5}</strong> shares · <span className="text-slate-500">${(ex5*5).toLocaleString()} deployed</span> · <span className="text-rose-600 dark:text-rose-400 font-bold">${previewRiskUsd.toFixed(0)} at risk</span></span>
                    </div>
                    <div className="grid grid-cols-[60px_1fr] gap-2 items-baseline">
                      <span className="font-bold text-slate-500 dark:text-slate-400">$50 stock</span>
                      <span><strong>{ex50}</strong> shares · <span className="text-slate-500">${(ex50*50).toLocaleString()} deployed</span> · <span className="text-rose-600 dark:text-rose-400 font-bold">${previewRiskUsd.toFixed(0)} at risk</span></span>
                    </div>
                  </>
                )
              })()}
              <p className="text-[10px] text-slate-500 dark:text-slate-400 mt-2 leading-snug border-t border-violet-200/40 dark:border-violet-800/40 pt-2">
                <strong className="text-slate-700 dark:text-slate-200">Deployed</strong> = cost of shares bought (uses buying power, can be 10–50x your $ risk on tight-stop trades).<br/>
                <strong className="text-rose-600 dark:text-rose-400">At risk</strong> = max you actually lose if the stop hits. That's the dollar number you set above.
              </p>
            </div>
          </div>

          {error && (
            <div className="rounded-lg p-3 text-xs bg-red-50 border border-red-200 text-red-700 dark:bg-red-900/20 dark:border-red-900 dark:text-red-300 flex items-start gap-2">
              <AlertCircle size={14} className="flex-shrink-0 mt-0.5"/>{error}
            </div>
          )}
        </div>

        <div className="sticky bottom-0 bg-white dark:bg-slate-900 px-6 py-4 border-t border-slate-200 dark:border-slate-700 flex gap-2">
          <button onClick={onClose} className="px-4 py-2 rounded-lg text-sm font-bold text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800">
            Cancel
          </button>
          <button
            onClick={() => saveMutation.mutate()}
            disabled={saveMutation.isPending}
            className="flex-1 px-4 py-2 rounded-lg text-sm font-bold bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white">
            {saveMutation.isPending ? 'Saving…' : 'Save sizing'}
          </button>
        </div>
      </div>
    </div>

    {ackPending && (
      <div className="fixed inset-0 z-[110] bg-black/70 flex items-center justify-center p-4">
        <div className="w-full max-w-md bg-white dark:bg-slate-900 rounded-2xl shadow-2xl p-6">
          <div className="flex items-center gap-2 mb-3">
            <ShieldAlert size={18} className="text-amber-600"/>
            <h3 className="text-base font-extrabold text-slate-900 dark:text-slate-100">Confirm risk change</h3>
          </div>
          <p className="text-sm text-slate-600 dark:text-slate-300 mb-4">
            You’re raising a risk limit on this account — this lets the bot deploy more capital or risk more per trade.
            By continuing you agree to these changes. We’ll then email a 6-digit code to confirm it’s you, and this change is logged.
          </p>
          <div className="flex gap-2">
            <button onClick={() => setAckPending(false)} className="px-4 py-2 rounded-lg text-sm font-bold text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800">Cancel</button>
            <button onClick={() => ackMutation.mutate()} disabled={ackMutation.isPending}
              className="flex-1 px-4 py-2 rounded-lg text-sm font-bold bg-amber-600 hover:bg-amber-700 disabled:opacity-50 text-white">
              {ackMutation.isPending ? 'Working…' : 'I agree to these changes'}
            </button>
          </div>
        </div>
      </div>
    )}

    {codePending && (
      <CodeVerifyModal purpose="risk_change" title="Verify to change risk settings"
        subtitle="Enter the 6-digit code we emailed to authorize this risk change."
        onVerified={() => { setCodePending(false); saveMutation.mutate() }}
        onCancel={() => setCodePending(false)} />
    )}
    </>
  )
}
