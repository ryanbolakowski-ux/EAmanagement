import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { useThemeStore } from './stores/themeStore'
import { lazy, Suspense, useEffect } from 'react'
import { Loader2 } from 'lucide-react'
import { useAuthStore } from './stores/authStore'
import Layout from './components/Layout/Layout'
// NotAvailable stays eager: it is the 451 redirect target and must render
// even when chunk fetches fail (e.g. geo-blocked CDN), so it can never be lazy.
import NotAvailable from './pages/NotAvailable'
import TwoFactorRequiredModal from './components/TwoFactorRequiredModal'
import DevicePicker from './components/DevicePicker'
import VersionBanner from './components/VersionBanner'
import SuggestionForm from './components/SuggestionForm'
import { Skeleton, ToastProvider } from './components/v2'

// V1 pages are code-split: each route downloads only its own chunk instead of
// every visitor paying for the whole app in one bundle. Every page file uses
// a default export, so plain lazy(() => import(...)) works throughout.
const Landing = lazy(() => import('./pages/Landing'))
const Kyc = lazy(() => import('./pages/Kyc'))
const Profile = lazy(() => import('./pages/Profile'))
const Pricing = lazy(() => import('./pages/Pricing'))
const SignalReview = lazy(() => import('./pages/SignalReview'))
const Dashboard = lazy(() => import('./pages/Dashboard'))
const StrategyBuilder = lazy(() => import('./pages/StrategyBuilder'))
const BiasDetail = lazy(() => import('./pages/BiasDetail'))
const Backtests = lazy(() => import('./pages/Backtests'))
const Optimization = lazy(() => import('./pages/Optimization'))
const PaperTrading = lazy(() => import('./pages/PaperTrading'))
const PaperSessionDetail = lazy(() => import('./pages/PaperSessionDetail'))
const LiveTrading = lazy(() => import('./pages/LiveTrading'))
const LiveTradingV2 = lazy(() => import('./pages/LiveTradingV2'))
const Privacy = lazy(() => import('./pages/legal/Privacy'))
const Terms = lazy(() => import('./pages/legal/Terms'))
const Disclosures = lazy(() => import('./pages/legal/Disclosures'))
const Cookies = lazy(() => import('./pages/legal/Cookies'))
const Help = lazy(() => import('./pages/Help'))
const OnboardingWizard = lazy(() => import('./pages/OnboardingWizard'))
const LiveAccountDetail = lazy(() => import('./pages/LiveAccountDetail'))
const Login = lazy(() => import('./pages/Auth/Login'))
const Register = lazy(() => import('./pages/Auth/Register'))
const ForgotPassword = lazy(() => import('./pages/Auth/ForgotPassword'))
const ResetPassword = lazy(() => import('./pages/Auth/ResetPassword'))
const Admin = lazy(() => import('./pages/Admin'))
const HowToTrade = lazy(() => import('./pages/HowToTrade'))
const PropFirms = lazy(() => import('./pages/PropFirms'))
const SharedStrategy = lazy(() => import('./pages/SharedStrategy'))
const AIStrategyBuilder = lazy(() => import('./pages/AIStrategyBuilder'))
const AccountSignals = lazy(() => import('./pages/AccountSignals'))
const TwoFactorSetup = lazy(() => import('./pages/TwoFactorSetup'))
const OptionsSessions = lazy(() => import('./pages/OptionsSessions'))
const OptionsSessionDetail = lazy(() => import('./pages/OptionsSessionDetail'))
const PendingTrades = lazy(() => import('./pages/PendingTrades'))
const PendingTradeConfirm = lazy(() => import('./pages/PendingTradeConfirm'))
const Options = lazy(() => import('./pages/Options'))
const Replay = lazy(() => import('./pages/Replay'))
const BacktestLab = lazy(() => import('./pages/BacktestLab'))

// V2 redesign pages are lazy so their chunk (and only theirs) loads on the
// /v2 routes — V1 users never download it. See pages/v2/ for the screens.
const LandingV2 = lazy(() => import('./pages/v2/LandingV2'))
const DashboardV2 = lazy(() => import('./pages/v2/DashboardV2'))

function PageLoadFallback() {
  // Shared Suspense fallback while a lazy V1 page chunk downloads: the same
  // centered-Loader2 pattern the pages themselves use (see Kyc.tsx), so the
  // brief flash stays in the V1 look — deliberately not a v2 component.
  return (
    <div className="min-h-screen flex items-center justify-center">
      <Loader2 className="animate-spin text-slate-400" />
    </div>
  )
}

