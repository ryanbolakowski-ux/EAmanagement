import { useState } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { useAuthStore } from '../../stores/authStore'
import { authApi } from '../../api/endpoints'
import { BarChart2, ArrowRight } from 'lucide-react'

export default function Login() {
  const [email, setEmail]       = useState('')
  const [password, setPassword] = useState('')
  const [error, setError]       = useState('')
  const [loading, setLoading]   = useState(false)
  const { setAuth } = useAuthStore()
  const navigate = useNavigate()

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true); setError('')
    try {
      const { data } = await authApi.login(email, password)
      const meRes    = await authApi.me()
      setAuth(meRes.data, data.access_token)
      navigate('/app')
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Invalid email or password.')
    } finally { setLoading(false) }
  }

  return (
    <div className="min-h-screen bg-slate-50 flex">
      {/* Left panel */}
      <div className="hidden lg:flex lg:w-1/2 bg-blue-600 p-12 flex-col justify-between">
        <Link to="/" className="flex items-center gap-2.5">
          <div className="w-8 h-8 bg-white/20 rounded-lg flex items-center justify-center">
            <BarChart2 size={16} className="text-white"/>
          </div>
          <span className="font-bold text-white text-lg">Edge Asset Management</span>
        </Link>
        <div>
          <blockquote className="text-white/90 text-xl font-medium leading-relaxed mb-4">
            "The platform that bridges the gap between strategy ideas and systematic execution."
          </blockquote>
          <div className="text-blue-200 text-sm">Designed for algorithmic futures traders.</div>
        </div>
        <div className="grid grid-cols-3 gap-4">
          {[['64%', 'Avg Win Rate'], ['2.4x', 'Profit Factor'], ['<8%', 'Max Drawdown']].map(([v, l]) => (
            <div key={l} className="bg-white/10 rounded-xl p-4">
              <div className="text-white font-bold text-xl">{v}</div>
              <div className="text-blue-200 text-xs mt-0.5">{l}</div>
            </div>
          ))}
        </div>
      </div>

      {/* Right panel */}
      <div className="flex-1 flex items-center justify-center p-8">
        <div className="w-full max-w-sm">
          <Link to="/" className="flex items-center gap-2 mb-8 lg:hidden">
            <BarChart2 size={20} className="text-blue-600"/>
            <span className="font-bold text-slate-900">Edge AM</span>
          </Link>

          <h1 className="text-2xl font-extrabold text-slate-900 mb-1">Welcome back</h1>
          <p className="text-sm text-slate-500 mb-7">Sign in to your trading dashboard</p>

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">Email address</label>
              <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required
                className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm text-slate-900 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                placeholder="you@example.com"/>
            </div>
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">Password</label>
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required
                className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm text-slate-900 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                placeholder="••••••••"/>
            </div>

            {error && (
              <div className="bg-red-50 border border-red-200 text-red-700 text-sm px-3.5 py-2.5 rounded-lg">
                {error}
              </div>
            )}

            <button type="submit" disabled={loading}
              className="w-full flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white font-semibold py-2.5 rounded-lg text-sm transition-colors shadow-sm shadow-blue-200 mt-1">
              {loading ? 'Signing in...' : (<>Sign in <ArrowRight size={16}/></>)}
            </button>
          </form>

          <p className="text-center text-sm text-slate-500 mt-6">
            Don't have an account?{' '}
            <Link to="/register" className="text-blue-600 font-semibold hover:text-blue-700">
              Start free trial
            </Link>
          </p>
        </div>
      </div>
    </div>
  )
}
