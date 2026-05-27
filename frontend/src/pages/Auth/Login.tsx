import { useState } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { useAuthStore } from '../../stores/authStore'
import { authApi } from '../../api/endpoints'
import { ArrowRight, ShieldCheck, BarChart2, Eye, EyeOff } from 'lucide-react'
import ThetaLogo from '../../components/ThetaLogo'

export default function Login() {
  const [email, setEmail]       = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError]       = useState('')
  const [loading, setLoading]   = useState(false)
  const [challengeToken, setChallengeToken] = useState<string | null>(null)
  const [twoFACode, setTwoFACode] = useState('')
  const { setAuth } = useAuthStore()
  const navigate = useNavigate()

  const finishLogin = async (accessToken: string) => {
    localStorage.setItem('access_token', accessToken)
    const meRes = await authApi.me()
    setAuth(meRes.data, accessToken)
    const params = new URLSearchParams(window.location.search); const rt = params.get('returnTo'); navigate(rt && rt.startsWith('/') ? rt : '/app')
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true); setError('')
    try {
      const { data } = await authApi.login(email, password)
      if (data.requires_2fa && data.challenge_token) {
        setChallengeToken(data.challenge_token)
      } else if (data.access_token) {
        await finishLogin(data.access_token)
      } else {
        setError('Unexpected login response.')
      }
    } catch (err: any) {
      if (!err.response) { setError('Cannot reach API server. Try a hard refresh (Cmd+Shift+R on Mac, Ctrl+Shift+R on Windows). If that fails, clear site data for thetaalgos.com and reload.') } else if (err.response.status === 401) { setError('Wrong email or password.') } else { setError(err.response?.data?.detail || 'Login failed (HTTP ' + err.response.status + ').') }
    } finally { setLoading(false) }
  }

  const handle2FASubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!challengeToken) return
    setLoading(true); setError('')
    try {
      const { data } = await authApi.verify2FA(challengeToken, twoFACode.trim())
      await finishLogin(data.access_token)
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Invalid authentication code.')
    } finally { setLoading(false) }
  }

  return (
    <div className="min-h-screen flex bg-gradient-to-br from-violet-600 via-violet-700 to-indigo-700">
      {/* Left panel — black at top fading into the brand violet at the bottom */}
      <div className="hidden lg:flex lg:w-1/2 p-12 flex-col justify-between text-white relative overflow-hidden"
           style={{ background: 'linear-gradient(180deg, #000000 0%, #1e1b4b 35%, #4c1d95 70%, #6d28d9 100%)' }}>
        {/* Soft glow blobs */}
        <div className="absolute -top-32 -left-32 w-96 h-96 rounded-full bg-violet-600 opacity-20 blur-3xl pointer-events-none"/>
        <div className="absolute -bottom-32 -right-32 w-96 h-96 rounded-full bg-purple-500 opacity-25 blur-3xl pointer-events-none"/>

        <Link to="/" className="inline-flex flex-col items-start gap-3 relative">
          <ThetaLogo size={140} />
          <div>
            <div className="text-white font-extrabold text-4xl tracking-tight leading-none">Theta Algos</div>
            <div className="text-violet-200 text-[11px] font-bold tracking-[0.3em] mt-1.5">EST. 2026</div>
          </div>
        </Link>

        <div className="relative">
          <blockquote className="text-white text-xl font-medium leading-relaxed mb-4">
            "Quantitative precision meets algorithmic edge. Time decay advantage automated."
          </blockquote>
          <div className="text-violet-200 text-sm">Built for futures + options swing traders.</div>
        </div>

        <div className="grid grid-cols-3 gap-4 relative">
          {[['64%', 'Avg Win Rate'], ['2.4x', 'Profit Factor'], ['<8%', 'Max Drawdown']].map(([v, l]) => (
            <div key={l} className="rounded-xl p-4 bg-white/10 backdrop-blur-sm border border-white/20">
              <div className="text-white font-bold text-xl">{v}</div>
              <div className="text-violet-100 text-xs mt-0.5">{l}</div>
            </div>
          ))}
        </div>
      </div>

      {/* Right panel — solid violet, white text */}
      <div className="flex-1 flex items-center justify-center p-8 text-white">
        <div className="w-full max-w-sm">
          <Link to="/" className="flex flex-col items-center gap-2 mb-8 lg:hidden">
            <ThetaLogo size={80} />
            <div className="text-center">
              <div className="text-white font-extrabold text-2xl tracking-tight leading-none">Theta Algos</div>
              <div className="text-violet-200 text-[10px] font-bold tracking-[0.3em] mt-1">EST. 2026</div>
            </div>
          </Link>

          {!challengeToken ? (
            <>
              <h1 className="text-3xl font-extrabold text-white mb-1">Welcome back</h1>
              <p className="text-sm text-violet-100 mb-7">Sign in to your trading dashboard</p>

              <form onSubmit={handleSubmit} className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-white mb-1.5">Email address</label>
                  <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required
                    className="w-full bg-white/15 backdrop-blur-sm border border-white/30 rounded-lg px-3.5 py-2.5 text-sm text-white placeholder-violet-200 focus:outline-none focus:ring-2 focus:ring-white/50 focus:border-transparent"
                    placeholder="you@example.com"/>
                </div>
                <div>
                  <div className="flex items-center justify-between mb-1.5">
                    <label className="block text-sm font-medium text-white">Password</label>
                    <Link to="/forgot-password" className="text-xs font-medium text-violet-100 hover:text-white underline">
                      Forgot password?
                    </Link>
                  </div>
                  <div className="relative">
                    <input type={showPassword ? 'text' : 'password'} value={password} onChange={(e) => setPassword(e.target.value)} required
                      className="w-full bg-white/15 backdrop-blur-sm border border-white/30 rounded-lg px-3.5 py-2.5 pr-10 text-sm text-white placeholder-violet-200 focus:outline-none focus:ring-2 focus:ring-white/50 focus:border-transparent"
                      placeholder="••••••••"/>
                    <button type="button" onClick={() => setShowPassword(s => !s)}
                      aria-label={showPassword ? 'Hide password' : 'Show password'}
                      className="absolute inset-y-0 right-0 flex items-center px-3 text-violet-100 hover:text-white">
                      {showPassword ? <EyeOff size={16}/> : <Eye size={16}/>}
                    </button>
                  </div>
                </div>

                {error && (
                  <div className="bg-red-500/20 border border-red-300/40 text-red-100 text-sm px-3.5 py-2.5 rounded-lg backdrop-blur-sm">
                    {error}
                  </div>
                )}

                <button type="submit" disabled={loading}
                  className="w-full flex items-center justify-center gap-2 bg-white text-violet-700 hover:bg-violet-50 disabled:opacity-50 font-bold py-2.5 rounded-lg text-sm transition-colors shadow-lg shadow-black/20 mt-1">
                  {loading ? 'Signing in...' : (<>Sign in <ArrowRight size={16}/></>)}
                </button>
              </form>

              <p className="text-center text-sm text-violet-100 mt-6">
                Don't have an account?{' '}
                <Link to="/register" className="text-white font-bold hover:underline">
                  Start free trial
                </Link>
              </p>
            </>
          ) : (
            <>
              <div className="flex items-center gap-3 mb-1">
                <div className="w-9 h-9 rounded-xl bg-white/20 backdrop-blur-sm text-white flex items-center justify-center">
                  <ShieldCheck size={18}/>
                </div>
                <h1 className="text-xl font-extrabold text-white">Two-factor required</h1>
              </div>
              <p className="text-sm text-violet-100 mb-7">Open your authenticator app and enter the 6-digit code for Theta Algos.</p>

              <form onSubmit={handle2FASubmit} className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-white mb-1.5">Authentication code</label>
                  <input
                    type="text"
                    inputMode="numeric"
                    autoFocus
                    value={twoFACode}
                    onChange={(e) => setTwoFACode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                    required
                    className="w-full bg-white/15 backdrop-blur-sm border border-white/30 rounded-lg px-3.5 py-2.5 text-base font-mono tracking-[0.4em] text-center text-white placeholder-violet-200 focus:outline-none focus:ring-2 focus:ring-white/50 focus:border-transparent"
                    placeholder="123456"/>
                </div>

                {error && (
                  <div className="bg-red-500/20 border border-red-300/40 text-red-100 text-sm px-3.5 py-2.5 rounded-lg backdrop-blur-sm">
                    {error}
                  </div>
                )}

                <button type="submit" disabled={loading || twoFACode.length !== 6}
                  className="w-full flex items-center justify-center gap-2 bg-white text-violet-700 hover:bg-violet-50 disabled:opacity-50 font-bold py-2.5 rounded-lg text-sm transition-colors shadow-lg shadow-black/20 mt-1">
                  {loading ? 'Verifying...' : (<>Verify and sign in <ArrowRight size={16}/></>)}
                </button>
              </form>

              <button
                onClick={() => { setChallengeToken(null); setTwoFACode(''); setError('') }}
                className="text-center w-full text-sm text-violet-100 hover:text-white mt-6">
                ← Use a different account
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