function V2PageFallback() {
  // Suspense fallback while a lazy V2 chunk downloads: a v2-root shell with
  // skeleton blocks, so the route paints in the V2 design language instantly.
  return (
    <div className="v2-root v2-page">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-6 space-y-4">
        <Skeleton width={220} height={28} />
        <Skeleton variant="card" />
        <Skeleton variant="table" />
      </div>
    </div>
  )
}

function V2Route({ children }: { children: React.ReactNode }) {
  // Shared wrapper for V2 routes only: lazy-chunk Suspense + the V2 toast
  // system. Mounted per-route (not app-wide) so the V1 render tree keeps
  // byte-for-byte identical semantics.
  return (
    <Suspense fallback={<V2PageFallback />}>
      <ToastProvider>{children}</ToastProvider>
    </Suspense>
  )
}

function AuthenticatedOnly({ children }: { children: React.ReactNode }) {
  // Only render children if user has a JWT in localStorage. Pre-auth pages
  // (login, register, forgot-password) should never see DevicePicker etc.
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  return isAuthenticated ? <>{children}</> : null
}

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  return isAuthenticated ? <>{children}</> : <Navigate to="/login" replace />
}

// PUBLIC-LIGHT GUARD (Ryan 2026-07-03): dark mode does not exist outside the
// app. Public/marketing routes always render light regardless of the visitor's
// OS or a logged-in user's stored theme; the stored theme re-applies the
// moment the route enters /app. (All existing light styling is keyed on
// html:not(.dark), so stripping the class flips everything consistently.)
function PublicLightGuard() {
  const { pathname } = useLocation()
  const theme = useThemeStore((s) => s.theme)
  useEffect(() => {
    const inApp = pathname === '/app' || pathname.startsWith('/app/')
    const root = document.documentElement
    if (!inApp) root.classList.remove('dark')
    else if (theme === 'dark') root.classList.add('dark')
    else root.classList.remove('dark')
  }, [pathname, theme])
  return null
}

function AdminAwareIndex() {
  // Admins always land on /app/admin instead of the trader Dashboard.
  const user = useAuthStore((s) => s.user)
  if ((user as any)?.is_admin) return <Navigate to="/app/admin" replace />
  // V2 dashboard is the default trader experience; V1 stays at /app/classic.
  return <V2Route><DashboardV2 /></V2Route>
}

const _TITLES: Record<string, string> = {
  "/": "Theta Algos - Algorithmic Trading",
  "/pricing": "Pricing - Theta Algos",
  "/login": "Sign In - Theta Algos",
  "/register": "Create Account - Theta Algos",
  "/forgot-password": "Reset Password - Theta Algos",
  "/privacy": "Privacy Policy - Theta Algos",
  "/terms": "Terms of Service - Theta Algos",
  "/disclosures": "Disclosures - Theta Algos",
  "/cookies": "Cookie Policy - Theta Algos",
  "/help": "Help & FAQ - Theta Algos",
  "/faq": "Help & FAQ - Theta Algos",
  "/app": "Dashboard - Theta Algos",
  "/app/strategies": "Strategy Builder - Theta Algos",
  "/app/plain-english": "Plain-English Builder - Theta Algos",
  "/app/how-to-trade": "How To Trade - Theta Algos",
  "/app/backtests": "Backtests - Theta Algos",
  "/app/backtest-lab": "Backtest Lab - Theta Algos",
  "/app/optimization": "Optimization - Theta Algos",
  "/app/paper": "Paper Trading - Theta Algos",
  "/app/replay": "Replay - Theta Algos",
  "/app/live": "Live Trading - Theta Algos",
  "/app/email-signals": "Email Signals - Theta Algos",
  "/app/account-signals": "Email Signals - Theta Algos",
  "/app/options": "Options - Theta Algos",
  "/app/bias": "Daily Bias - Theta Algos",
  "/app/profile": "Profile - Theta Algos",
  "/app/settings/2fa": "Set up 2FA - Theta Algos",
  "/app/kyc": "Identity Verification - Theta Algos",
  "/app/prop-firms": "Prop Firms - Theta Algos",
  "/app/admin": "Admin - Theta Algos",
}

function RouteTitle() {
  const { pathname } = useLocation()
  useEffect(() => {
    // exact match, else longest known prefix, else brand default
    let title = _TITLES[pathname]
    if (!title) {
      const hit = Object.keys(_TITLES)
        .filter((k) => k !== "/" && pathname.startsWith(k))
        .sort((a, b) => b.length - a.length)[0]
      title = hit ? _TITLES[hit] : "Theta Algos - Algorithmic Trading"
    }
    document.title = title
  }, [pathname])
  return null
}


