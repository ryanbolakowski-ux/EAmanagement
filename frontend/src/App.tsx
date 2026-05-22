import { Routes, Route, Navigate } from 'react-router-dom'
import { useAuthStore } from './stores/authStore'
import Layout from './components/Layout/Layout'
import Landing from './pages/Landing'
import NotAvailable from './pages/NotAvailable'
import Kyc from './pages/Kyc'
import Profile from './pages/Profile'
import Pricing from './pages/Pricing'
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
import OptionsSessions from './pages/OptionsSessions'
import OptionsSessionDetail from './pages/OptionsSessionDetail'
import PendingTrades from './pages/PendingTrades'
import PendingTradeConfirm from './pages/PendingTradeConfirm'
import Options from './pages/Options'
import DevicePicker from './components/DevicePicker'

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

export default function App() {
  return (
    <>
    <DevicePicker />
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
      <Route path="/onboarding"      element={<OnboardingWizard />} />
      <Route path="/reset-password"  element={<ResetPassword />} />
      <Route path="/not-available"  element={<NotAvailable />} />
      <Route path="/shared/:token" element={<SharedStrategy />} />

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
      </Route>

      {/* Fallback */}
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
    </>
  )
}
