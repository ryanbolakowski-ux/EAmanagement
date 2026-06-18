import { useEffect, useState } from 'react'
import { Outlet, NavLink, Link, useNavigate, useLocation } from 'react-router-dom'
import {
  LayoutDashboard, TrendingUp, FlaskConical, Sliders,
  PlayCircle, Zap, LogOut, Shield, BarChart2, BookOpen, Menu, X, Building2, Bell, LineChart, Globe, Sparkles, HelpCircle,
} from 'lucide-react'
import ThetaLogo from '../ThetaLogo'
import { useAuthStore } from '../../stores/authStore'
import { authApi } from '../../api/endpoints'
import ChatBubble from '../ChatBubble'
import AutomationStatusBadge from '../AutomationStatusBadge'
import { ENABLE_AI_CHAT } from '../../utils/featureFlags'
import { getDevicePref } from '../DevicePicker'

const traderNav: { to: string; icon: any; label: string }[] = [
  { to: '/app',             icon: LayoutDashboard, label: 'Dashboard'     },
  { to: '/app/strategies',  icon: TrendingUp,      label: 'Strategies'    },
  { to: '/app/plain-english', icon: Sparkles, label: 'Build in Plain English' },
  { to: '/app/how-to-trade',icon: BookOpen,        label: 'How To Trade'  },
  { to: '/app/backtests',   icon: FlaskConical,    label: 'Backtests'     },
  { to: '/app/optimization',icon: Sliders,         label: 'Optimization'  },
  { to: '/app/paper',       icon: PlayCircle,      label: 'Paper Trading' },
  { to: '/app/live',        icon: Zap,             label: 'Live Trading'  },
  { to: '/app/email-signals', icon: Bell, label: 'Email Signals' },]
const adminNav: { to: string; icon: any; label: string }[] = [
  { to: '/app/admin',       icon: Shield,          label: 'Admin Dashboard'  },
]

// Mobile bottom-nav: pick the 4 most-used destinations to keep tap targets large.
const mobileBottomNav = (isAdmin: boolean) => isAdmin
  ? adminNav
  : [
    traderNav[0],  // Dashboard
    traderNav[1],  // Strategies
    traderNav[6],  // Paper (was [5] which is Optimization)
    traderNav[8],  // Live (was [6] which is Paper)
  ]

