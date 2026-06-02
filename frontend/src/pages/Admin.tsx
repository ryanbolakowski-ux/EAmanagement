import { useState, useEffect, useMemo } from 'react'
import { authApi } from '../api/endpoints'
import api from '../api/client'
import { useAuthStore } from '../stores/authStore'
import {
  Shield, ShieldCheck, ShieldAlert, Users, TrendingUp, Activity,
  FlaskConical, Sliders, Trash2, Eye, DollarSign, X, Search,
  Gift, LayoutDashboard, FileCheck2, CreditCard, Lock, RefreshCw,
} from 'lucide-react'
import { fmtEntryTime, fmtHold } from '../components/TradeMetrics'
import SystemsCheck from '../components/SystemsCheck'

const API = ((import.meta as any).env?.VITE_API_URL || '') + '/api/v1/admin'
const TIER_LABELS: Record<string, string> = {
  free_trial: 'Free Trial', tier_2: 'Futures Signals', tier_3: 'Options Scanner', tier_4: 'Options Live', tier_5: 'Fully Automated',
}
const TIER_ORDER = ['free_trial', 'tier_2', 'tier_3', 'tier_4', 'tier_5']
const TIER_COLORS: Record<string, string> = {
  free_trial: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  tier_2: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  tier_3: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  tier_4: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  tier_5: 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
}
const TIER_PRICES: Record<string, number> = { tier_2: 49, tier_3: 99, tier_4: 199, tier_5: 399 }

const KYC_BADGE = (status: string) => {
  const styles: Record<string, string> = {
    verified: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
    pending: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
    requires_input: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
    failed: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
    not_started: 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400',
  }
  return styles[status] || styles.not_started
}

type AdminTab = 'overview' | 'users' | 'kyc' | 'subscriptions' | 'comps' | 'systems' | 'system'

function StatCard({ icon: Icon, label, value, sub, tone = 'blue' }: any) {
  const tones: Record<string, string> = {
    blue: 'bg-blue-50 text-blue-600 dark:bg-blue-900/30 dark:text-blue-300',
    green: 'bg-emerald-50 text-emerald-600 dark:bg-emerald-900/30 dark:text-emerald-300',
    red: 'bg-red-50 text-red-600 dark:bg-red-900/30 dark:text-red-300',
    amber: 'bg-amber-50 text-amber-600 dark:bg-amber-900/30 dark:text-amber-300',
    violet: 'bg-violet-50 text-violet-600 dark:bg-violet-900/30 dark:text-violet-300',
    indigo: 'bg-indigo-50 text-indigo-600 dark:bg-indigo-900/30 dark:text-indigo-300',
    cyan: 'bg-cyan-50 text-cyan-600 dark:bg-cyan-900/30 dark:text-cyan-300',
    slate: 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400',
  }
  return (
    <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-5 transition-shadow hover:shadow-md">
      <div className="flex items-center justify-between mb-4">
        <div className={`w-10 h-10 rounded-xl flex items-center justify-center ${tones[tone] || tones.blue}`}>
          <Icon size={18} />
        </div>
        <span className="text-[10px] font-bold uppercase tracking-widest text-slate-400 dark:text-slate-500">{label}</span>
      </div>
      <div className="text-2xl font-extrabold text-slate-900 dark:text-slate-100 tabular-nums">{value ?? '—'}</div>
      {sub && <div className="text-xs text-slate-500 dark:text-slate-400 mt-1">{sub}</div>}
    </div>
  )
}

function SectionHeader({ title, subtitle, action }: { title: string; subtitle?: string; action?: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between mb-5 gap-4">
      <div>
        <h2 className="text-xl font-extrabold text-slate-900 dark:text-slate-100">{title}</h2>
        {subtitle && <p className="text-sm text-slate-500 dark:text-slate-400 mt-0.5">{subtitle}</p>}
      </div>
      {action}
    </div>
  )
}

