/**
 * Options Sessions list — every paper/live options session the user has run.
 *
 * Active sessions are pinned to the top with a "live" badge and the
 * configured underlyings/mode. Historical sessions follow below, sorted by
 * start time descending. Click any row to drill into the detail page.
 */
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Play, Square, TrendingUp, TrendingDown, Activity, ArrowLeft } from 'lucide-react'
import { optionsApi, type OptionsSession } from '../api/endpoints'

function PnlBadge({ value }: { value: number }) {
  const up = value > 0
  const down = value < 0
  const tone = up
    ? 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300'
    : down
      ? 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300'
      : 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300'
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-bold tabular-nums ${tone}`}>
      {up && <TrendingUp size={11}/>}
      {down && <TrendingDown size={11}/>}
      {value >= 0 ? '+' : ''}${value.toFixed(2)}
    </span>
  )
}

function SessionRow({ s, onStop }: { s: OptionsSession; onStop: (id: string) => void }) {
  const mode = s.label?.split(':')[1] || 'paper'  // label format: "options:<mode>:<underlyings>"
  return (
    <Link to={`/app/options/sessions/${s.session_id}`}
      className="group block bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-700 p-4 hover:border-blue-300 hover:shadow-md transition-all">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0 flex-1">
          <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-md text-[10px] font-bold uppercase tracking-wider ${
            s.is_active
              ? 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300'
              : 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400'
          }`}>
            {s.is_active && <Activity size={10}/>}
            {s.is_active ? 'Live' : 'Stopped'}
          </span>
          <span className={`text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${
            mode === 'live'
              ? 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300'
              : 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
          }`}>
            {mode}
          </span>
          <div className="min-w-0 flex-1">
            <div className="font-semibold text-slate-900 dark:text-slate-100 text-sm truncate">
              {s.underlyings.join(', ')}
            </div>
            <div className="text-[11px] text-slate-500 dark:text-slate-400">
              {s.total_trades} trade{s.total_trades === 1 ? '' : 's'}
              {s.started_at && ` · started ${new Date(s.started_at).toLocaleString()}`}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <PnlBadge value={s.net_pnl}/>
          {s.is_active && (
            <button onClick={(e) => { e.preventDefault(); e.stopPropagation(); onStop(s.session_id) }}
              className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-[10px] font-bold bg-red-600 hover:bg-red-700 text-white">
              <Square size={9} fill="white"/> Stop
            </button>
          )}
        </div>
      </div>
    </Link>
  )
}

export default function OptionsSessions() {
  const qc = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ['options-sessions'],
    queryFn: () => optionsApi.listSessions().then(r => r.data),
    refetchInterval: 5_000,
  })

  const stopMutation = useMutation({
    mutationFn: (id: string) => optionsApi.stopSession(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['options-sessions'] }),
  })

  const sessions = data?.sessions || []
  const active     = sessions.filter(s => s.is_active)
  const historical = sessions.filter(s => !s.is_active)

  return (
    <div className="p-6 max-w-5xl">
      <Link to="/app/options" className="inline-flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200 mb-3">
        <ArrowLeft size={12}/> Options home
      </Link>
      <h1 className="text-2xl font-extrabold text-slate-900 dark:text-slate-100 mb-1">Options Sessions</h1>
      <p className="text-sm text-slate-500 dark:text-slate-400 mb-6">
        Paper and live options strategies — start one from the Strategy Builder card, monitor here.
      </p>

      {isLoading ? (
        <div className="space-y-3">
          {[1,2,3].map(i => <div key={i} className="h-20 bg-slate-100 dark:bg-slate-800 rounded-xl animate-pulse"/>)}
        </div>
      ) : sessions.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-slate-300 dark:border-slate-700 p-12 text-center">
          <Play size={36} className="mx-auto text-slate-300 dark:text-slate-600 mb-3"/>
          <p className="font-semibold text-slate-500 dark:text-slate-400 mb-1">No sessions yet</p>
          <p className="text-xs text-slate-400 dark:text-slate-500 max-w-sm mx-auto">
            Go to <Link to="/app/strategies" className="text-blue-600 underline">Strategies</Link> → switch to the Options tab → click <strong>Activate</strong> on any options strategy to start your first session.
          </p>
        </div>
      ) : (
        <>
          {active.length > 0 && (
            <div className="mb-6">
              <h2 className="text-[10px] font-bold uppercase tracking-widest text-slate-500 dark:text-slate-400 mb-2">Active</h2>
              <div className="space-y-2">
                {active.map(s => <SessionRow key={s.session_id} s={s} onStop={(id) => stopMutation.mutate(id)}/>)}
              </div>
            </div>
          )}
          {historical.length > 0 && (
            <div>
              <h2 className="text-[10px] font-bold uppercase tracking-widest text-slate-500 dark:text-slate-400 mb-2">History</h2>
              <div className="space-y-2">
                {historical.map(s => <SessionRow key={s.session_id} s={s} onStop={() => {}}/>)}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