export default function App() {
  return (
    <>
    <AuthenticatedOnly>
      <DevicePicker />
    </AuthenticatedOnly>
    <VersionBanner />
    <AuthenticatedOnly>
      <TwoFactorRequiredModal />
    </AuthenticatedOnly>
    <RouteTitle />
    <AuthenticatedOnly>
      <SuggestionForm />
    </AuthenticatedOnly>
    {/* One shared Suspense boundary for every lazy V1 page chunk. V2 routes
        keep their own nested boundary (V2Route above), so they still fall
        back to the V2 skeleton shell rather than this spinner. */}
    <Suspense fallback={<PageLoadFallback />}>
    <>
    <PublicLightGuard />
    <Routes>
      {/* Public routes */}
      <Route path="/"         element={<V2Route><LandingV2 /></V2Route>} />
      {/* V1 landing kept during the V2 transition (Ryan 2026-07-03: "make everything v2") */}
      <Route path="/classic"  element={<Landing />} />
      <Route path="/pricing"  element={<Pricing />} />
      <Route path="/login"           element={<Login />} />
      <Route path="/register"        element={<Register />} />
      <Route path="/forgot-password" element={<ForgotPassword />} />
      <Route path="/privacy"         element={<Privacy />} />
      <Route path="/terms"           element={<Terms />} />
      <Route path="/disclosures"     element={<Disclosures />} />
      <Route path="/cookies"         element={<Cookies />} />
      <Route path="/help"            element={<Help />} />
      <Route path="/faq"             element={<Navigate to="/help" replace />} />
      <Route path="/onboarding"      element={<OnboardingWizard />} />
      <Route path="/reset-password"  element={<ResetPassword />} />
      <Route path="/not-available"  element={<NotAvailable />} />
      <Route path="/shared/:token" element={<SharedStrategy />} />

      {/* V2 redesign — public landing (lazy chunk, V2Route wrapper above) */}
      <Route path="/v2" element={<V2Route><LandingV2 /></V2Route>} />

      {/* Protected app routes */}
      <Route
        path="/app"
        element={
          <ProtectedRoute>
            <Layout />
          </ProtectedRoute>
        }
      >
        <Route index                element={<AdminAwareIndex />} />
        <Route path="classic"       element={<Dashboard />} />
        <Route path="bias"          element={<BiasDetail />} />
        <Route path="strategies"    element={<StrategyBuilder />} />
        <Route path="how-to-trade"  element={<HowToTrade />} />
        <Route path="strategies/shared/:token" element={<SharedStrategy />} />
        <Route path="plain-english" element={<AIStrategyBuilder />} />
        <Route path="prop-firms" element={<PropFirms />} />
        <Route path="account-signals" element={<AccountSignals />} />
        <Route path="email-signals" element={<AccountSignals />} />
        <Route path="signals/:id/review" element={<SignalReview />} />
        <Route path="options" element={<Options />} />
        <Route path="options/sessions" element={<OptionsSessions />} />
        <Route path="options/sessions/:id" element={<OptionsSessionDetail />} />
        <Route path="options/pending" element={<PendingTrades />} />
        <Route path="pending/:token" element={<PendingTradeConfirm />} />
        <Route path="how-to-trade/:id" element={<HowToTrade />} />
        <Route path="backtests"     element={<Backtests />} />
        <Route path="backtest-lab"  element={<BacktestLab />} />
        <Route path="optimization"  element={<Optimization />} />
        <Route path="paper"             element={<PaperTrading />} />
        <Route path="paper/:id"         element={<PaperSessionDetail />} />
        <Route path="replay"            element={<Replay />} />
        <Route path="admin"             element={<Admin />} />
        <Route path="live"              element={<LiveTradingV2 />} />
        <Route path="live/classic"      element={<LiveTrading />} />
        <Route path="live/:id"          element={<LiveAccountDetail />} />
        <Route path="kyc"               element={<Kyc />} />
        <Route path="profile"           element={<Profile />} />
        <Route path="settings/2fa"      element={<TwoFactorSetup />} />
        {/* V2 redesign — dashboard. Child of /app so it inherits the exact
            same ProtectedRoute + Layout guard as every other app screen. */}
        <Route path="v2"                element={<V2Route><DashboardV2 /></V2Route>} />
      </Route>

      {/* Fallback */}
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
    </>
    </Suspense>
    </>
  )
}