export default function Admin() {
  const { token, user: storedMe, setAuth } = useAuthStore()
  const [freshMe, setFreshMe] = useState<any>(null)
  const me = freshMe ?? storedMe
  const [tab, setTab] = useState<AdminTab>('overview')

  useEffect(() => {
    if (!token) return
    authApi.me().then(r => { setFreshMe(r.data); if (r.data && token) setAuth(r.data, token) }).catch(() => {})
  }, [token])

  // Data state
  const [stats, setStats] = useState<any>(null)
  const [users, setUsers] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [passcodeVerified, setPasscodeVerified] = useState<boolean | null>(null)
  const [passcodeInput, setPasscodeInput] = useState('')
  const [passcodeError, setPasscodeError] = useState<string | null>(null)
  const [passcodeBusy, setPasscodeBusy] = useState(false)
  const [viewingTrades, setViewingTrades] = useState<string | null>(null)
  const [userTrades, setUserTrades] = useState<any[]>([])
  const [userAcks, setUserAcks] = useState<any[]>([])
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [tradeFilter, setTradeFilter] = useState('')
  const [comps, setComps] = useState<any[]>([])
  const [subscriptions, setSubscriptions] = useState<any[]>([])
  const [compTarget, setCompTarget] = useState<any | null>(null)
  const [compForm, setCompForm] = useState<{tier: string; days: number; note: string}>({tier: 'tier_3', days: 30, note: ''})
  const [compBusy, setCompBusy] = useState(false)
  const [detailTab, setDetailTab] = useState<'trades' | 'acks'>('trades')

  // KYC state
  const [kycEvents, setKycEvents] = useState<any[]>([])
  const [kycSummary, setKycSummary] = useState<any[]>([])
  const [kycOverrideUser, setKycOverrideUser] = useState<any | null>(null)
  const [kycOverrideStatus, setKycOverrideStatus] = useState('verified')
  const [kycOverrideReason, setKycOverrideReason] = useState('')
  const [kycBusy, setKycBusy] = useState(false)

  const headers: Record<string,string> = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }

  useEffect(() => {
    if (!me?.is_admin) return
    fetch(API + '/passcode-status', { headers }).then(r => r.json())
      .then(d => setPasscodeVerified(!!d.passcode_verified)).catch(() => setPasscodeVerified(false))
  }, [me?.is_admin])

  async function submitPasscode(e?: React.FormEvent) {
    e?.preventDefault()
    if (!passcodeInput.trim() || passcodeBusy) return
    setPasscodeBusy(true); setPasscodeError(null)
    try {
      const r = await fetch(API + '/verify-passcode', { method: 'POST', headers, body: JSON.stringify({ code: passcodeInput.trim() }) })
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || 'Invalid passcode.') }
      setPasscodeVerified(true); setPasscodeInput(''); void fetchData()
    } catch (err: any) { setPasscodeError(err.message || 'Invalid passcode.') }
    finally { setPasscodeBusy(false) }
  }

  async function lockAdmin() {
    await fetch(API + '/lock', { method: 'POST', headers }).catch(() => {})
    setPasscodeVerified(false)
  }

  async function fetchData() {
    setLoading(true)
    try {
      const [statsRes, usersRes, compsRes, subsRes, kycEventsRes, kycSummaryRes] = await Promise.all([
        fetch(API + '/stats', { headers }),
        fetch(API + '/users', { headers }),
        fetch(API + '/comps', { headers }),
        fetch(API + '/subscriptions', { headers }),
        fetch(API + '/kyc/events?limit=300', { headers }),
        fetch(API + '/kyc/summary', { headers }),
      ])
      setStats(await statsRes.json())
      setUsers(await usersRes.json())
      setComps((await compsRes.json()).comps || [])
      setSubscriptions((await subsRes.json()).subscriptions || [])
      setKycEvents((await kycEventsRes.json()).events || [])
      setKycSummary((await kycSummaryRes.json()).by_status || [])
      setError(null)
    } catch (e: any) { setError(e.message) }
    setLoading(false)
  }

  useEffect(() => {
    if (!passcodeVerified) return
    fetchData()
    const i = setInterval(fetchData, 300000)
    return () => clearInterval(i)
  }, [passcodeVerified])

  // ── Actions ──
  async function changeTier(userId: string, tier: string) {
    await fetch(API + '/users/' + userId + '/tier', { method: 'PUT', headers, body: JSON.stringify({ tier }) })
    fetchData()
  }
  async function deleteUser(userId: string) {
    await fetch(API + '/users/' + userId, { method: 'DELETE', headers })
    setDeleteConfirm(null); fetchData()
  }
  async function viewTrades(userId: string) {
    const [t, a] = await Promise.all([
      fetch(API + '/users/' + userId + '/trades', { headers }),
      fetch(API + '/users/' + userId + '/acknowledgments', { headers }),
    ])
    if (t.ok) setUserTrades(await t.json())
    if (a.ok) setUserAcks(await a.json()); else setUserAcks([])
    setViewingTrades(userId); setDetailTab('trades')
  }
  async function submitGrantComp() {
    if (!compTarget) return
    setCompBusy(true)
    try {
      const r = await fetch(API + '/users/' + compTarget.id + '/grant-comp', {
        method: 'POST', headers,
        body: JSON.stringify({ tier: compForm.tier, expires_days: compForm.days, note: compForm.note || null }),
      })
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || 'Could not grant comp.') }
      setCompTarget(null); setCompForm({tier: 'tier_3', days: 30, note: ''}); await fetchData()
    } catch (e: any) { alert(e.message || 'Could not grant comp.') }
    finally { setCompBusy(false) }
  }
  async function revokeComp(userId: string) {
    if (!confirm('Revoke this comp? User drops to Tier 1 immediately.')) return
    await fetch(API + '/users/' + userId + '/revoke-comp', { method: 'POST', headers })
    fetchData()
  }
  async function applyKycOverride() {
    if (!kycOverrideUser) return
    setKycBusy(true)
    try {
      const r = await fetch(API + '/kyc/manual', {
        method: 'POST', headers,
        body: JSON.stringify({ user_id: kycOverrideUser.id, new_status: kycOverrideStatus, reason: kycOverrideReason || null }),
      })
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || 'Override failed.') }
      setKycOverrideUser(null); setKycOverrideReason(''); setKycOverrideStatus('verified')
      await fetchData()
    } catch (e: any) { alert(e.message || 'Override failed.') }
    finally { setKycBusy(false) }
  }

  const ACK_KIND_LABELS: Record<string, string> = {
    terms_of_service: 'Terms of Service', risk_disclosure: 'Risk Disclosure',
    live_trading_consent: 'Live Trading Consent', risk_change: 'Risk Per Trade Change',
  }
  const mrr = users.reduce((s, u) => s + (TIER_PRICES[u.tier] || 0), 0)
  const paying = users.filter(u => TIER_PRICES[u.tier]).length
  const verifiedCount = users.filter(u => u.kyc_status === 'verified').length
  const pendingKyc = users.filter(u => u.kyc_status === 'pending' || u.kyc_status === 'requires_input').length

  const filtered = useMemo(() => users.filter(u =>
    search === '' || u.username?.toLowerCase().includes(search.toLowerCase()) || u.email?.toLowerCase().includes(search.toLowerCase())
  ), [users, search])

  // ── Gates ──
  if (error) return (
    <div className="p-8">
      <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 rounded-xl p-6 text-center">
        <Shield size={32} className="mx-auto text-red-400 mb-3"/>
        <p className="font-semibold text-red-600">Admin Access Required</p>
        <p className="text-sm text-red-400 mt-1">You need admin access to view this page.</p>
      </div>
    </div>
  )
  if (me?.is_admin && passcodeVerified === false) return (
    <div className="min-h-screen bg-slate-100 dark:bg-slate-950 flex items-center justify-center p-6">
      <div className="w-full max-w-md bg-white dark:bg-slate-900 rounded-2xl border border-violet-200 dark:border-violet-900/60 shadow-2xl p-7">
        <div className="text-center mb-5">
          <div className="inline-flex w-14 h-14 rounded-full bg-violet-100 dark:bg-violet-900/40 items-center justify-center mb-3">
            <Lock className="w-7 h-7 text-violet-600 dark:text-violet-300"/>
          </div>
          <h2 className="text-xl font-extrabold text-slate-900 dark:text-slate-100">Admin safe-word</h2>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1.5 leading-relaxed">
            Even after your password and 2FA, admin actions require this extra code.
          </p>
        </div>
        <form onSubmit={submitPasscode} className="space-y-3">
          <input type="password" inputMode="numeric" autoFocus value={passcodeInput}
            onChange={e => setPasscodeInput(e.target.value)} placeholder="Enter safe-word"
            className="w-full px-3 py-2.5 border border-slate-300 dark:border-slate-700 rounded-lg text-base font-mono tracking-[0.4em] text-center dark:bg-slate-800 dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-violet-500"/>
          {passcodeError && <div className="text-sm text-red-600 dark:text-red-400 text-center">{passcodeError}</div>}
          <button type="submit" disabled={!passcodeInput.trim() || passcodeBusy}
            className="w-full bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white font-bold py-2.5 rounded-lg text-sm">
            {passcodeBusy ? 'Verifying…' : 'Unlock admin'}
          </button>
        </form>
        <p className="text-[10px] text-slate-400 text-center mt-4">Valid for 8 hours on this browser.</p>
      </div>
    </div>
  )
  if (passcodeVerified === null) return null

  const tradingUser = viewingTrades ? users.find(u => u.id === viewingTrades) : null
  const SidebarItem = ({ id, icon: Icon, label, badge }: any) => (
    <button onClick={() => setTab(id)}
      className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-semibold transition-colors ${
        tab === id
          ? 'bg-violet-600 text-white'
          : 'text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800'
      }`}>
      <Icon size={16}/>
      <span className="flex-1 text-left">{label}</span>
      {badge !== undefined && <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${tab===id ? 'bg-white/20 text-white' : 'bg-slate-200 dark:bg-slate-700 text-slate-700 dark:text-slate-200'}`}>{badge}</span>}
    </button>
  )

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-slate-950">
      <div className="max-w-[1600px] mx-auto flex flex-col lg:flex-row gap-6 p-4 sm:p-6">

        {/* Sidebar */}
        <aside className="lg:w-60 flex-shrink-0">
          <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-4 lg:sticky lg:top-6">
            <div className="flex items-center gap-2 px-2 pb-4 mb-3 border-b border-slate-200 dark:border-slate-800">
              <div className="w-9 h-9 rounded-lg bg-gradient-to-br from-violet-500 to-indigo-600 flex items-center justify-center">
                <Shield className="text-white" size={18}/>
              </div>
              <div>
                <div className="text-sm font-extrabold text-slate-900 dark:text-slate-100 leading-tight">Admin</div>
                <div className="text-[10px] text-slate-500 dark:text-slate-400 font-mono truncate max-w-[150px]">{me?.email}</div>
              </div>
            </div>
            <nav className="space-y-1">
              <SidebarItem id="overview" icon={LayoutDashboard} label="Overview"/>
              <SidebarItem id="users" icon={Users} label="Users" badge={users.length}/>
              <SidebarItem id="kyc" icon={FileCheck2} label="KYC" badge={pendingKyc || undefined}/>
              <SidebarItem id="subscriptions" icon={CreditCard} label="Subscriptions" badge={subscriptions.length}/>
              <SidebarItem id="comps" icon={Gift} label="Comps" badge={comps.length}/>
              <SidebarItem id="systems" icon={Activity} label="Systems Check"/>
              <SidebarItem id="system" icon={Lock} label="System"/>
            </nav>
            <div className="mt-4 pt-4 border-t border-slate-200 dark:border-slate-800 space-y-2">
              <button onClick={fetchData} className="w-full flex items-center justify-center gap-2 text-xs font-semibold text-slate-600 dark:text-slate-300 py-2 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800">
                <RefreshCw size={12} className={loading ? 'animate-spin' : ''}/> Refresh
              </button>
              <button onClick={lockAdmin} className="w-full flex items-center justify-center gap-2 text-xs font-semibold text-red-600 dark:text-red-400 py-2 rounded-lg hover:bg-red-50 dark:hover:bg-red-900/20">
                <Lock size={12}/> Lock admin
              </button>
            </div>
          </div>
        </aside>

        {/* Main */}
        <main className="flex-1 min-w-0 space-y-6">

          {tab === 'overview' && (<>
            <SectionHeader title="Platform overview" subtitle="Real-time snapshot of users, revenue, and trading activity."/>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
              <StatCard icon={Users} label="Total Users" value={stats?.total_users} sub={`+${stats?.new_today ?? 0} today`} tone="blue"/>
              <StatCard icon={DollarSign} label="Monthly Revenue" value={`$${mrr}`} sub={`${paying} paying customers`} tone="green"/>
              <StatCard icon={ShieldCheck} label="KYC Verified" value={verifiedCount} sub={`${pendingKyc} pending`} tone="violet"/>
              <StatCard icon={TrendingUp} label="New This Week" value={stats?.new_this_week} tone="indigo"/>
            </div>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
              <StatCard icon={Activity} label="Active Paper Sessions" value={stats?.active_paper_sessions} tone="amber"/>
              <StatCard icon={FlaskConical} label="Backtests (7d)" value={stats?.recent_backtests} tone="cyan"/>
              <StatCard icon={Sliders} label="Optimizations (7d)" value={stats?.recent_optimizations} tone="violet"/>
              <StatCard icon={TrendingUp} label="Avg Win Rate" value={`${((stats?.avg_win_rate ?? 0) * 100).toFixed(1)}%`} sub={`${stats?.accounts_with_trades ?? 0} accounts`} tone={(stats?.avg_win_rate ?? 0) >= 0.5 ? 'green' : 'amber'}/>
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <StatCard icon={Activity} label="Total Paper Trades" value={(stats?.paper_trade_count ?? 0).toLocaleString()} sub={`Platform P&L: ${(stats?.paper_total_pnl||0) < 0 ? '-' : '+'}$${Math.abs(stats?.paper_total_pnl||0).toLocaleString(undefined,{maximumFractionDigits:0})}`} tone="blue"/>
              <StatCard icon={Sliders} label="Total Live Trades" value={(stats?.live_trade_count ?? 0).toLocaleString()} sub={`Platform P&L: ${(stats?.live_total_pnl||0) < 0 ? '-' : '+'}$${Math.abs(stats?.live_total_pnl||0).toLocaleString(undefined,{maximumFractionDigits:0})}`} tone={(stats?.live_total_pnl||0) >= 0 ? 'green' : 'red'}/>
            </div>

            <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-5">
              <h3 className="text-sm font-bold text-slate-700 dark:text-slate-200 mb-4">Users by Tier</h3>
              <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
                {TIER_ORDER.map(t => {
                  const count = (stats?.tiers || {})[t] ?? 0
                  return (
                    <div key={t} className={`rounded-xl p-4 ${TIER_COLORS[t]}`}>
                      <div className="text-3xl font-extrabold tabular-nums">{count}</div>
                      <div className="text-[11px] font-bold uppercase tracking-wider opacity-80 mt-1">{TIER_LABELS[t]}</div>
                    </div>
                  )
                })}
              </div>
            </div>
          </>)}

          {tab === 'users' && (<>
            <SectionHeader title="Users" subtitle={`${users.length} total · ${verifiedCount} verified · ${paying} paying`}
              action={
                <div className="relative w-72">
                  <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"/>
                  <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search by username or email…"
                    className="w-full border border-slate-300 dark:border-slate-700 dark:bg-slate-900 rounded-lg pl-9 pr-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-violet-500"/>
                </div>
              }/>
            <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="bg-slate-50 dark:bg-slate-900/50 text-[11px] uppercase tracking-wider text-slate-500">
                    <tr>
                      <th className="px-4 py-3 text-left font-semibold">User</th>
                      <th className="px-4 py-3 text-left font-semibold">Tier</th>
                      <th className="px-4 py-3 text-left font-semibold">KYC</th>
                      <th className="px-4 py-3 text-left font-semibold">Country</th>
                      <th className="px-4 py-3 text-left font-semibold">Joined</th>
                      <th className="px-4 py-3 text-right font-semibold">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                    {filtered.map(u => (
                      <tr key={u.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/50">
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-3">
                            <div className="w-9 h-9 rounded-full bg-gradient-to-br from-blue-400 to-indigo-500 text-white flex items-center justify-center text-sm font-bold flex-shrink-0">
                              {(u.username?.[0] ?? '?').toUpperCase()}
                            </div>
                            <div className="min-w-0">
                              <div className="font-bold text-slate-900 dark:text-slate-100 truncate">{u.username}</div>
                              <div className="text-xs text-slate-500 dark:text-slate-400 truncate">{u.email}</div>
                            </div>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <select value={u.tier} onChange={e => changeTier(u.id, e.target.value)}
                            className="border border-slate-300 dark:border-slate-700 dark:bg-slate-800 rounded-lg px-2 py-1 text-xs">
                            {TIER_ORDER.map(t => <option key={t} value={t}>{TIER_LABELS[t]}</option>)}
                          </select>
                        </td>
                        <td className="px-4 py-3">
                          <span className={`text-[10px] font-bold uppercase tracking-wider px-2 py-1 rounded ${KYC_BADGE(u.kyc_status)}`}>
                            {u.kyc_status === 'not_started' ? 'Not Started' : u.kyc_status}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-xs font-mono text-slate-500">{u.country_code || '—'}</td>
                        <td className="px-4 py-3 text-xs text-slate-400">{new Date(u.created_at).toLocaleDateString()}</td>
                        <td className="px-4 py-3 text-right">
                          <div className="inline-flex items-center gap-1">
                            <button onClick={() => viewTrades(u.id)} className="p-1.5 rounded-lg hover:bg-blue-100 dark:hover:bg-blue-900/40 text-slate-400 hover:text-blue-600" title="View trades + legal acks"><Eye size={14}/></button>
                            <button onClick={() => setKycOverrideUser(u)} className="p-1.5 rounded-lg hover:bg-violet-100 dark:hover:bg-violet-900/40 text-slate-400 hover:text-violet-600" title="KYC override"><ShieldCheck size={14}/></button>
                            <button onClick={() => setCompTarget(u)} className="p-1.5 rounded-lg hover:bg-emerald-100 dark:hover:bg-emerald-900/40 text-slate-400 hover:text-emerald-600" title="Grant comp"><Gift size={14}/></button>
                            <button onClick={() => setDeleteConfirm(u.id)} className="p-1.5 rounded-lg hover:bg-red-100 dark:hover:bg-red-900/40 text-slate-400 hover:text-red-500" title="Delete user"><Trash2 size={14}/></button>
                          </div>
                        </td>
                      </tr>
                    ))}
                    {filtered.length === 0 && (
                      <tr><td colSpan={6} className="text-center py-10 text-sm text-slate-400">No users match this search.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </>)}

          {tab === 'kyc' && (<>
            <SectionHeader title="KYC Verification" subtitle="Identity verification status across all users. Use override for beta testers or compliance edge cases."/>
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
              {kycSummary.map((row: any) => (
                <div key={row.status} className={`rounded-xl border p-4 ${KYC_BADGE(row.status)}`}>
                  <div className="text-2xl font-extrabold tabular-nums">{row.n}</div>
                  <div className="text-[10px] font-bold uppercase tracking-wider mt-1 opacity-80">{row.status}</div>
                </div>
              ))}
              {kycSummary.length === 0 && <div className="col-span-5 text-sm text-slate-400 italic">No KYC data yet.</div>}
            </div>

            <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-5">
              <div className="flex items-center gap-2 mb-2">
                <ShieldCheck size={18} className="text-violet-600"/>
                <h3 className="text-base font-extrabold text-slate-900 dark:text-slate-100">Quick verify (per user)</h3>
              </div>
              <p className="text-xs text-slate-500 dark:text-slate-400 mb-3">Click the shield icon in the Users tab to override a specific user's KYC status. Each override is logged below with your admin email + reason.</p>
              <button onClick={() => setTab('users')} className="text-xs font-bold text-violet-600 hover:text-violet-700 inline-flex items-center gap-1">
                Go to Users table <span>→</span>
              </button>
            </div>

            <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl overflow-hidden">
              <div className="px-5 py-4 border-b border-slate-200 dark:border-slate-800 flex items-center justify-between">
                <h3 className="text-base font-extrabold text-slate-900 dark:text-slate-100">Audit Log</h3>
                <span className="text-xs text-slate-400">{kycEvents.length} events shown</span>
              </div>
              {kycEvents.length === 0 ? (
                <div className="text-center py-10 text-sm text-slate-400 italic">No KYC events yet.</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead className="bg-slate-50 dark:bg-slate-900/50 text-[10px] uppercase tracking-wider text-slate-500">
                      <tr>
                        <th className="px-4 py-2.5 text-left">When</th>
                        <th className="px-4 py-2.5 text-left">User</th>
                        <th className="px-4 py-2.5 text-left">Event</th>
                        <th className="px-4 py-2.5 text-left">Status</th>
                        <th className="px-4 py-2.5 text-left">Provider</th>
                        <th className="px-4 py-2.5 text-left">Country</th>
                        <th className="px-4 py-2.5 text-left">Detail</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                      {kycEvents.map((e: any) => (
                        <tr key={e.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                          <td className="px-4 py-2 whitespace-nowrap font-mono text-[10px] text-slate-500">{new Date(e.created_at).toLocaleString()}</td>
                          <td className="px-4 py-2">
                            <div className="font-semibold text-slate-900 dark:text-slate-100">{e.username || '—'}</div>
                            <div className="text-[10px] text-slate-400">{e.user_email}</div>
                          </td>
                          <td className="px-4 py-2 text-slate-600 dark:text-slate-300">{e.event_type}</td>
                          <td className="px-4 py-2"><span className={`text-[9px] font-bold uppercase px-1.5 py-0.5 rounded ${KYC_BADGE(e.status)}`}>{e.status || 'n/a'}</span></td>
                          <td className="px-4 py-2 text-slate-500">{e.provider || '—'}</td>
                          <td className="px-4 py-2 font-mono text-slate-500">{e.country || e.country_code || '—'}</td>
                          <td className="px-4 py-2 text-slate-400 max-w-xs truncate" title={e.detail || ''}>{e.detail || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </>)}

          {tab === 'subscriptions' && (<>
            <SectionHeader title="Paying Subscriptions" subtitle="Live Stripe-backed customers."/>
            <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl overflow-hidden">
              {subscriptions.length === 0 ? <div className="text-center py-10 text-sm text-slate-400 italic">No paying subscriptions yet.</div> : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead className="bg-slate-50 dark:bg-slate-900/50 text-[10px] uppercase tracking-wider text-slate-500">
                      <tr>
                        <th className="px-4 py-2.5 text-left">User</th><th className="px-4 py-2.5 text-left">Tier</th>
                        <th className="px-4 py-2.5 text-left">Since</th><th className="px-4 py-2.5 text-left">Period ends</th>
                        <th className="px-4 py-2.5 text-left">Stripe ID</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                      {subscriptions.map(s => (
                        <tr key={s.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                          <td className="px-4 py-2 font-semibold text-slate-900 dark:text-slate-100">{s.email}</td>
                          <td className="px-4 py-2"><span className={`text-[10px] font-bold uppercase px-1.5 py-0.5 rounded ${TIER_COLORS[s.tier]}`}>{TIER_LABELS[s.tier] || s.tier}</span></td>
                          <td className="px-4 py-2 text-slate-500">{s.subscription_started_at ? new Date(s.subscription_started_at).toLocaleDateString() : '—'}</td>
                          <td className="px-4 py-2 text-slate-500">{s.subscription_ends_at ? new Date(s.subscription_ends_at).toLocaleDateString() : '—'}</td>
                          <td className="px-4 py-2 font-mono text-[10px] text-slate-400 truncate max-w-[180px]" title={s.stripe_subscription_id}>{s.stripe_subscription_id?.slice(0,18)}…</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </>)}

          {tab === 'comps' && (<>
            <SectionHeader title="Comped Accounts" subtitle="Users you've granted a free paid tier. Auto-expires unless renewed."/>
            <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl overflow-hidden">
              {comps.length === 0 ? <div className="text-center py-10 text-sm text-slate-400 italic">No active comps. Click the gift icon on any user to grant one.</div> : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead className="bg-slate-50 dark:bg-slate-900/50 text-[10px] uppercase tracking-wider text-slate-500">
                      <tr>
                        <th className="px-4 py-2.5 text-left">User</th><th className="px-4 py-2.5 text-left">Tier</th>
                        <th className="px-4 py-2.5 text-left">Granted</th><th className="px-4 py-2.5 text-left">Expires</th>
                        <th className="px-4 py-2.5 text-left">Days left</th><th className="px-4 py-2.5 text-left">Note</th>
                        <th className="px-4 py-2.5 text-right">Action</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                      {comps.map(c => (
                        <tr key={c.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                          <td className="px-4 py-2 font-semibold text-slate-900 dark:text-slate-100">{c.email}</td>
                          <td className="px-4 py-2"><span className={`text-[10px] font-bold uppercase px-1.5 py-0.5 rounded ${TIER_COLORS[c.tier]}`}>{TIER_LABELS[c.tier] || c.tier}</span></td>
                          <td className="px-4 py-2 text-slate-500">{c.granted_at ? new Date(c.granted_at).toLocaleDateString() : '—'}</td>
                          <td className="px-4 py-2 text-slate-500">{c.expires_at ? new Date(c.expires_at).toLocaleDateString() : '—'}</td>
                          <td className="px-4 py-2"><span className={`font-bold tabular-nums ${c.expired ? 'text-red-600' : c.days_left != null && c.days_left <= 3 ? 'text-amber-600' : 'text-slate-700 dark:text-slate-200'}`}>{c.expired ? 'Expired' : (c.days_left != null ? `${c.days_left}d` : '—')}</span></td>
                          <td className="px-4 py-2 text-slate-500 max-w-[200px] truncate" title={c.note || ''}>{c.note || '—'}</td>
                          <td className="px-4 py-2 text-right">
                            <button onClick={() => revokeComp(c.id)} className="px-2 py-1 rounded text-[10px] font-bold bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300 hover:bg-red-200">Revoke</button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </>)}

          {tab === 'systems' && (<>
            <SectionHeader title="Systems Check" subtitle="Live status of every subsystem — scanners, emails, trading, integrations, infra."/>
            <SystemsCheck token={token}/>
          </>)}

          {tab === 'system' && (<>
            <SectionHeader title="System & Security" subtitle="Admin session, safe-word, and platform configuration."/>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-5">
                <div className="flex items-center gap-2 mb-3"><Lock size={18} className="text-violet-600"/><h3 className="font-extrabold text-slate-900 dark:text-slate-100">Admin session</h3></div>
                <p className="text-sm text-slate-500 dark:text-slate-400 mb-4">Your safe-word session is currently <span className="font-bold text-emerald-600">unlocked</span>. Lock it to require the safe-word again on the next action.</p>
                <button onClick={lockAdmin} className="bg-red-600 hover:bg-red-700 text-white font-bold text-sm px-4 py-2 rounded-lg">Lock admin now</button>
              </div>
              <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-5">
                <div className="flex items-center gap-2 mb-3"><ShieldAlert size={18} className="text-amber-600"/><h3 className="font-extrabold text-slate-900 dark:text-slate-100">Geo & VPN gate</h3></div>
                <p className="text-sm text-slate-500 dark:text-slate-400 mb-2">Powered by IPQualityScore. Non-US IPs, confirmed VPNs, Tor, and high-fraud-score connections are blocked at the middleware layer.</p>
                <p className="text-xs text-slate-400">Admin accounts bypass the gate for platform-management purposes.</p>
              </div>
            </div>
          </>)}
        </main>
      </div>

      {/* ── Modals ── */}
      {deleteConfirm && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white dark:bg-slate-900 rounded-2xl shadow-2xl max-w-sm w-full p-6 text-center">
            <Trash2 size={32} className="mx-auto text-red-500 mb-3"/>
            <h3 className="font-bold text-slate-900 dark:text-slate-100 mb-2">Delete User?</h3>
            <p className="text-sm text-slate-500 dark:text-slate-400 mb-5">This permanently removes this user and all their data.</p>
            <div className="flex gap-3">
              <button onClick={() => setDeleteConfirm(null)} className="flex-1 border border-slate-200 dark:border-slate-700 py-2.5 rounded-xl text-sm font-medium">Cancel</button>
              <button onClick={() => deleteUser(deleteConfirm)} className="flex-1 bg-red-600 hover:bg-red-700 text-white py-2.5 rounded-xl text-sm font-semibold">Delete</button>
            </div>
          </div>
        </div>
      )}

      {kycOverrideUser && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onClick={() => !kycBusy && setKycOverrideUser(null)}>
          <div onClick={e => e.stopPropagation()} className="bg-white dark:bg-slate-900 rounded-2xl shadow-2xl max-w-md w-full p-6">
            <div className="flex items-center gap-2 mb-1"><ShieldCheck size={20} className="text-violet-600"/><h3 className="font-extrabold text-slate-900 dark:text-slate-100">KYC override</h3></div>
            <p className="text-xs text-slate-500 dark:text-slate-400 mb-4">Manually set <strong>{kycOverrideUser.email}</strong>'s KYC status. The change + your reason are logged in the audit log.</p>
            <div className="space-y-3">
              <div>
                <label className="text-[10px] font-bold uppercase tracking-wider text-slate-500 block mb-1">New status</label>
                <select value={kycOverrideStatus} onChange={e => setKycOverrideStatus(e.target.value)} className="w-full px-3 py-2 border border-slate-300 dark:border-slate-700 dark:bg-slate-800 rounded-lg text-sm">
                  <option value="verified">verified</option>
                  <option value="not_started">not_started</option>
                  <option value="requires_input">requires_input</option>
                  <option value="failed">failed</option>
                </select>
              </div>
              <div>
                <label className="text-[10px] font-bold uppercase tracking-wider text-slate-500 block mb-1">Reason (audit log)</label>
                <input value={kycOverrideReason} onChange={e => setKycOverrideReason(e.target.value)} placeholder="e.g. beta tester / Stripe edge case"
                  className="w-full px-3 py-2 border border-slate-300 dark:border-slate-700 dark:bg-slate-800 rounded-lg text-sm"/>
              </div>
            </div>
            <div className="flex gap-2 justify-end mt-5">
              <button onClick={() => setKycOverrideUser(null)} disabled={kycBusy} className="px-4 py-2 border border-slate-300 dark:border-slate-700 rounded-lg text-sm font-semibold">Cancel</button>
              <button onClick={applyKycOverride} disabled={kycBusy} className="px-4 py-2 bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white rounded-lg text-sm font-bold">
                {kycBusy ? 'Applying…' : 'Apply override'}
              </button>
            </div>
          </div>
        </div>
      )}

      {compTarget && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onClick={() => !compBusy && setCompTarget(null)}>
          <div onClick={e => e.stopPropagation()} className="bg-white dark:bg-slate-900 rounded-2xl shadow-2xl max-w-md w-full p-6">
            <div className="flex items-center gap-2 mb-1"><Gift size={20} className="text-emerald-600"/><h3 className="font-extrabold text-slate-900 dark:text-slate-100">Comp a user</h3></div>
            <p className="text-xs text-slate-500 dark:text-slate-400 mb-4">Grant <strong>{compTarget.email}</strong> a free paid tier. Auto-downgrades to Tier 1 when it expires.</p>
            <div className="space-y-3">
              <div>
                <label className="text-[10px] font-bold uppercase tracking-wider text-slate-500 block mb-1">Tier</label>
                <select value={compForm.tier} onChange={e => setCompForm({...compForm, tier: e.target.value})} className="w-full px-3 py-2 border border-slate-300 dark:border-slate-700 dark:bg-slate-800 rounded-lg text-sm">
                  <option value="tier_2">Tier 2 — Futures Signals · $49/mo</option>
                  <option value="tier_3">Tier 3 — Options Scanner · $99/mo</option>
                  <option value="tier_4">Tier 4 — Options Live · $199/mo</option>
                  <option value="tier_5">Tier 5 — Fully Automated · $399/mo</option>
                </select>
              </div>
              <div>
                <label className="text-[10px] font-bold uppercase tracking-wider text-slate-500 block mb-1">Free for (days)</label>
                <input type="number" min={1} max={365} value={compForm.days} onChange={e => setCompForm({...compForm, days: parseInt(e.target.value) || 30})}
                  className="w-full px-3 py-2 border border-slate-300 dark:border-slate-700 dark:bg-slate-800 rounded-lg text-sm tabular-nums"/>
              </div>
              <div>
                <label className="text-[10px] font-bold uppercase tracking-wider text-slate-500 block mb-1">Internal note</label>
                <input value={compForm.note} onChange={e => setCompForm({...compForm, note: e.target.value})} placeholder="e.g. friend / beta tester"
                  className="w-full px-3 py-2 border border-slate-300 dark:border-slate-700 dark:bg-slate-800 rounded-lg text-sm"/>
              </div>
            </div>
            <div className="flex gap-2 justify-end mt-5">
              <button onClick={() => setCompTarget(null)} disabled={compBusy} className="px-4 py-2 border border-slate-300 dark:border-slate-700 rounded-lg text-sm font-semibold">Cancel</button>
              <button onClick={submitGrantComp} disabled={compBusy} className="px-4 py-2 bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white rounded-lg text-sm font-bold">
                {compBusy ? 'Granting…' : 'Grant comp'}
              </button>
            </div>
          </div>
        </div>
      )}

      {viewingTrades && tradingUser && (() => {
        const totalNet = userTrades.reduce((s: number, t: any) => s + (t.net_pnl ?? 0), 0)
        const wins = userTrades.filter((t: any) => (t.net_pnl ?? 0) > 0).length
        const losses = userTrades.filter((t: any) => (t.net_pnl ?? 0) < 0).length
        const winRate = (wins + losses) > 0 ? (wins / (wins + losses)) * 100 : 0
        return (
          <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
            <div className="bg-white dark:bg-slate-900 rounded-2xl shadow-2xl w-full max-w-6xl max-h-[88vh] flex flex-col">
              <div className="flex items-start justify-between px-6 py-4 border-b border-slate-200 dark:border-slate-700">
                <div className="flex items-center gap-3 min-w-0">
                  <div className="w-12 h-12 rounded-full bg-gradient-to-br from-blue-400 to-indigo-500 text-white flex items-center justify-center text-base font-bold flex-shrink-0">
                    {(tradingUser?.username?.[0] ?? '?').toUpperCase()}
                  </div>
                  <div className="min-w-0">
                    <h3 className="font-extrabold text-lg text-slate-900 dark:text-slate-100 truncate">{tradingUser?.username}</h3>
                    <div className="text-xs text-slate-500 dark:text-slate-400 truncate">{tradingUser?.email} · {TIER_LABELS[tradingUser?.tier]}</div>
                  </div>
                </div>
                <button onClick={() => { setViewingTrades(null); setTradeFilter('') }} className="p-1.5 rounded-lg hover:bg-slate-200 dark:hover:bg-slate-700"><X size={18}/></button>
              </div>
              <div className="px-6 py-3 grid grid-cols-2 sm:grid-cols-4 gap-3 border-b border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-950/40">
                <div><div className="text-[10px] uppercase font-bold tracking-wider text-slate-400">Net P&L</div><div className={`text-xl font-extrabold mt-0.5 ${totalNet >= 0 ? 'text-emerald-600' : 'text-red-500'}`}>{totalNet >= 0 ? '+' : ''}${totalNet.toLocaleString(undefined, {maximumFractionDigits: 2})}</div></div>
                <div><div className="text-[10px] uppercase font-bold tracking-wider text-slate-400">Total Trades</div><div className="text-xl font-extrabold mt-0.5">{userTrades.length}</div></div>
                <div><div className="text-[10px] uppercase font-bold tracking-wider text-slate-400">Win Rate</div><div className="text-xl font-extrabold mt-0.5">{winRate.toFixed(1)}%</div></div>
                <div><div className="text-[10px] uppercase font-bold tracking-wider text-slate-400">Wins / Losses</div><div className="text-xl font-extrabold mt-0.5"><span className="text-emerald-600">{wins}</span> <span className="text-slate-300">/</span> <span className="text-red-500">{losses}</span></div></div>
              </div>
              <div className="px-6 border-b border-slate-200 dark:border-slate-700 flex gap-2">
                {([{id:'trades' as const,label:`Trades (${userTrades.length})`},{id:'acks' as const,label:`Acknowledgments (${userAcks.length})`}]).map(t => (
                  <button key={t.id} onClick={() => setDetailTab(t.id)}
                    className={`px-3 py-2.5 text-xs font-bold uppercase tracking-widest -mb-px border-b-2 ${detailTab === t.id ? 'border-violet-600 text-violet-600' : 'border-transparent text-slate-500 hover:text-slate-800'}`}>{t.label}</button>
                ))}
              </div>
              {detailTab === 'acks' ? (
                <div className="flex-1 overflow-auto">
                  {userAcks.length === 0 ? <p className="text-sm text-slate-400 text-center py-12">No acknowledgments yet.</p> : (
                    <table className="w-full text-sm">
                      <thead className="sticky top-0 bg-slate-100 dark:bg-slate-800"><tr className="text-[10px] text-slate-500 uppercase tracking-wider">
                        <th className="py-2 px-3 text-left">When</th><th className="py-2 px-3 text-left">Document</th>
                        <th className="py-2 px-3 text-left">Version</th><th className="py-2 px-3 text-left">Detail</th>
                        <th className="py-2 px-3 text-left">IP</th><th className="py-2 px-3 text-left">UA</th>
                      </tr></thead>
                      <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                        {userAcks.map((a: any) => (
                          <tr key={a.id}>
                            <td className="py-2 px-3 font-mono text-xs">{a.agreed_at ? new Date(a.agreed_at).toISOString().slice(0,19).replace('T',' ') : '—'}</td>
                            <td className="py-2 px-3 font-bold">{ACK_KIND_LABELS[a.kind] || a.kind}</td>
                            <td className="py-2 px-3"><span className="text-[10px] font-bold bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300 px-1.5 py-0.5 rounded">{a.content_version}</span></td>
                            <td className="py-2 px-3 text-xs max-w-[280px] truncate" title={a.detail || ''}>{a.detail || '—'}</td>
                            <td className="py-2 px-3 font-mono text-xs text-slate-500">{a.ip_address || '—'}</td>
                            <td className="py-2 px-3 text-[10px] text-slate-400 max-w-[180px] truncate" title={a.user_agent || ''}>{a.user_agent || '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              ) : (<>
                <div className="px-6 py-3 border-b border-slate-200 dark:border-slate-700">
                  <div className="relative">
                    <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"/>
                    <input value={tradeFilter} onChange={e => setTradeFilter(e.target.value)} placeholder="Filter by symbol, direction, mode, reason…"
                      className="w-full border border-slate-300 dark:border-slate-700 dark:bg-slate-800 rounded-lg pl-9 pr-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-violet-500"/>
                  </div>
                </div>
                <div className="flex-1 overflow-auto">
                  {userTrades.length === 0 ? <p className="text-sm text-slate-400 text-center py-12">No trades found</p> : (
                    <table className="w-full text-sm">
                      <thead className="sticky top-0 bg-slate-100 dark:bg-slate-800"><tr className="text-[10px] text-slate-500 uppercase tracking-wider">
                        <th className="py-2 px-3 text-left">Entered</th><th className="py-2 px-3 text-left">Hold</th>
                        <th className="py-2 px-3 text-left">Symbol</th><th className="py-2 px-3 text-left">Side</th>
                        <th className="py-2 px-3 text-left">Entry</th><th className="py-2 px-3 text-left">Exit</th>
                        <th className="py-2 px-3 text-left">Net P&L</th><th className="py-2 px-3 text-left">Mode</th>
                        <th className="py-2 px-3 text-left">Reason</th>
                      </tr></thead>
                      <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                        {userTrades.filter((t: any) => {
                          if (!tradeFilter) return true
                          const q = tradeFilter.toLowerCase()
                          return (t.instrument ?? '').toLowerCase().includes(q) || (t.direction ?? '').toLowerCase().includes(q) || (t.mode ?? '').toLowerCase().includes(q) || (t.exit_reason ?? '').toLowerCase().includes(q)
                        }).map((t: any, i: number) => {
                          const p = t.net_pnl ?? 0
                          return (
                            <tr key={i}>
                              <td className="py-2 px-3 whitespace-nowrap text-slate-700 dark:text-slate-200">{fmtEntryTime(t.entry_time)}</td>
                              <td className="py-2 px-3 whitespace-nowrap text-slate-500">{fmtHold(t.entry_time, t.exit_time)}</td>
                              <td className="py-2 px-3 font-bold">{t.instrument ?? '—'}</td>
                              <td className="py-2 px-3"><span className={`text-[10px] font-bold uppercase px-1.5 py-0.5 rounded ${t.direction === 'long' ? 'bg-emerald-100 text-emerald-700' : 'bg-red-100 text-red-700'}`}>{(t.direction ?? '—').toUpperCase()}</span></td>
                              <td className="py-2 px-3">{t.entry_price != null ? t.entry_price.toFixed(2) : '—'}</td>
                              <td className="py-2 px-3">{t.exit_price != null ? t.exit_price.toFixed(2) : 'Open'}</td>
                              <td className={`py-2 px-3 font-bold ${p > 0 ? 'text-emerald-600' : p < 0 ? 'text-red-500' : 'text-slate-400'}`}>{p >= 0 ? '+' : ''}${p.toFixed(2)}</td>
                              <td className="py-2 px-3 capitalize text-slate-600">{t.mode}</td>
                              <td className="py-2 px-3 text-xs text-slate-400">{t.exit_reason ?? '—'}</td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  )}
                </div>
              </>)}
            </div>
          </div>
        )
      })()}
    </div>
  )
}
