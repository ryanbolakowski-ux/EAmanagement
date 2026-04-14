import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { liveTradingApi, strategiesApi, tradesApi } from '../api/endpoints'
import { useState } from 'react'
import { Zap, AlertTriangle, X, ShieldAlert, ServerCrash, Activity, Plus, Link2 } from 'lucide-react'

export default function LiveTrading() {
  const qc = useQueryClient()
  const [tab, setTab] = useState<'accounts' | 'trades'>('accounts')
  const [showAddAccount, setShowAddAccount] = useState(false)
  const [showStartSession, setShowStartSession] = useState(false)
  const [accountForm, setAccountForm] = useState({
    account_name: '', is_demo: true,
    credentials: { username: '', password: '', app_id: '', cid: '', sec: '' },
  })
  const [sessionForm, setSessionForm] = useState({ strategy_id: '', broker_account_id: '', instrument: 'ES' })
  const [killConfirm, setKillConfirm] = useState<string | null>(null)

  const { data: accounts = [] }   = useQuery({ queryKey: ['broker-accounts'], queryFn: () => liveTradingApi.listAccounts().then(r => r.data) })
  const { data: strategies = [] } = useQuery({ queryKey: ['strategies'],      queryFn: () => strategiesApi.list().then(r => r.data) })
  const { data: liveTrades = [] } = useQuery({ queryKey: ['live-trades'],     queryFn: () => tradesApi.list({ mode: 'live', limit: 50 }).then(r => r.data) })

  const addAccountMutation = useMutation({
    mutationFn: () => liveTradingApi.addAccount({ ...accountForm, broker: 'tradovate' }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['broker-accounts'] }); setShowAddAccount(false) },
  })

  const startSessionMutation = useMutation({
    mutationFn: () => liveTradingApi.startSession(sessionForm),
    onSuccess: () => { qc.invalidateQueries({}); setShowStartSession(false) },
  })

  const killMutation = useMutation({
    mutationFn: (id: string) => liveTradingApi.killSwitch(id),
    onSuccess: () => { qc.invalidateQueries({}); setKillConfirm(null) },
  })

  const activeAccount = accounts.find((a: any) => a.is_active)

  return (
    <div className="p-8 max-w-6xl">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-extrabold text-slate-900">Live Trading</h1>
          <p className="text-slate-500 text-sm mt-1">Real-money automated execution via Tradovate</p>
        </div>
        <div className="flex gap-2.5">
          <button onClick={() => setShowAddAccount(true)}
            className="flex items-center gap-2 border border-slate-200 text-slate-700 hover:bg-slate-50 px-4 py-2.5 rounded-xl text-sm font-medium transition-colors">
            <Plus size={14}/> Add Account
          </button>
          <button onClick={() => setShowStartSession(true)}
            className="flex items-center gap-2 bg-rose-600 hover:bg-rose-700 text-white px-4 py-2.5 rounded-xl text-sm font-semibold transition-colors shadow-sm shadow-rose-100">
            <Zap size={14}/> Deploy Strategy
          </button>
        </div>
      </div>

      {/* Warning banner */}
      <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 mb-6 flex items-start gap-3">
        <AlertTriangle size={16} className="text-amber-500 flex-shrink-0 mt-0.5"/>
        <p className="text-xs text-amber-700 leading-relaxed">
          <span className="font-semibold">Live trading uses real money.</span> Ensure your strategy has been thoroughly backtested and paper traded before deploying.
          Always set a daily loss limit and monitor positions actively. Trading futures involves substantial risk of loss.
        </p>
      </div>

      {/* Active connection indicator */}
      {activeAccount && (
        <div className="bg-green-50 border border-green-200 rounded-xl p-4 mb-6 flex items-center gap-3">
          <div className="w-2.5 h-2.5 rounded-full bg-green-500 animate-pulse flex-shrink-0"/>
          <div>
            <div className="font-semibold text-green-800 text-sm">Connected to {activeAccount.account_name}</div>
            <div className="text-xs text-green-600 mt-0.5">
              {activeAccount.broker} · {activeAccount.is_demo ? 'Demo environment' : 'Live environment'}
            </div>
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-1 border-b border-slate-200 mb-6">
        {(['accounts', 'trades'] as const).map(t => (
          <button key={t} onClick={() => setTab(t)}
            className={`px-4 py-2.5 text-sm font-semibold capitalize border-b-2 transition-colors ${
              tab === t
                ? 'border-blue-600 text-blue-600'
                : 'border-transparent text-slate-500 hover:text-slate-800'
            }`}>
            {t === 'accounts' ? 'Broker Accounts' : 'Trade History'}
          </button>
        ))}
      </div>

      {/* Accounts tab */}
      {tab === 'accounts' && (
        <div className="space-y-3">
          {accounts.length === 0 ? (
            <div className="bg-white rounded-2xl border border-dashed border-slate-200 p-14 text-center">
              <div className="w-14 h-14 bg-rose-50 rounded-2xl flex items-center justify-center mx-auto mb-5">
                <Link2 size={24} className="text-rose-500"/>
              </div>
              <p className="font-semibold text-slate-700 mb-1">No broker accounts connected</p>
              <p className="text-sm text-slate-400 mb-5">Connect your Tradovate account to start live trading</p>
              <button onClick={() => setShowAddAccount(true)}
                className="inline-flex items-center gap-2 bg-rose-600 hover:bg-rose-700 text-white px-4 py-2.5 rounded-xl text-sm font-semibold transition-colors">
                <Plus size={14}/> Connect Account
              </button>
            </div>
          ) : (
            accounts.map((a: any) => (
              <div key={a.id} className="bg-white rounded-xl border border-slate-200 p-5 flex items-center justify-between hover:border-slate-300 hover:shadow-sm transition-all">
                <div className="flex items-center gap-4">
                  <div className="w-10 h-10 bg-rose-50 rounded-xl flex items-center justify-center flex-shrink-0">
                    <Zap size={18} className="text-rose-600"/>
                  </div>
                  <div>
                    <div className="font-semibold text-slate-900 text-sm">{a.account_name}</div>
                    <div className="text-xs text-slate-400 mt-0.5">{a.broker} · {a.is_demo ? 'Demo' : 'Live'}</div>
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <span className={`badge ${a.is_active ? 'badge-green' : 'badge-grey'}`}>
                    {a.is_active ? 'Connected' : 'Inactive'}
                  </span>
                  {a.is_active && (
                    <button onClick={() => setKillConfirm(a.id)}
                      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border border-red-200 text-red-600 hover:bg-red-50 transition-colors">
                      <ShieldAlert size={12}/> Kill Switch
                    </button>
                  )}
                </div>
              </div>
            ))
          )}
        </div>
      )}

      {/* Trades tab */}
      {tab === 'trades' && (
        <div className="bg-white rounded-2xl border border-slate-200 overflow-hidden shadow-sm">
          {liveTrades.length === 0 ? (
            <div className="p-14 text-center">
              <Activity size={32} className="mx-auto text-slate-200 mb-3"/>
              <p className="text-sm font-medium text-slate-400">No live trades yet</p>
              <p className="text-xs text-slate-300 mt-1">Deploy a strategy to begin live execution</p>
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
                {liveTrades.map((t: any) => (
                  <tr key={t.id} className="hover:bg-slate-50 transition-colors">
                    <td className="px-4 py-3.5 font-semibold text-slate-900">{t.instrument}</td>
                    <td className="px-4 py-3.5">
                      <span className={`badge ${t.direction === 'long' ? 'badge-green' : 'badge-red'}`}>
                        {t.direction.toUpperCase()}
                      </span>
                    </td>
                    <td className="px-4 py-3.5 text-slate-600 font-medium">{t.entry_price?.toFixed(2) ?? '—'}</td>
                    <td className="px-4 py-3.5 text-slate-600 font-medium">{t.exit_price?.toFixed(2) ?? 'Open'}</td>
                    <td className="px-4 py-3.5 text-slate-400">{t.stop_loss?.toFixed(2) ?? '—'}</td>
                    <td className="px-4 py-3.5 text-slate-400">{t.take_profit?.toFixed(2) ?? '—'}</td>
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
      )}

      {/* Kill switch confirm modal */}
      {killConfirm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm">
            <div className="p-6 text-center">
              <div className="w-14 h-14 bg-red-50 rounded-2xl flex items-center justify-center mx-auto mb-4">
                <ServerCrash size={26} className="text-red-500"/>
              </div>
              <h2 className="text-lg font-extrabold text-slate-900 mb-2">Trigger Kill Switch?</h2>
              <p className="text-sm text-slate-500 mb-6 leading-relaxed">
                This will immediately halt all trading activity, cancel all open orders, and close any open positions for this session.
              </p>
              <div className="flex gap-3">
                <button onClick={() => setKillConfirm(null)} className="flex-1 border border-slate-200 text-slate-600 py-2.5 rounded-xl text-sm font-medium">Cancel</button>
                <button onClick={() => killMutation.mutate(killConfirm)} disabled={killMutation.isPending}
                  className="flex-1 bg-red-600 hover:bg-red-700 disabled:opacity-50 text-white py-2.5 rounded-xl text-sm font-bold transition-colors">
                  {killMutation.isPending ? 'Stopping...' : 'Confirm Kill Switch'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Add account modal */}
      {showAddAccount && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md max-h-[90vh] flex flex-col">
            <div className="flex items-center justify-between px-6 py-5 border-b border-slate-100">
              <h2 className="text-base font-bold text-slate-900">Connect Tradovate Account</h2>
              <button onClick={() => setShowAddAccount(false)} className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400"><X size={16}/></button>
            </div>
            <div className="px-6 py-5 space-y-4 overflow-y-auto flex-1">
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Account Name</label>
                <input value={accountForm.account_name} onChange={e => setAccountForm({ ...accountForm, account_name: e.target.value })}
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  placeholder="e.g. Tradovate Main"/>
              </div>

              <div className="border-t border-slate-100 pt-4">
                <div className="text-xs font-semibold text-slate-600 uppercase tracking-wider mb-3">Tradovate Credentials</div>
                <div className="space-y-3">
                  {([
                    { key: 'username', label: 'Username', type: 'text' },
                    { key: 'password', label: 'Password', type: 'password' },
                    { key: 'app_id',   label: 'App ID',   type: 'text' },
                    { key: 'cid',      label: 'CID',      type: 'text' },
                    { key: 'sec',      label: 'Secret',   type: 'password' },
                  ] as const).map(({ key, label, type }) => (
                    <div key={key}>
                      <label className="text-xs font-medium text-slate-500 block mb-1">{label}</label>
                      <input type={type} value={accountForm.credentials[key]}
                        onChange={e => setAccountForm({ ...accountForm, credentials: { ...accountForm.credentials, [key]: e.target.value } })}
                        className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"/>
                    </div>
                  ))}
                </div>
              </div>

              <div className="bg-slate-50 rounded-xl p-3.5 flex items-center gap-3">
                <input type="checkbox" id="is_demo" checked={accountForm.is_demo}
                  onChange={e => setAccountForm({ ...accountForm, is_demo: e.target.checked })}
                  className="w-4 h-4 rounded text-blue-600"/>
                <label htmlFor="is_demo" className="text-sm font-medium text-slate-700 cursor-pointer">
                  Use demo/simulator environment
                </label>
              </div>

              <div className="bg-blue-50 border border-blue-100 rounded-xl p-3 text-xs text-blue-700">
                Your credentials are encrypted with AES-256 before storage and are never logged or transmitted in plaintext.
              </div>
            </div>
            <div className="flex gap-3 px-6 py-4 border-t border-slate-100">
              <button onClick={() => setShowAddAccount(false)} className="flex-1 border border-slate-200 text-slate-600 py-2.5 rounded-xl text-sm font-medium">Cancel</button>
              <button onClick={() => addAccountMutation.mutate()} disabled={!accountForm.account_name || !accountForm.credentials.username || addAccountMutation.isPending}
                className="flex-1 bg-rose-600 hover:bg-rose-700 disabled:opacity-50 text-white py-2.5 rounded-xl text-sm font-semibold transition-colors">
                {addAccountMutation.isPending ? 'Connecting...' : 'Connect Account'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Deploy strategy modal */}
      {showStartSession && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm">
            <div className="flex items-center justify-between px-6 py-5 border-b border-slate-100">
              <h2 className="text-base font-bold text-slate-900">Deploy Strategy Live</h2>
              <button onClick={() => setShowStartSession(false)} className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400"><X size={16}/></button>
            </div>
            <div className="px-6 py-5 space-y-4">
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Strategy</label>
                <select value={sessionForm.strategy_id} onChange={e => setSessionForm({...sessionForm, strategy_id: e.target.value})}
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                  <option value="">Select a strategy...</option>
                  {strategies.map((s: any) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Broker Account</label>
                <select value={sessionForm.broker_account_id} onChange={e => setSessionForm({...sessionForm, broker_account_id: e.target.value})}
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                  <option value="">Select an account...</option>
                  {accounts.map((a: any) => <option key={a.id} value={a.id}>{a.account_name}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5">Instrument</label>
                <select value={sessionForm.instrument} onChange={e => setSessionForm({...sessionForm, instrument: e.target.value})}
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                  {['ES', 'NQ', 'RTY', 'YM'].map(i => <option key={i}>{i}</option>)}
                </select>
              </div>
              <div className="bg-red-50 border border-red-100 rounded-xl p-3 text-xs text-red-700">
                <span className="font-semibold">Warning:</span> This will execute real trades with real money. Confirm your strategy is fully tested.
              </div>
            </div>
            <div className="flex gap-3 px-6 py-4 border-t border-slate-100">
              <button onClick={() => setShowStartSession(false)} className="flex-1 border border-slate-200 text-slate-600 py-2.5 rounded-xl text-sm font-medium">Cancel</button>
              <button onClick={() => startSessionMutation.mutate()} disabled={!sessionForm.strategy_id || !sessionForm.broker_account_id || startSessionMutation.isPending}
                className="flex-1 bg-rose-600 hover:bg-rose-700 disabled:opacity-50 text-white py-2.5 rounded-xl text-sm font-semibold transition-colors">
                {startSessionMutation.isPending ? 'Deploying...' : 'Deploy Live'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
