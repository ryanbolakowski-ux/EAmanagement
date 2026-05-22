/* Self-contained Add Broker modal — usable from any page.
   Props: { open, onClose, onSuccess }. Owns all its own state. */
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { X, ShieldCheck, AlertCircle, CheckCircle2 } from 'lucide-react'
import { liveTradingApi } from '../api/endpoints'

interface Props {
  open: boolean
  onClose: () => void
  onSuccess?: () => void
}

const BROKERS = [
  {
    slug: 'alpaca', name: 'Alpaca', category: 'multi_asset',
    status: 'available',
    tagline: 'Stocks · options · crypto. Free API, paper + live in one place.',
    fields: [
      { key: 'api_key', label: 'API Key ID', type: 'text',
        hint: 'Alpaca dashboard → API Keys → Key ID (paper or live).' },
      { key: 'api_secret', label: 'API Secret', type: 'password',
        hint: 'Shown only once when you generate the key — store securely.' },
    ],
    helpUrl: 'https://alpaca.markets/docs/api-references/trading-api/',
  },
  {
    slug: 'tradier', name: 'Tradier', category: 'multi_asset',
    status: 'available',
    tagline: 'Stocks · options. Sandbox + live in one platform.',
    fields: [
      { key: 'access_token', label: 'Access Token', type: 'password',
        hint: 'Tradier → Settings → API Access → Sandbox or Production token.' },
      { key: 'account_id',  label: 'Account ID (optional)', type: 'text',
        hint: 'Leave blank to auto-resolve from your token. Format: VA-XXXXXXXX.' },
    ],
    helpUrl: 'https://documentation.tradier.com',
  },
  {
    slug: 'tradovate', name: 'Tradovate', category: 'futures',
    status: 'available',
    tagline: 'Futures — ES/NQ/RTY/YM. Most prop firms use this.',
    fields: [
      { key: 'username', label: 'Username',  type: 'text',     hint: 'Your Tradovate login email.' },
      { key: 'password', label: 'Password',  type: 'password', hint: 'Stored encrypted; never logged.' },
      { key: 'cid',      label: 'CID',       type: 'text',     hint: 'API Access → Application ID.' },
      { key: 'sec',      label: 'Secret',    type: 'password', hint: 'API Access → Secret key.' },
    ],
    helpUrl: 'https://api.tradovate.com',
  },
] as const

