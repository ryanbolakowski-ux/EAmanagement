import { Outlet, NavLink, useNavigate } from 'react-router-dom'
import {
  LayoutDashboard, TrendingUp, FlaskConical, Sliders,
  PlayCircle, Zap, LogOut, BarChart2, ChevronDown,
} from 'lucide-react'
import { useAuthStore } from '../../stores/authStore'

const navItems = [
  { to: '/app',             icon: LayoutDashboard, label: 'Dashboard'     },
  { to: '/app/strategies',  icon: TrendingUp,      label: 'Strategies'    },
  { to: '/app/backtests',   icon: FlaskConical,    label: 'Backtests'     },
  { to: '/app/optimization',icon: Sliders,         label: 'Optimization'  },
  { to: '/app/paper',       icon: PlayCircle,      label: 'Paper Trading' },
  { to: '/app/live',        icon: Zap,             label: 'Live Trading'  },
]

const TIER_LABELS: Record<string, string> = {
  free_trial: 'Free Trial',
  tier_1:     'Backtest',
  tier_3:     'Live Trader',
  tier_4:     'Advanced',
  tier_5:     'Enterprise',
}

export default function Layout() {
  const { user, logout } = useAuthStore()
  const navigate = useNavigate()

  return (
    <div className="flex h-screen bg-slate-50">
      {/* ── Sidebar ── */}
      <aside className="w-60 bg-white border-r border-slate-200 flex flex-col flex-shrink-0">
        {/* Logo */}
        <div className="px-5 py-5 border-b border-slate-100">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 bg-blue-600 rounded-lg flex items-center justify-center flex-shrink-0">
              <BarChart2 size={16} className="text-white"/>
            </div>
            <div>
              <div className="font-bold text-slate-900 text-sm leading-tight">Edge AM</div>
              <div className="text-[10px] text-slate-400 leading-tight">
                {TIER_LABELS[user?.subscription_tier ?? 'free_trial'] ?? 'Free Trial'}
              </div>
            </div>
          </div>
        </div>

        {/* Nav */}
        <nav className="flex-1 px-3 py-4 space-y-0.5 overflow-y-auto">
          {navItems.map(({ to, icon: Icon, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/app'}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all duration-150 ${
                  isActive
                    ? 'bg-blue-600 text-white shadow-sm shadow-blue-200'
                    : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
                }`
              }
            >
              <Icon size={16}/>
              {label}
            </NavLink>
          ))}
        </nav>

        {/* User */}
        <div className="px-4 py-4 border-t border-slate-100">
          <div className="flex items-center gap-2.5 mb-2">
            <div className="w-7 h-7 rounded-full bg-blue-100 text-blue-700 flex items-center justify-center text-xs font-bold flex-shrink-0">
              {user?.username?.[0]?.toUpperCase() ?? 'U'}
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-xs font-semibold text-slate-800 truncate">{user?.username}</div>
              <div className="text-[10px] text-slate-400 truncate">{user?.email}</div>
            </div>
          </div>
          <button
            onClick={() => { logout(); navigate('/') }}
            className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-red-500 transition-colors mt-1 w-full"
          >
            <LogOut size={12}/>
            Sign out
          </button>
        </div>
      </aside>

      {/* ── Main ── */}
      <main className="flex-1 overflow-auto">
        <Outlet/>
      </main>
    </div>
  )
}
