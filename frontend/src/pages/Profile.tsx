import { useState, useRef, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { QRCodeSVG } from 'qrcode.react'
import { profileApi, billingApi, authApi } from '../api/endpoints'
import { useAuthStore } from '../stores/authStore'
import { useNavigate } from 'react-router-dom'
import { User, CreditCard, TrendingUp, Shield, ArrowLeft, Check, ShieldCheck, Sun, Moon, Monitor, Smartphone, X, FileText } from "lucide-react"
import { useThemeStore } from '../stores/themeStore'

const TIERS = [
  { id: 'free_trial', name: 'Tier 1 (Free Trial)', price: 0,
    features: ['30 days · no card required', 'Full scanner preview', 'Paper trading only', '500-ticker universe'] },
  { id: 'tier_2', name: 'Tier 2 (Futures Signals)', price: 49,
    features: ['ICT signals on ES/NQ/RTY/YM', 'Paper trading + backtest', '5 prop-firm accounts', 'Email support'] },
  { id: 'tier_3', name: 'Tier 3 (Options Scanner)', price: 99,
    features: ['Everything in Tier 2', '3,000+ ticker pre-market scanner', 'Daily 1+4 email at 8:30 ET', 'Low-Float Squeeze · 52WH · Oracle · Gap Runner', 'Manual execution in your broker'] },
  { id: 'tier_4', name: 'Tier 4 (Options Live) — Most Popular', price: 199,
    features: ['Everything in Tier 3', 'Tradier broker integration', 'One-click confirm → real fills', 'Live greeks · real bid/ask', 'Priority email support'] },
  { id: 'tier_5', name: 'Tier 5 (Fully Automated)', price: 399,
    features: ['Everything in Tier 4', 'Auto-execute (no manual confirm)', 'Multi-strategy concurrent', 'Wheel strategy with auto-rebalance', 'Priority + chat support'] },
]

export default function Profile() {
  const _user = useAuthStore((s) => s.user)
  const isAdmin = !!(_user as any)?.is_admin

  if (isAdmin) {
    return (
      <div className="min-h-screen bg-slate-50 dark:bg-slate-950 p-6">
        <div className="max-w-2xl mx-auto bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-6">
          <h1 className="text-2xl font-extrabold text-slate-900 dark:text-slate-100 mb-1">Admin account</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mb-6">
            This is a platform-admin account. Trading features are disabled by policy.
            Use the Admin Dashboard to manage users, view audit logs, and configure platform settings.
          </p>
          <div className="space-y-3 text-sm">
            <div><span className="text-slate-500">Email:</span> <span className="font-mono">{(_user as any)?.email}</span></div>
            <div><span className="text-slate-500">Username:</span> <span className="font-mono">{(_user as any)?.username}</span></div>
            <div><span className="text-slate-500">Role:</span> <span className="font-semibold text-violet-600">Admin</span></div>
            <div><span className="text-slate-500">2FA:</span> {(_user as any)?.totp_enabled ? <span className="text-green-600 font-semibold">Enabled</span> : <span className="text-amber-600 font-semibold">Disabled — set up in Admin Dashboard</span>}</div>
          </div>
          <div className="mt-6 flex gap-2">
            <a href="/app/admin" className="bg-violet-600 hover:bg-violet-700 text-white font-semibold px-4 py-2 rounded-lg text-sm">Admin Dashboard</a>
            <button onClick={() => { useAuthStore.getState().logout(); window.location.href='/login' }} className="bg-slate-200 dark:bg-slate-800 text-slate-700 dark:text-slate-200 font-semibold px-4 py-2 rounded-lg text-sm">Log out</button>
          </div>
        </div>
      </div>
    )
  }
  const { user } = useAuthStore()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [showUpgrade, setShowUpgrade] = useState(false)
  const [selectedTier, setSelectedTier] = useState('')
  const [promoCode, setPromoCode] = useState('')
  const [upgradeMsg, setUpgradeMsg] = useState('')

  const { data: profile, isLoading } = useQuery({
    queryKey: ['profile'],
    queryFn: () => profileApi.getProfile().then(r => r.data),
  })

  const upgradeMutation = useMutation({
    mutationFn: (data: { tier: string; promo_code?: string }) => profileApi.upgrade(data),
    onSuccess: (res) => {
      setUpgradeMsg('Upgraded to ' + (res.data.tier_name || selectedTier) + '!')
      qc.invalidateQueries({ queryKey: ['profile'] })
      setShowUpgrade(false)
    },
    onError: (err: any) => {
      setUpgradeMsg(err.response?.data?.detail || 'Upgrade failed')
    },
  })

  if (isLoading) return <div className="p-8 text-slate-500 dark:text-slate-400">Loading profile...</div>
  if (!profile) return <div className="p-8 text-slate-500 dark:text-slate-400">Could not load profile</div>

  return (
    <div className="p-8 max-w-4xl">
      <button onClick={() => navigate('/app')} className="flex items-center gap-1 text-sm text-slate-500 hover:text-blue-600 mb-6 dark:text-slate-400">
        <ArrowLeft size={14}/> Back to Dashboard
      </button>

      <h1 className="text-2xl font-bold text-slate-900 mb-6 dark:text-slate-100">My Profile</h1>

      {upgradeMsg && (
        <div className="mb-4 p-3 rounded-lg bg-blue-50 dark:bg-blue-900/20 text-blue-700 text-sm">{upgradeMsg}</div>
      )}

      {/* User Info Card */}
      <div className="bg-white rounded-xl border border-slate-200 p-6 mb-6 dark:bg-slate-800 dark:border-slate-700">
        <div className="flex items-center gap-4 mb-4">
          <div className="w-14 h-14 rounded-full bg-blue-100 text-blue-700 flex items-center justify-center text-xl font-bold">
            {profile.username?.[0]?.toUpperCase() || 'U'}
          </div>
          <div>
            <div className="text-lg font-bold text-slate-900 dark:text-slate-100">{profile.username}</div>
            <div className="text-sm text-slate-500 dark:text-slate-400">{profile.email}</div>
            <div className="text-xs text-slate-400 dark:text-slate-500">Member since {new Date(profile.created_at).toLocaleDateString()}</div>
          </div>
        </div>
      </div>

      {/* Subscription Card */}
      <div className="bg-white rounded-xl border border-slate-200 p-6 mb-6 dark:bg-slate-800 dark:border-slate-700">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <CreditCard size={18} className="text-blue-600"/>
            <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">Subscription</h2>
          </div>
          {!user?.is_admin && (
            <div className="flex gap-2">
              <button onClick={async () => {
                try {
                  const res = await billingApi.getPortal()
                  window.location.href = res.data.portal_url
                } catch {}
              }} className="px-4 py-1.5 border border-slate-200 text-slate-600 text-sm rounded-lg hover:bg-slate-50 dark:text-slate-300 dark:border-slate-700 dark:hover:bg-slate-800">
                Manage Billing
              </button>
              <button onClick={() => setShowUpgrade(!showUpgrade)} className="px-4 py-1.5 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700">
                {showUpgrade ? 'Cancel' : 'Change Plan'}
              </button>
            </div>
          )}
        </div>
        {user?.is_admin ? (
          <div className="flex items-center gap-3">
            <span className="inline-flex items-center gap-2 px-3 py-2 rounded-xl bg-gradient-to-r from-violet-600 to-violet-700 text-white font-bold text-sm shadow-lg shadow-violet-300/50 dark:shadow-violet-900/40">
              <svg viewBox="0 0 24 24" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2.4"><path d="M12 1l3 5h6l-5 4 2 7-6-4-6 4 2-7-5-4h6z"/></svg>
              ADMIN ACCOUNT
            </span>
            <div className="text-xs text-slate-500 dark:text-slate-400 leading-relaxed">
              No subscription — platform-owner access. All features unlocked. Admin Dashboard requires the safe-word.
            </div>
          </div>
        ) : (
          <div className="flex items-center gap-6">
            <div>
              <div className="text-sm text-slate-500 dark:text-slate-400">Current Plan</div>
              <div className="text-xl font-bold text-slate-900 dark:text-slate-100">{profile.tier_name}</div>
            </div>
            <div>
              <div className="text-sm text-slate-500 dark:text-slate-400">Monthly Price</div>
              <div className="text-xl font-bold text-slate-900 dark:text-slate-100">{profile.tier_price === 0 ? 'Free' : `$${profile.tier_price}/mo`}</div>
            </div>
          </div>
        )}
      </div>

      {/* Upgrade Tiers */}
      {showUpgrade && (
        <div className="bg-white rounded-xl border border-slate-200 p-6 mb-6 dark:bg-slate-800 dark:border-slate-700">
          <h3 className="text-lg font-semibold text-slate-900 mb-4 dark:text-slate-100">Choose a Plan</h3>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3 mb-4">
            {TIERS.map(tier => {
              const isMostPopular = tier.id === 'tier_4'
              const isSelected    = selectedTier === tier.id
              return (
                <div key={tier.id} onClick={() => setSelectedTier(tier.id)}
                  className={`relative p-4 rounded-lg border-2 cursor-pointer transition-all
                    ${isSelected
                      ? 'border-blue-600 bg-blue-50 dark:bg-blue-900/20'
                      : isMostPopular
                        ? 'border-amber-400 dark:border-amber-700/60 bg-amber-50/40 dark:bg-amber-900/10 hover:border-amber-500'
                        : 'border-slate-200 dark:border-slate-700 hover:border-blue-300'}`}>
                  {isMostPopular && (
                    <span className="absolute -top-2 left-1/2 -translate-x-1/2 bg-amber-400 text-amber-900 text-[9px] font-bold uppercase tracking-wider px-2 py-0.5 rounded">
                      Most Popular
                    </span>
                  )}
                  <div className="font-bold text-slate-900 dark:text-slate-100 text-sm leading-tight">{tier.name}</div>
                  <div className="text-2xl font-extrabold text-blue-600 dark:text-blue-400 my-1 tabular-nums">
                    {tier.price === 0
                      ? <>Free<span className="text-xs text-slate-400 dark:text-slate-500 font-medium ml-1">/30d</span></>
                      : <>${tier.price}<span className="text-sm text-slate-400 dark:text-slate-500 font-medium">/mo</span></>}
                  </div>
                  <ul className="mt-2 space-y-1">
                    {tier.features.map(f => (
                      <li key={f} className="text-[11px] text-slate-600 dark:text-slate-300 flex items-start gap-1 leading-snug">
                        <Check size={11} className="text-green-500 flex-shrink-0 mt-0.5"/>{f}
                      </li>
                    ))}
                  </ul>
                </div>
              )
            })}
          </div>
          <div className="space-y-3">
            <div className="flex items-center gap-3">
              <button onClick={async () => {
                if (!selectedTier) return
                try {
                  const res = await billingApi.createCheckout(selectedTier)
                  window.location.href = res.data.checkout_url
                } catch (err: any) {
                  setUpgradeMsg(err.response?.data?.detail || 'Checkout failed')
                }
              }}
                disabled={!selectedTier}
                className="px-6 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 disabled:opacity-50">
                Pay with Stripe
              </button>
            </div>
            <div className="flex items-center gap-2">
              <div className="h-px flex-1 bg-slate-200 dark:bg-slate-900"></div>
              <span className="text-xs text-slate-400 dark:text-slate-500">or use promo code</span>
              <div className="h-px flex-1 bg-slate-200 dark:bg-slate-900"></div>
            </div>
            <div className="flex items-center gap-3">
              <input value={promoCode} onChange={e => setPromoCode(e.target.value)}
                placeholder="Promo code" className="px-3 py-2 border border-slate-300 rounded-lg text-sm w-48 dark:border-slate-700"/>
              <button onClick={() => { if (selectedTier) upgradeMutation.mutate({ tier: selectedTier, promo_code: promoCode || undefined }) }}
                disabled={!selectedTier || upgradeMutation.isPending}
                className="px-6 py-2 bg-slate-700 text-white rounded-lg text-sm hover:bg-slate-800 disabled:opacity-50">
                {upgradeMutation.isPending ? 'Processing...' : 'Apply Code'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Stats Card */}
      <div className="bg-white rounded-xl border border-slate-200 p-6 mb-6 dark:bg-slate-800 dark:border-slate-700">
        <div className="flex items-center gap-2 mb-4">
          <TrendingUp size={18} className="text-blue-600"/>
          <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">Lifetime Stats</h2>
        </div>
        <div className="grid grid-cols-3 gap-6">
          <div>
            <div className="text-sm text-slate-500 dark:text-slate-400">Lifetime P&L</div>
            <div className={`text-2xl font-bold ${(profile.lifetime_pnl ?? 0) >= 0 ? 'text-green-600' : 'text-red-500'}`}>
              ${profile.lifetime_pnl.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
            </div>
          </div>
          <div>
            <div className="text-sm text-slate-500 dark:text-slate-400">Total Trades</div>
            <div className="text-2xl font-bold text-slate-900 dark:text-slate-100">{profile.total_trades}</div>
          </div>
          <div>
            <div className="text-sm text-slate-500 dark:text-slate-400">Win Rate</div>
            <div className="text-2xl font-bold text-slate-900 dark:text-slate-100">{(profile.win_rate * 100).toFixed(1)}%</div>
          </div>
        </div>
      </div>

      {/* Accounts Card */}
      <div className="bg-white rounded-xl border border-slate-200 p-6 mb-6 dark:bg-slate-800 dark:border-slate-700">
        <div className="flex items-center gap-2 mb-4">
          <Shield size={18} className="text-blue-600"/>
          <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">Broker Accounts</h2>
        </div>
        {profile.accounts.length === 0 ? (
          <div className="text-sm text-slate-400 dark:text-slate-500">No broker accounts connected yet</div>
        ) : (
          <div className="space-y-2">
            {profile.accounts.map((a: any) => (
              <div key={a.id} className="flex items-center justify-between p-3 bg-slate-50 rounded-lg dark:bg-slate-900">
                <div>
                  <div className="text-sm font-medium text-slate-900 dark:text-slate-100">{a.name}</div>
                  <div className="text-xs text-slate-400 dark:text-slate-500">{a.broker} {a.is_demo ? '(Demo)' : '(Live)'}</div>
                </div>
                <div className={`text-xs px-2 py-0.5 rounded ${a.is_active ? 'bg-green-100 text-green-700' : 'bg-slate-200 text-slate-500'}`}>
                  {a.is_active ? 'Active' : 'Inactive'}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Two-Factor Authentication */}
      <ThemeCard />
      <DeviceCard />
      <AcknowledgementsCard />
      <TwoFactorCard />

      {/* Active Sessions */}
      <div className="bg-white rounded-xl border border-slate-200 p-6 dark:bg-slate-800 dark:border-slate-700">
        <div className="text-sm text-slate-500 dark:text-slate-400">Active Paper Trading Sessions</div>
        <div className="text-2xl font-bold text-slate-900 dark:text-slate-100">{profile.active_paper_sessions}</div>
      </div>
    </div>
  )
}



// ── Legal Acknowledgements card ─────────────────────────────────────
function AcknowledgementsCard() {
  const qc = useQueryClient()
  const [openKind, setOpenKind] = useState<string | null>(null)

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['legal-status'],
    queryFn: () => fetch('/api/v1/legal/status', {
      headers: { Authorization: `Bearer ${useAuthStore.getState().token}` },
    }).then(r => r.json()),
  })

  const acks: Record<string, { current_version: string; accepted: boolean }> = data?.acknowledgments || {}

  const META: Record<string, { title: string; body: string; required: string }> = {
    risk_disclosure: {
      title: 'Risk disclosure',
      body: 'You acknowledge that trading involves risk, past performance does not guarantee future results, and you alone decide what to deploy.',
      required: 'Required for all live + paper trading sessions',
    },
    live_trading_consent: {
      title: 'Live trading consent',
      body: 'You authorize the bot to place real-money orders on your linked broker account, subject to your sizing rules and risk limits.',
      required: 'Required to start any live broker session',
    },
    options_trading_consent: {
      title: 'Options trading consent',
      body: 'You understand options-specific risks: time decay (theta), IV crush around earnings, total loss of premium, assignment, and pin risk.',
      required: 'Required to deploy any options strategy live',
    },
    terms_of_service: {
      title: 'Terms of Service',
      body: 'General terms governing your use of Theta Algos.',
      required: 'Required at signup',
    },
  }

  return (
    <div className="bg-white rounded-xl border border-slate-200 p-6 mb-6 dark:bg-slate-800 dark:border-slate-700">
      <div className="flex items-center gap-2 mb-4">
        <ShieldCheck size={18} className="text-violet-600 dark:text-violet-400"/>
        <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">Legal acknowledgements</h2>
      </div>
      <p className="text-sm text-slate-500 mb-4 dark:text-slate-400">
        Review and accept the legal documents required to use Theta Algos. Sessions cannot start until the relevant ones are accepted.
      </p>

      {isLoading ? (
        <div className="text-sm text-slate-400">Loading…</div>
      ) : (
        <div className="space-y-2">
          {Object.entries(META).map(([kind, meta]) => {
            const status = acks[kind]
            const accepted = status?.accepted
            return (
              <div key={kind}
                className={`rounded-xl border p-4 flex items-start gap-3 ${
                  accepted
                    ? 'border-emerald-200 dark:border-emerald-900 bg-emerald-50/40 dark:bg-emerald-900/10'
                    : 'border-amber-200 dark:border-amber-900 bg-amber-50/40 dark:bg-amber-900/10'
                }`}>
                <div className={`mt-0.5 w-7 h-7 rounded-full flex items-center justify-center flex-shrink-0 ${
                  accepted ? 'bg-emerald-500 text-white' : 'bg-amber-500 text-white'
                }`}>
                  {accepted ? <Check size={14}/> : <FileText size={14}/>}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-bold text-sm text-slate-900 dark:text-slate-100">{meta.title}</span>
                    {status && (
                      <span className="text-[9px] uppercase tracking-wider font-bold text-slate-500 dark:text-slate-400">v{status.current_version}</span>
                    )}
                  </div>
                  <p className="text-xs text-slate-600 dark:text-slate-300 mt-0.5 leading-snug">{meta.body}</p>
                  <p className="text-[10px] text-slate-400 dark:text-slate-500 mt-1 italic">{meta.required}</p>
                </div>
                <button
                  onClick={() => accepted ? null : setOpenKind(kind)}
                  disabled={accepted}
                  className={`px-3 py-1.5 rounded-lg text-[11px] font-bold whitespace-nowrap ${
                    accepted
                      ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300 cursor-default'
                      : 'bg-amber-500 hover:bg-amber-600 text-white'
                  }`}>
                  {accepted ? 'Accepted ✓' : 'Review & accept'}
                </button>
              </div>
            )
          })}
        </div>
      )}

      {openKind && (
        <AckDocModal kind={openKind} onClose={() => setOpenKind(null)} onAccepted={() => { refetch(); setOpenKind(null) }}/>
      )}
    </div>
  )
}

// Inline modal that loads /legal/documents/{kind} and POSTs /legal/acknowledge
function AckDocModal({ kind, onClose, onAccepted }: { kind: string; onClose: () => void; onAccepted: () => void }) {
  const token = useAuthStore.getState().token
  const [doc, setDoc] = useState<{ kind: string; version: string; html?: string; body?: string; title: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [scrolled, setScrolled] = useState(false)
  const ref = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    fetch(`/api/v1/legal/documents/${kind}`, { headers: { Authorization: `Bearer ${token}` } })
      .then(r => r.json()).then(setDoc).catch(() => setErr('Could not load document'))
  }, [kind, token])

  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (el.scrollHeight - el.clientHeight <= 4) setScrolled(true)
  }, [doc])

  function onScroll(e: React.UIEvent<HTMLDivElement>) {
    const el = e.currentTarget
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 16) setScrolled(true)
  }

  async function accept() {
    setBusy(true); setErr(null)
    try {
      const r = await fetch('/api/v1/legal/acknowledge', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ kind }),
      })
      if (!r.ok) {
        const j = await r.json().catch(() => ({}))
        throw new Error(j.detail || 'Could not record acknowledgement.')
      }
      onAccepted()
    } catch (e: any) { setErr(e.message) }
    finally { setBusy(false) }
  }

  return (
    <div className="fixed inset-0 z-[100] bg-black/70 flex items-center justify-center p-4" onClick={onClose}>
      <div onClick={e => e.stopPropagation()} className="bg-white dark:bg-slate-900 rounded-2xl shadow-2xl w-full max-w-2xl max-h-[85vh] flex flex-col">
        <div className="px-6 py-4 border-b border-slate-200 dark:border-slate-700 flex items-center justify-between">
          <div>
            <div className="text-[10px] uppercase tracking-widest text-amber-600 dark:text-amber-400 font-bold">Acknowledgement</div>
            <h2 className="text-lg font-extrabold text-slate-900 dark:text-slate-100">{doc?.title || '...'}</h2>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-700 p-1.5 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800"><X size={18}/></button>
        </div>
        <div ref={ref} onScroll={onScroll} className="flex-1 overflow-y-auto px-6 py-5 text-sm text-slate-700 dark:text-slate-200 leading-relaxed">
          {!doc ? <div className="text-slate-400">Loading…</div>
            : doc.html ? <div dangerouslySetInnerHTML={{ __html: doc.html }}/>
            : <pre className="whitespace-pre-wrap font-sans">{doc.body}</pre>}
        </div>
        {err && <div className="px-6 pb-2 text-xs text-rose-600">{err}</div>}
        <div className="px-6 py-4 border-t border-slate-200 dark:border-slate-700 flex gap-2 items-center">
          {!scrolled && <span className="text-[10px] text-slate-400 italic">Scroll to the bottom to enable accept</span>}
          <div className="flex-1"/>
          <button onClick={onClose} className="px-4 py-2 rounded-lg text-sm font-bold text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800">Cancel</button>
          <button onClick={accept} disabled={!scrolled || busy}
            className="px-4 py-2 rounded-lg text-sm font-bold bg-emerald-600 hover:bg-emerald-700 disabled:opacity-40 text-white">
            {busy ? 'Recording…' : 'I understand and agree'}
          </button>
        </div>
      </div>
    </div>
  )
}


