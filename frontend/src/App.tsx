import { Routes, Route, Navigate } from 'react-router-dom'
import { useAuthStore } from './stores/authStore'
import Layout from './components/Layout/Layout'
import Landing from './pages/Landing'
import Pricing from './pages/Pricing'
import Dashboard from './pages/Dashboard'
import StrategyBuilder from './pages/StrategyBuilder'
import Backtests from './pages/Backtests'
import Optimization from './pages/Optimization'
import PaperTrading from './pages/PaperTrading'
import LiveTrading from './pages/LiveTrading'
import Login from './pages/Auth/Login'
import Register from './pages/Auth/Register'

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  return isAuthenticated ? <>{children}</> : <Navigate to="/login" replace />
}

export default function App() {
  return (
    <Routes>
      {/* Public routes */}
      <Route path="/"         element={<Landing />} />
      <Route path="/pricing"  element={<Pricing />} />
      <Route path="/login"    element={<Login />} />
      <Route path="/register" element={<Register />} />

      {/* Protected app routes */}
      <Route
        path="/app"
        element={
          <ProtectedRoute>
            <Layout />
          </ProtectedRoute>
        }
      >
        <Route index                element={<Dashboard />} />
        <Route path="strategies"    element={<StrategyBuilder />} />
        <Route path="backtests"     element={<Backtests />} />
        <Route path="optimization"  element={<Optimization />} />
        <Route path="paper"         element={<PaperTrading />} />
        <Route path="live"          element={<LiveTrading />} />
      </Route>

      {/* Fallback */}
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