export default function AddBrokerInline({ open, onClose, onSuccess }: Props) {
  const qc = useQueryClient()
  const [selected, setSelected] = useState<string | null>(null)
  const [accountName, setAccountName] = useState('')
  const [isDemo, setIsDemo] = useState(true)
  const [creds, setCreds] = useState<Record<string, string>>({})
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null)
  const [error, setError] = useState<string | null>(null)

  const broker = BROKERS.find(b => b.slug === selected)

  const testMutation = useMutation({
    mutationFn: () => liveTradingApi.testConnection({
      broker: selected!,
      is_demo: isDemo,
      credentials: creds,
    }),
    onSuccess: (r: any) => {
      const env = r.data.environment === 'demo' ? '(Sandbox)' : '(Live)'
      setTestResult({ ok: true, msg: `Connected to ${broker?.name} ${env}.` })
    },
    onError: (e: any) => setTestResult({ ok: false, msg: e?.response?.data?.detail || 'Connection test failed.' }),
  })

  const addMutation = useMutation({
    mutationFn: () => liveTradingApi.addAccount({
      account_name: accountName,
      broker: selected!,
      is_demo: isDemo,
      credentials: creds,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['broker-accounts'] })
      qc.invalidateQueries({ queryKey: ['portfolio-summary'] })
      reset()
      onSuccess?.()
      onClose()
    },
    onError: (e: any) => setError(e?.response?.data?.detail || 'Failed to add account.'),
  })

  function reset() {
    setSelected(null); setAccountName(''); setIsDemo(true)
    setCreds({}); setTestResult(null); setError(null)
  }

  if (!open) return null

  return (
    <div className="fixed inset-0 z-[100] bg-black/70 flex items-center justify-center p-4" onClick={() => { reset(); onClose() }}>
      <div onClick={e => e.stopPropagation()} className="bg-white dark:bg-slate-900 rounded-2xl shadow-2xl w-full max-w-2xl max-h-[90vh] flex flex-col">
        <div className="px-6 py-4 border-b border-slate-200 dark:border-slate-700 flex items-center justify-between">
          <div className="flex items-center gap-3 min-w-0">
            {broker && (
              <button onClick={() => { setSelected(null); setTestResult(null); setError(null) }}
                className="text-slate-400 hover:text-slate-700 dark:hover:text-slate-200 text-sm font-medium flex-shrink-0">← Back</button>
            )}
            <div>
              <div className="text-[10px] uppercase tracking-[0.2em] text-violet-500 dark:text-violet-400 font-bold mb-0.5">
                {broker ? 'Step 2 · credentials' : 'Step 1 · pick broker'}
              </div>
              <h2 className="text-lg font-extrabold text-slate-900 dark:text-slate-100">
                {broker ? `Connect ${broker.name}` : 'Add a broker account'}
              </h2>
            </div>
          </div>
          <button onClick={() => { reset(); onClose() }} className="p-1.5 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-400">
            <X size={18}/>
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-5">
          {!broker ? (
            <div className="space-y-3">
              <p className="text-sm text-slate-500 dark:text-slate-400">
                Pick the broker where your account lives. Credentials are AES-256 encrypted before storage; they're never logged or sent anywhere else.
              </p>
              <div className="grid sm:grid-cols-2 gap-3">
                {BROKERS.map(b => (
                  <button key={b.slug} onClick={() => setSelected(b.slug)}
                    className="text-left rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4 hover:border-violet-400 dark:hover:border-violet-600 hover:shadow-md transition-all">
                    <div className="flex items-center gap-2 mb-1">
                      <span className="font-extrabold text-slate-900 dark:text-slate-100">{b.name}</span>
                      <span className="text-[9px] font-bold uppercase tracking-wider text-emerald-700 bg-emerald-100 dark:text-emerald-300 dark:bg-emerald-900/40 px-1.5 py-0.5 rounded">Available</span>
                    </div>
                    <div className="text-xs text-slate-500 dark:text-slate-400">{b.tagline}</div>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="space-y-4">
              <div>
                <label className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 block mb-1.5">Nickname</label>
                <input value={accountName} onChange={e => setAccountName(e.target.value)}
                  placeholder={`${broker.name} Main`}
                  className="w-full border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-2 text-sm bg-white dark:bg-slate-800"/>
              </div>

              <div className="border-t border-slate-100 dark:border-slate-800 pt-4">
                <div className="flex items-center justify-between mb-2">
                  <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">{broker.name} credentials</div>
                  {broker.helpUrl && (
                    <a href={broker.helpUrl} target="_blank" rel="noopener noreferrer" className="text-[11px] font-bold text-violet-600 dark:text-violet-400 hover:underline">Where do I find these? ↗</a>
                  )}
                </div>
                <div className="space-y-3">
                  {broker.fields.map(({ key, label, type, hint }) => (
                    <div key={key}>
                      <label className="text-[11px] font-semibold text-slate-700 dark:text-slate-300 block mb-1">{label}</label>
                      <input type={type as string} value={creds[key] ?? ''} autoComplete="off"
                        onChange={e => { setCreds({ ...creds, [key]: e.target.value }); setTestResult(null); setError(null) }}
                        className="w-full border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-2 text-sm bg-white dark:bg-slate-800"/>
                      <p className="text-[10.5px] text-slate-400 dark:text-slate-500 mt-1">{hint}</p>
                    </div>
                  ))}
                </div>
              </div>

              <label className="flex items-start gap-3 rounded-xl border border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-900/20 p-3.5 cursor-pointer">
                <input type="checkbox" checked={isDemo} onChange={e => setIsDemo(e.target.checked)}
                  className="w-4 h-4 mt-0.5 rounded text-amber-600"/>
                <div className="flex-1">
                  <div className="text-sm font-bold text-amber-900 dark:text-amber-200">Sandbox / demo mode</div>
                  <div className="text-[11px] text-amber-700 dark:text-amber-300 mt-0.5">
                    Recommended for testing. Simulated fills, real market data, no real money. Toggle off later from the broker card.
                  </div>
                </div>
              </label>

              <div className="bg-violet-50 dark:bg-violet-900/20 border border-violet-200 dark:border-violet-900 rounded-xl p-3 flex items-start gap-2.5 text-xs text-violet-700 dark:text-violet-300">
                <ShieldCheck size={14} className="flex-shrink-0 mt-0.5"/>
                <span>Credentials are encrypted with AES-256 before being stored. Never logged. Never transmitted in plaintext.</span>
              </div>

              {testResult && (
                <div className={`rounded-xl p-3 text-xs flex items-start gap-2 ${testResult.ok ? 'bg-emerald-50 border border-emerald-200 text-emerald-700 dark:bg-emerald-900/20 dark:border-emerald-900 dark:text-emerald-300' : 'bg-rose-50 border border-rose-200 text-rose-700 dark:bg-rose-900/20 dark:border-rose-900 dark:text-rose-300'}`}>
                  {testResult.ok ? <CheckCircle2 size={13} className="flex-shrink-0 mt-0.5"/> : <AlertCircle size={13} className="flex-shrink-0 mt-0.5"/>}
                  {testResult.msg}
                </div>
              )}
              {error && (
                <div className="rounded-xl p-3 text-xs bg-rose-50 border border-rose-200 text-rose-700 dark:bg-rose-900/20 dark:border-rose-900 dark:text-rose-300 flex items-start gap-2">
                  <AlertCircle size={13} className="flex-shrink-0 mt-0.5"/>{error}
                </div>
              )}
            </div>
          )}
        </div>

        {broker && (
          <div className="px-6 py-4 border-t border-slate-200 dark:border-slate-700 flex gap-2">
            <button onClick={() => { reset(); onClose() }} className="px-4 py-2 rounded-lg text-sm font-bold text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800">
              Cancel
            </button>
            <button onClick={() => testMutation.mutate()}
              disabled={testMutation.isPending || Object.keys(creds).length === 0}
              className="px-4 py-2 rounded-lg text-sm font-bold border border-slate-200 dark:border-slate-700 text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 disabled:opacity-50">
              {testMutation.isPending ? 'Testing…' : 'Test connection'}
            </button>
            <button onClick={() => addMutation.mutate()}
              disabled={addMutation.isPending || !accountName || Object.keys(creds).length === 0}
              className="flex-1 px-4 py-2 rounded-lg text-sm font-bold bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white">
              {addMutation.isPending ? 'Connecting…' : 'Connect account'}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