function ThemeCard() {
  const { theme, setTheme } = useThemeStore()
  const opts: { id: 'light' | 'dark'; label: string; icon: any }[] = [
    { id: 'light', label: 'Light', icon: Sun },
    { id: 'dark',  label: 'Dark',  icon: Moon },
  ]
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-6 mb-6 dark:bg-slate-800 dark:border-slate-700">
      <div className="flex items-center gap-2 mb-4">
        <Sun size={18} className="text-blue-600 dark:hidden"/>
        <Moon size={18} className="text-blue-400 hidden dark:inline"/>
        <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">Appearance</h2>
      </div>
      <p className="text-sm text-slate-500 mb-4 dark:text-slate-400">Pick the look that suits you. Stored on this device.</p>
      <div className="grid grid-cols-2 gap-3 max-w-sm">
        {opts.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => setTheme(id)}
            className={`flex items-center gap-3 px-4 py-3 rounded-xl border text-sm font-medium transition-all ${ theme === id ? 'border-blue-500 bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300' : 'border-slate-200 text-slate-600 hover:border-slate-300 dark:border-slate-700 dark:text-slate-300 dark:hover:border-slate-600' }`}
          >
            <Icon size={16}/>
            {label}
          </button>
        ))}
      </div>
    </div>
  )
}

