import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { paperTradingApi, strategiesApi, tradesApi } from '../api/endpoints'
import { useState } from 'react'
import { PlayCircle, StopCircle, X, Activity } from 'lucide-react'

export default function PaperTrading() {
  const qc = useQueryClient()
  const [showStart, setShowStart] = useState(false)
  const [form, setForm] = useState({ strategy_id: '', instrument: 'ES', daily_loss_limit: '' })

  const { data: sessions = [] }   = useQuery({ queryKey: ['paper-sessions'], queryFn: () => paperTradingApi.listSessions().then(r => r.data) })
  const { data: strategies = [] } = useQuery({ queryKey: ['strategies'], queryFn: () => strategiesApi.list().then(r => r.data) })
  const { data: trades = [] }     = useQuery({ queryKey: ['paper-trades'], queryFn: () => tradesApi.list({ mode: 'paper', limit: 50 }).then(r => r.data) })

  const startMutation = useMutation({
    mutationFn: () => paperTradingApi.startSession({ strategy_id: form.strategy_id, instrument: form.instrument, daily_loss_limit: form.daily_loss_limit ? parseFloat(form.daily_loss_limit) : undefined }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['paper-sessions'] }); setShowStart(false) },
  })

  const stopMutation = useMutation({
    mutationFn: (id: string) => paperTradingApi.stopSession(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['paper-sessions'] }),
  })

  const activeSession = sessions.find((s: any) => s.is_active)

  // Stats
  const completedTrades = trades.filter((t: any) => t.status === 'closed')
  const totalPnl  = completedTrades.reduce((acc: number, t: any) => acc + (t.net_pnl ?? 0), 0)
  const wins      = completedTrades.filter((t: any) => (t.net_pnl ?? 0) > 0).length
  const winRate   = completedTrades.length > 0 ? (wins / completedTrades.length * 100).toFixed(1) : '—'

  return (
    <div className="p-8 max-w-6xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-extrabold text-slate-900">Paper Trading</h1>
          <p className="text-slate-500 text-sm mt-1">Simulate live trading with real-time market data, zero risk</p>
        </div>
        {!activeSession ? (
          <button onClick={() => setShowStart(true)}
            className="flex items-center gap-2 bg-green-600 hover:bg-green-700 text-white px-4 py-2.5 rounded-xl text-sm font-semibold transition-colors shadow-sm shadow-green-100">
            <PlayCircle size={15}/> Start Session
          </button>
        ) : (
          <button onClick={() => stopMutation.mutate(activeSession.id)}
            className="flex items-center gap-2 bg-red-50 hover:bg-red-100 text-red-600 border border-red-200 px-4 py-2.5 rounded-xl text-sm font-semibold transition-colors">
            <StopCircle size={15}/> Stop Session
          </button>
        )}
      </div>

      {/* Stats bar */}
      <div className="grid grid-cols-3 gap-4 mb-6">
        <div className="bg-white rounded-xl border border-slate-200 p-4">
          <div className="text-xs text-slate-400 uppercase tracking-wider font-medium mb-1.5">Total Trades</div>
          <div className="text-2xl font-extrabold text-slate-900">{completedTrades.length}</div>
        </div>
        <div className="bg-white rounded-xl border border-slate-200 p-4">
          <div className="text-xs text-slate-400 uppercase tracking-wider font-medium mb-1.5">Win Rate</div>
          <div className={`text-2xl font-extrabold ${completedTrades.length > 0 && wins/completedTrades.length >= 0.5 ? 'text-green-600' : 'text-slate-900'}`}>{winRate}{completedTrades.length > 0 ? '%' : ''}</div>
        </div>
        <div className="bg-white rounded-xl border border-slate-200 p-4">
          <div className="text-xs text-slate-400 uppercase tracking-wider font-medium mb-1.5">Net P&L</div>
          <div className={`text-2xl font-extrabold ${totalPnl >= 0 ? 'text-green-600' : 'text-red-500'}`}>
            {totalPnl >= 0 ? '+' : ''}${totalPnl.toLocaleString('en-US', { minimumFractionDigits: 2 })}
          </div>
        </div>
      </div>

      {/* Active session banner */}
      {activeSession && (
        <div className="bg-green-50 border border-green-200 rounded-xl p-4 mb-6 flex items-center gap-3">
          <div className="w-2.5 h-2.5 rounded-full bg-green-500 animate-pulse flex-shrink-0"/>
          <div>
            <div className="font-semibold text-green-800 text-sm">Session active</div>
            <div className="text-xs text-green-600 mt-0.5">{activeSession.total_trades} trades executed · Net P&L: ${activeSession.net_pnl.toFixed(2)}</div>
          </div>
        </div>
      )}

      {/* Trade history */}
      <h2 className="text-base font-bold text-slate-900 mb-3">Trade History</h2>
      <div className="bg-white rounded-2xl border border-slate-200 overflow-hidden shadow-sm">
        {trades.length === 0 ? (
          <div className="p-14 text-center">
            <Activity size={32} className="mx-auto text-slate-200 mb-3"/>
            <p className="text-sm font-medium text-slate-400">No paper trades yet</p>
            <p className="text-xs text-slate-300 mt-1">Start a session to begin simulated trading</p>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-slate-50 border-b border-slate-200">
                {['Instrument', 'Direction', 'Entry', 'Exit', 'Stop Loss', 'Take Profit', 'Net P&L', 'Exit Reason', 'Status'].map(h => (
                  <th key={h} className="px-4 py-3 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider whitespace-nowrap">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {trades.map((t: any) => (
                <tr key={t.id} className="hover:bg-slate-50 transition-colors">
                  <td className="px-4 py-3.5 font-semibold text-slate-900">{t.instrument}</td>
                  <td className="px-4 py-3.5">
                    <span className={`badge ${t.direction === 'long' ? 'badge-green' : 'badge-red'}`}>
                      {t.direction.toUpperCase()}
                    </span>
                  </td>
                  <td className="px-4 py-3.5 text-slate-600 font-medium">{t.entry_price?.toFixed(2) ?? '—'}</td>
                  <td className="px-4 py-3.5 text-slate-600 font-medium">{t.exit_price?.toFixed(2) ?? 'Open'}</td>
                  <td className="px-4 py-3.5 text-slate-400">{t.stop_loss.toFixed(2)}</td>
                  <td className="px-4 py-3.5 text-slate-400">{t.take_profit.toFixed(2)}</td>
                  <td className={`px-4 py-3.5 font-bold ${(t.net_pnl ?? 0) >= 0 ? 'text-green-600' : 'text-red-500'}`}>
                    {t.net_pnl != null ? `${t.net_pnl >= 0 ? '+' : ''}$${t.net_pnl.toFixed(2)}` : '—'}
                  </td>
                  <td className="px-4 py-3.5 text-slate-400 text-xs">{t.exit_reason ?? '—'}</td>
                  <td className="px-4 py-3.5">
                    <span className={`badge ${t.status === 'closed' ? 'badge-grey' : t.status === 'open' ? 'badge-blue' : 'badge-amber'}`}>
                      {t.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Start modal */}
      {showStart && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm">
            <div className="flex items-center justify-between px-6 py-5 border-b border-slate-100">
              <h2 className="text-base font-bold text-slate-900">Start Paper Session</h2>
              <button onClick={() => setShowStart(false)} className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400"><X size={16}/></button>
            </div>
            <div className="px-6 py-5 space-y-4">
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Strategy</label>
                <select value={form.strategy_id} onChange={e => setForm({...form, strategy_id: e.target.value})}
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                  <option value="">Select a strategy...</option>
                  {strategies.map((s: any) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Daily Loss Limit ($) — optional</label>
                <input type="number" value={form.daily_loss_limit} onChange={e => setForm({...form, daily_loss_limit: e.target.value})}
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" placeholder="e.g. 500"/>
              </div>
            </div>
            <div className="flex gap-3 px-6 py-4 border-t border-slate-100">
              <button onClick={() => setShowStart(false)} className="flex-1 border border-slate-200 text-slate-600 py-2.5 rounded-xl text-sm font-medium">Cancel</button>
              <button onClick={() => startMutation.mutate()} disabled={!form.strategy_id || startMutation.isPending}
                className="flex-1 bg-green-600 hover:bg-green-700 disabled:opacity-50 text-white py-2.5 rounded-xl text-sm font-semibold transition-colors">
                Start Session
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