export default function Layout() {
  const { user, logout, setAuth, token } = useAuthStore()
  const isAdmin = !!user?.is_admin
  const navItems = isAdmin ? adminNav : traderNav
  const navigate = useNavigate()
  const [drawerOpen, setDrawerOpen] = useState(false)
  // Re-render when device pref changes (DevicePicker swaps body class)
  const [pref, setPref] = useState<'browser' | 'mobile' | null>(() => getDevicePref())

  useEffect(() => {
    if (token && !user) {
      authApi.me().then(res => {
        setAuth(res.data, token!)
      }).catch(() => {
        logout()
        navigate('/')
      })
    }
  }, [])

  // ── Admin / KYC routing gates ──
  // 1. Admin accounts are STRICTLY admin — they cannot use any trader feature.
  //    They land on /app/admin and can only reach /app/profile for account settings.
  // 2. Non-admin users must complete KYC before accessing anything except /app/kyc
  //    and /app/profile.
  const location = useLocation()
  useEffect(() => {
    if (!user) return
    const isAdmin = !!(user as any).is_admin
    if (isAdmin) {
      // Admins: lock to /app/admin and /app/profile only
      const adminAllowed = location.pathname.startsWith('/app/admin') || location.pathname === '/app/profile'
      if (!adminAllowed) {
        navigate('/app/admin', { replace: true })
      }
      return  // admins skip KYC gate entirely
    }
    // Non-admin: enforce KYC
    const kyc = (user as any).kyc_status
    if (kyc === 'verified') return
    const allowed = ['/app/kyc', '/app/profile']
    if (!allowed.includes(location.pathname)) {
      navigate('/app/kyc', { replace: true })
    }
  }, [user, location.pathname])

  // Watch for body class flips (DevicePicker writes them on first pick).
  useEffect(() => {
    const obs = new MutationObserver(() => setPref(getDevicePref()))
    obs.observe(document.body, { attributes: true, attributeFilter: ['class'] })
    return () => obs.disconnect()
  }, [])

  const isMobile = pref === 'mobile'

  if (isMobile) {
    return (
      <div className="min-h-screen bg-slate-100 dark:bg-slate-950 flex flex-col">
        {/* ── Top bar ── */}
        <header className="sticky top-0 z-30 bg-white dark:bg-slate-900 border-b border-slate-200 dark:border-slate-800 px-4 py-3 flex items-center justify-between">
          <Link to="/app" className="flex items-center gap-2.5">
            <ThetaLogo size={36} />
            <div className="leading-none">
              <div className="text-sm font-extrabold tracking-[0.15em] text-slate-900 dark:text-slate-100">
                THETA ALGOS
              </div>
              <div className="text-[8px] font-bold tracking-[0.22em] text-violet-600 dark:text-violet-400 mt-0.5">
                EST. 2026
              </div>
            </div>
          </Link>
          <button
            onClick={() => setDrawerOpen(true)}
            className="p-2 -mr-2 rounded-lg text-slate-600 dark:text-slate-300 hover:bg-violet-100/60 dark:hover:bg-violet-900/20"
          >
            <Menu size={22} />
          </button>
        </header>

        {/* ── Slide-out drawer (full nav) ── */}
        {drawerOpen && (
          <>
            <div className="fixed inset-0 z-40 bg-black/50" onClick={() => setDrawerOpen(false)} />
            <aside className="fixed top-0 right-0 bottom-0 z-50 w-72 bg-white dark:bg-slate-900 border-l border-slate-200 dark:border-slate-800 flex flex-col">
              <div className="px-4 py-4 border-b border-slate-200 dark:border-slate-800 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <div className="w-7 h-7 rounded-full bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 flex items-center justify-center text-xs font-bold">
                    {user?.username?.[0]?.toUpperCase() ?? 'U'}
                  </div>
                  <div className="min-w-0">
                    <div className="text-xs font-semibold text-slate-800 dark:text-slate-100 truncate">{user?.username}</div>
                    <div className="text-[10px] text-slate-400 dark:text-slate-500 truncate">{user?.email}</div>
                  </div>
                </div>
                <button onClick={() => setDrawerOpen(false)} className="p-1.5 rounded-lg text-slate-400 hover:bg-violet-100/60 dark:hover:bg-violet-900/20">
                  <X size={18}/>
                </button>
              </div>
              {!isAdmin && (
                <div className="px-4 pb-3">
                  <AutomationStatusBadge/>
                </div>
              )}
              <nav className="flex-1 px-3 py-3 space-y-1 overflow-y-auto">
                {navItems.map(({ to, icon: Icon, label }) => (
                  <NavLink
                    key={to}
                    to={to}
                    end={to === '/app'}
                    onClick={() => setDrawerOpen(false)}
                    className={({ isActive }) =>
                      `flex items-center gap-3 px-3 py-3 rounded-lg text-base font-medium transition ${
                        isActive
                          ? 'bg-gradient-to-r from-violet-600 to-violet-700 text-white shadow-md shadow-violet-300/50 dark:shadow-violet-900/40'
                          : 'text-slate-700 dark:text-slate-200 hover:bg-violet-100/60 dark:hover:bg-violet-900/20'
                      }`
                    }
                  >
                    <Icon size={18}/>
                    {label}
                  </NavLink>
                ))}
                <NavLink
                  to="/app/profile"
                  onClick={() => setDrawerOpen(false)}
                  className={({ isActive }) =>
                    `flex items-center gap-3 px-3 py-3 rounded-lg text-base font-medium transition ${
                      isActive ? 'bg-gradient-to-r from-violet-600 to-violet-700 text-white shadow-md shadow-violet-300/50 dark:shadow-violet-900/40' : 'text-slate-700 dark:text-slate-200 hover:bg-violet-100/60 dark:hover:bg-violet-900/20'
                    }`
                  }
                >
                  <Shield size={18}/>
                  Profile
                </NavLink>
                <Link
                  to="/help"
                  onClick={() => setDrawerOpen(false)}
                  className="flex items-center gap-3 px-3 py-3 rounded-lg text-base font-medium text-slate-700 dark:text-slate-200 hover:bg-violet-100/60 dark:hover:bg-violet-900/20"
                >
                  <HelpCircle size={18}/>
                  Help & FAQ
                </Link>
              </nav>
              <div className="px-3 py-3 border-t border-slate-200 dark:border-slate-800">
                <button
                  onClick={() => { logout(); navigate('/') }}
                  className="w-full flex items-center gap-2 px-3 py-2.5 rounded-lg text-sm text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20"
                >
                  <LogOut size={14}/>
                  Sign out
                </button>
              </div>
            </aside>
          </>
        )}

        {/* ── Main content ── */}
        <main className="flex-1 overflow-auto p-3 pb-24">
          <Outlet/>
        </main>

        {/* ── Bottom nav ── */}
        <nav className="fixed bottom-0 left-0 right-0 z-30 bg-white dark:bg-slate-900 border-t border-slate-200 dark:border-slate-800 grid grid-cols-4">
          {mobileBottomNav(isAdmin).map(({ to, icon: Icon, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/app'}
              className={({ isActive }) =>
                `flex flex-col items-center gap-1 py-2.5 text-[10px] font-medium ${
                  isActive ? 'text-violet-600 dark:text-violet-400' : 'text-slate-500 dark:text-slate-400'
                }`
              }
            >
              <Icon size={20}/>
              <span>{label.split(' ')[0]}</span>
            </NavLink>
          ))}
        </nav>

        {ENABLE_AI_CHAT && <ChatBubble/>}
      </div>
    )
  }

  // ── Default browser layout ──
  return (
    <div className="flex h-screen bg-slate-300 dark:bg-slate-950">
      <aside className="w-60 bg-slate-200 dark:bg-slate-900 border-r border-slate-300 dark:border-slate-800 flex flex-col flex-shrink-0 dark:border-slate-700">
        <div className="px-4 py-5 border-b border-slate-300 dark:border-slate-800 dark:border-slate-700">
          <Link to="/app" className="flex flex-col items-center gap-1.5">
            <ThetaLogo size={120} />
            <div className="text-center mt-1">
              <div className="text-base font-extrabold tracking-[0.18em] text-slate-900 dark:text-slate-100 leading-none">
                THETA ALGOS
              </div>
              <div className="text-[9px] font-bold tracking-[0.25em] text-violet-600 dark:text-violet-400 mt-1">
                EST. 2026
              </div>
            </div>
          </Link>
        </div>
        <nav className="flex-1 px-3 py-4 space-y-0.5 overflow-y-auto">
          {navItems.map(({ to, icon: Icon, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/app'}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all duration-150 ${
                  isActive
                    ? 'bg-blue-600 text-white shadow-sm shadow-blue-200 dark:shadow-none'
                    : 'text-slate-600 dark:text-slate-300 hover:bg-violet-100/60 dark:hover:bg-violet-900/20 hover:text-slate-900 dark:hover:text-white'
                }`
              }
            >
              <Icon size={16}/>
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="px-4 py-4 border-t border-slate-300 dark:border-slate-800 dark:border-slate-700">
          <div className="flex items-center gap-2.5 mb-2 cursor-pointer hover:bg-violet-100/60 dark:hover:bg-violet-900/20 rounded-lg p-1 -m-1 transition-colors" onClick={() => navigate('/app/profile')}>
            <div className="w-7 h-7 rounded-full bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 flex items-center justify-center text-xs font-bold flex-shrink-0">
              {user?.username?.[0]?.toUpperCase() ?? 'U'}
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-xs font-semibold text-slate-800 dark:text-slate-100 truncate">{user?.username}</div>
              <div className="text-[10px] text-slate-400 dark:text-slate-500 truncate">{user?.email}</div>
            </div>
          </div>
          {!isAdmin && (
            <div className="mb-2">
              <AutomationStatusBadge/>
            </div>
          )}
          <Link
            to="/help"
            className="flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400 hover:text-violet-600 dark:hover:text-violet-400 transition-colors w-full"
          >
            <HelpCircle size={12}/>
            Help & FAQ
          </Link>
          <button
            onClick={() => { logout(); navigate('/') }}
            className="flex items-center gap-1.5 text-xs text-slate-400 dark:text-slate-500 hover:text-red-500 transition-colors mt-1 w-full"
          >
            <LogOut size={12}/>
            Sign out
          </button>
        </div>
      </aside>
      <main className="flex-1 overflow-auto">
        <Outlet/>
      </main>
      {ENABLE_AI_CHAT && <ChatBubble/>}
    </div>
  )
}