function DeviceCard() {
  const KEY = 'edge_device_pref'
  const [pref, setPref] = useState<'browser' | 'mobile'>(() => {
    const v = localStorage.getItem(KEY)
    return v === 'mobile' ? 'mobile' : 'browser'
  })
  const choose = (p: 'browser' | 'mobile') => {
    localStorage.setItem(KEY, p)
    document.body.classList.toggle('device-mobile', p === 'mobile')
    document.body.classList.toggle('device-browser', p === 'browser')
    setPref(p)
  }
  const opts: { id: 'browser' | 'mobile'; label: string; icon: any }[] = [
    { id: 'browser', label: 'Web Browser', icon: Monitor },
    { id: 'mobile',  label: 'Mobile',      icon: Smartphone },
  ]
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-6 mb-6 dark:bg-slate-800 dark:border-slate-700">
      <div className="flex items-center gap-2 mb-4">
        <Monitor size={18} className="text-blue-600"/>
        <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">Display</h2>
      </div>
      <p className="text-sm text-slate-500 mb-4 dark:text-slate-400">Switch between desktop and mobile-optimized layouts. Stored on this device.</p>
      <div className="grid grid-cols-2 gap-3 max-w-sm">
        {opts.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => choose(id)}
            className={`flex items-center gap-3 px-4 py-3 rounded-xl border text-sm font-medium transition-all ${ pref === id ? 'border-blue-500 bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300' : 'border-slate-200 text-slate-600 hover:border-slate-300 dark:border-slate-700 dark:text-slate-300 dark:hover:border-slate-600' }`}
          >
            <Icon size={16}/>
            {label}
          </button>
        ))}
      </div>
    </div>
  )
}

