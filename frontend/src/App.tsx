import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { lazy, Suspense, useEffect } from 'react'
import { useAuthStore } from './stores/authStore'
import Layout from './components/Layout/Layout'
import Landing from './pages/Landing'
import NotAvailable from './pages/NotAvailable'
import Kyc from './pages/Kyc'
import Profile from './pages/Profile'
import Pricing from './pages/Pricing'
import SignalReview from './pages/SignalReview'
import Dashboard from './pages/Dashboard'
import StrategyBuilder from './pages/StrategyBuilder'
import BiasDetail from './pages/BiasDetail'
import Backtests from './pages/Backtests'
import Optimization from './pages/Optimization'
import PaperTrading from './pages/PaperTrading'
import PaperSessionDetail from './pages/PaperSessionDetail'
import LiveTrading from './pages/LiveTrading'
import LiveTradingV2 from './pages/LiveTradingV2'
import Privacy from './pages/legal/Privacy'
import Terms from './pages/legal/Terms'
import Disclosures from './pages/legal/Disclosures'
import Cookies from './pages/legal/Cookies'
import Help from './pages/Help'
import OnboardingWizard from './pages/OnboardingWizard'
import LiveAccountDetail from './pages/LiveAccountDetail'
import Login from './pages/Auth/Login'
import Register from './pages/Auth/Register'
import ForgotPassword from './pages/Auth/ForgotPassword'
import ResetPassword from './pages/Auth/ResetPassword'
import Admin from './pages/Admin'
import HowToTrade from './pages/HowToTrade'
import PropFirms from './pages/PropFirms'
import SharedStrategy from './pages/SharedStrategy'
import AIStrategyBuilder from './pages/AIStrategyBuilder'
import AccountSignals from './pages/AccountSignals'
import TwoFactorSetup from './pages/TwoFactorSetup'
import TwoFactorRequiredModal from './components/TwoFactorRequiredModal'
import OptionsSessions from './pages/OptionsSessions'
import OptionsSessionDetail from './pages/OptionsSessionDetail'
import PendingTrades from './pages/PendingTrades'
import PendingTradeConfirm from './pages/PendingTradeConfirm'
import Options from './pages/Options'
import DevicePicker from './components/DevicePicker'
import VersionBanner from './components/VersionBanner'
import SuggestionForm from './components/SuggestionForm'
import { Skeleton, ToastProvider } from './components/v2'

// V2 redesign pages are lazy so their chunk (and only theirs) loads on the
// /v2 routes — V1 users never download it. See pages/v2/ for the screens.
const LandingV2 = lazy(() => import('./pages/v2/LandingV2'))
const DashboardV2 = lazy(() => import('./pages/v2/DashboardV2'))

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

function AdminAwareIndex() {
  // Admins always land on /app/admin instead of the trader Dashboard.
  const user = useAuthStore((s) => s.user)
  if ((user as any)?.is_admin) return <Navigate to="/app/admin" replace />
  return <Dashboard />
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
  "/app/optimization": "Optimization - Theta Algos",
  "/app/paper": "Paper Trading - Theta Algos",
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
    <Routes>
      {/* Public routes */}
      <Route path="/"         element={<Landing />} />
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
        <Route path="optimization"  element={<Optimization />} />
        <Route path="paper"             element={<PaperTrading />} />
        <Route path="paper/:id"         element={<PaperSessionDetail />} />
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
  )
}
