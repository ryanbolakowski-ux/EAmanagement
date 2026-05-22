/**
 * OptionsStrikePreview — embedded in the StrategyBuilder card for options
 * strategies. Pulls the chain, asks the picker which strike *this strategy*
 * would currently choose, and shows the contract details + greeks + cost.
 *
 * The intent is to remove the "I have no idea what this strategy actually
 * trades" mystery. If you save a strategy with options_target_delta_min=0.3,
 * you should be able to glance at the card and see "right now this would
 * buy SPY 510C 30DTE @ $5.20/contract, delta 0.42".
 */
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Activity, TrendingUp, TrendingDown, AlertTriangle } from 'lucide-react'
import { optionsApi } from '../api/endpoints'

interface Props {
  strategyId: string
  underlying: string
  spot?: number
}

// Module-level counter — each new OptionsStrikePreview gets the next slot,
// staggering its fetch by N * 600ms so 6 cards don't all hit Polygon at once.
let __previewLoadIndex = 0

export default function OptionsStrikePreview({ strategyId, underlying, spot }: Props) {
  const [enabled, setEnabled] = useState(false)
  useEffect(() => {
    const myIndex = __previewLoadIndex++
    const t = window.setTimeout(() => setEnabled(true), myIndex * 600)
    return () => window.clearTimeout(t)
  }, [])

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['options-preview', strategyId, underlying, spot],
    queryFn: () => optionsApi.previewStrike(strategyId, underlying, spot).then(r => r.data),
    staleTime: 10 * 60_000,   // 10 min — chain barely changes intraday
    gcTime: 30 * 60_000,
    retry: false,
    enabled,
  })

  if (isLoading) {
    return (
      <div className="bg-violet-50 dark:bg-violet-900/20 border border-violet-200 dark:border-violet-900/40 rounded-xl p-3 text-xs text-violet-700 dark:text-violet-300">
        Loading current strike pick…
      </div>
    )
  }

  if (error) {
    const detail = (error as any)?.response?.data?.detail || 'Could not pull chain. Polygon may be rate-limited.'
    return (
      <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-900/40 rounded-xl p-3 text-xs text-amber-800 dark:text-amber-200 flex items-start gap-2">
        <AlertTriangle size={13} className="flex-shrink-0 mt-0.5"/>
        <div>
          <div className="font-bold">Preview unavailable</div>
          <div className="opacity-80 mt-0.5">{detail}</div>
          <button onClick={() => refetch()} className="text-[10px] font-semibold underline mt-1">retry</button>
        </div>
      </div>
    )
  }

  if (!data) return null
  const p = data.pick.long
  const isCall = p.right === 'call'
  const Arrow = isCall ? TrendingUp : TrendingDown
  const tone  = isCall ? 'text-green-700 dark:text-green-300' : 'text-red-700 dark:text-red-300'
  const bg    = isCall ? 'bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-900/40'
                        : 'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-900/40'

  return (
    <div className={`rounded-xl border p-3 ${bg}`}>
      <div className="flex items-center justify-between gap-2 mb-2">
        <div className="flex items-center gap-1.5">
          <Arrow size={13} className={tone}/>
          <span className={`text-[11px] font-bold uppercase tracking-wider ${tone}`}>
            {isCall ? 'Long Call' : 'Long Put'}
          </span>
          {data.pick.band_missed && (
            <span className="text-[9px] font-bold px-1.5 py-0.5 rounded bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
              BAND MISSED
            </span>
          )}
        </div>
        <span className="text-[10px] text-slate-500 dark:text-slate-400">
          {data.pick.days_to_expiration}d · spot ${data.spot.toLocaleString()}
        </span>
      </div>

      <div className="font-mono text-xs text-slate-900 dark:text-slate-100 mb-2 truncate" title={p.ticker}>
        {data.underlying} {p.strike} {isCall ? 'CALL' : 'PUT'} · {p.expiration}
      </div>

      <div className="grid grid-cols-4 gap-1.5 text-[10px]">
        <Stat label="Premium" value={`$${p.theoretical_premium.toFixed(2)}`}/>
        <Stat label="Cost" value={`$${p.cost_per_contract_usd.toLocaleString()}`}/>
        <Stat label="Δ" value={p.delta.toFixed(2)}/>
        <Stat label="Θ/day" value={p.theta.toFixed(2)}/>
      </div>

      {data.pick.short && (
        <div className="mt-2 pt-2 border-t border-slate-200/60 dark:border-slate-700/60 text-[10px] text-slate-600 dark:text-slate-300">
          Short leg: <span className="font-semibold">{data.pick.short.strike} {data.pick.short.right === 'call' ? 'C' : 'P'}</span> @ ${data.pick.short.theoretical_premium.toFixed(2)} (Δ {data.pick.short.delta.toFixed(2)})
        </div>
      )}
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-white/60 dark:bg-slate-800/40 rounded-md px-1.5 py-1">
      <div className="text-slate-500 dark:text-slate-400 leading-none mb-0.5">{label}</div>
      <div className="font-bold text-slate-900 dark:text-slate-100 tabular-nums">{value}</div>
    </div>
  )
}