function TwoFactorCard() {
  const qc = useQueryClient()
  const { data: me } = useQuery({
    queryKey: ['auth-me'],
    queryFn: () => authApi.me().then(r => r.data),
  })

  const [setupData, setSetupData] = useState<{ secret: string; otpauth_url: string } | null>(null)
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const refreshMe = () => qc.invalidateQueries({ queryKey: ['auth-me'] })

  const startSetup = async () => {
    setBusy(true); setError('')
    try {
      const { data } = await authApi.setup2FA()
      setSetupData(data)
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Could not start 2FA setup.')
    } finally { setBusy(false) }
  }

  const confirm = async () => {
    setBusy(true); setError('')
    try {
      await authApi.confirm2FA(code.trim())
      setSetupData(null); setCode('')
      refreshMe()
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Invalid code.')
    } finally { setBusy(false) }
  }

  const disable = async () => {
    setBusy(true); setError('')
    try {
      await authApi.disable2FA(code.trim())
      setCode('')
      refreshMe()
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Invalid code.')
    } finally { setBusy(false) }
  }

  const enabled = !!me?.totp_enabled

  return (
    <div className="bg-white rounded-xl border border-slate-200 p-6 mb-6 dark:bg-slate-800 dark:border-slate-700">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <ShieldCheck size={18} className="text-blue-600"/>
          <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">Two-Factor Authentication</h2>
        </div>
        <span className={`text-xs font-semibold px-2 py-1 rounded-lg ${enabled ? 'bg-green-50 dark:bg-green-900/20 text-green-700' : 'bg-slate-100 text-slate-500'}`}>
          {enabled ? 'Enabled' : 'Disabled'}
        </span>
      </div>

      {!enabled && !setupData && !me?.totp_setup_pending && (
        <>
          <p className="text-sm text-slate-500 mb-4 dark:text-slate-400">
            Add a second login step using an authenticator app (Google Authenticator, Authy, 1Password, etc.). Once enabled, signing in requires the 6-digit code.
          </p>
          <button onClick={startSetup} disabled={busy}
            className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-semibold rounded-lg">
            {busy ? 'Starting…' : 'Enable 2FA'}
          </button>
        </>
      )}

      {!enabled && !setupData && me?.totp_setup_pending && (
        <>
          <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-xl p-4 mb-4 flex items-start gap-3">
            <ShieldCheck size={18} className="text-amber-600 flex-shrink-0 mt-0.5"/>
            <div className="text-sm text-amber-800 dark:text-amber-200">
              <div className="font-bold mb-1">⚠️ 2FA setup is incomplete</div>
              <p className="leading-relaxed">
                You started enabling 2FA but never verified the code. Click <strong>Resume setup</strong> below to see your QR code again and enter the 6-digit code — until then, you can still sign in without 2FA.
              </p>
            </div>
          </div>
          <button onClick={startSetup} disabled={busy}
            className="px-4 py-2 bg-amber-600 hover:bg-amber-700 disabled:opacity-50 text-white text-sm font-semibold rounded-lg">
            {busy ? 'Resuming…' : 'Resume 2FA setup'}
          </button>
        </>
      )}

      {!enabled && setupData && (
        <>
          <p className="text-sm text-slate-500 mb-4 dark:text-slate-400">
            Scan this QR code in your authenticator app, then enter the 6-digit code it shows.
          </p>
          <div className="flex flex-col sm:flex-row gap-5 mb-4">
            <div className="bg-white border border-slate-200 rounded-xl p-3 self-start dark:bg-slate-800 dark:border-slate-700">
              <QRCodeSVG value={setupData.otpauth_url} size={160} />
            </div>
            <div className="flex-1">
              <div className="text-xs text-slate-500 mb-1 dark:text-slate-400">Or enter this code manually:</div>
              <code className="block bg-slate-100 rounded-lg px-3 py-2 text-xs font-mono text-slate-700 break-all mb-4 dark:bg-slate-800 dark:text-slate-200">{setupData.secret}</code>
              <label className="block text-sm font-medium text-slate-700 mb-1.5 dark:text-slate-200">6-digit code</label>
              <input
                type="text"
                inputMode="numeric"
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                className="w-full border border-slate-300 rounded-lg px-3 py-2 text-base font-mono tracking-[0.4em] text-center focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700"
                placeholder="123456"/>
            </div>
          </div>
          {error && <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 text-red-700 text-sm px-3 py-2 rounded-lg mb-3">{error}</div>}
          <div className="flex gap-2">
            <button onClick={confirm} disabled={busy || code.length !== 6}
              className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-semibold rounded-lg">
              {busy ? 'Verifying…' : 'Verify and enable'}
            </button>
            <button onClick={() => { setSetupData(null); setCode(''); setError('') }}
              className="px-4 py-2 border border-slate-200 text-slate-600 text-sm rounded-lg hover:bg-slate-50 dark:text-slate-300 dark:border-slate-700 dark:hover:bg-slate-800">
              Cancel
            </button>
          </div>
        </>
      )}

      {enabled && (
        <>
          <p className="text-sm text-slate-500 mb-4 dark:text-slate-400">
            2FA is on. To disable it, enter a current 6-digit code from your authenticator app.
          </p>
          <div className="flex flex-col sm:flex-row gap-2 sm:items-end">
            <div className="flex-1">
              <label className="block text-sm font-medium text-slate-700 mb-1.5 dark:text-slate-200">Authentication code</label>
              <input
                type="text"
                inputMode="numeric"
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                className="w-full border border-slate-300 rounded-lg px-3 py-2 text-base font-mono tracking-[0.4em] text-center focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700"
                placeholder="123456"/>
            </div>
            <button onClick={disable} disabled={busy || code.length !== 6}
              className="px-4 py-2 bg-red-600 hover:bg-red-700 disabled:opacity-50 text-white text-sm font-semibold rounded-lg sm:mb-0">
              {busy ? 'Disabling…' : 'Disable 2FA'}
            </button>
          </div>
          {error && <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 text-red-700 text-sm px-3 py-2 rounded-lg mt-3">{error}</div>}
        </>
      )}
    </div>
  )
}
