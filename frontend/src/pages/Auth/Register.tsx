import { useState } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { useAuthStore } from '../../stores/authStore'
import { authApi } from '../../api/endpoints'
import api from '../../api/client'
import { BarChart2, ArrowRight, CheckCircle2 } from 'lucide-react'
import AcknowledgmentModal from '../../components/AcknowledgmentModal'
import { TERMS_OF_SERVICE_TEXT } from '../../utils/legalText'
import ThetaLogo from '../../components/ThetaLogo'

export default function Register() {
  const [email, setEmail]       = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [agreedTOS, setAgreedTOS] = useState(false)
  const [agreedUSOnly, setAgreedUSOnly] = useState(false)
  const [agreed18, setAgreed18] = useState(false)
  const [showTOS, setShowTOS]   = useState(false)
  const [error, setError]       = useState('')
  const [loading, setLoading]   = useState(false)
  const { setAuth } = useAuthStore()
  const navigate = useNavigate()

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!agreedTOS) {
      setError('You must read and accept the Terms of Service to create an account.')
      return
    }
    if (!agreedUSOnly) {
      setError('You must confirm you are a US resident to create an account.')
      return
    }
    if (!agreed18) {
      setError('You must be at least 18 years old to use Theta Algos.')
      return
    }
    setLoading(true); setError('')
    try {
      const { data } = await authApi.register(email, username, password)
      localStorage.setItem('access_token', data.access_token)
      // Log the acknowledgment now that we have a token
      try {
        await api.post('/api/v1/legal/acknowledge', { kind: 'terms_of_service', detail: 'accepted on registration' })
      } catch { /* non-fatal */ }
      const meRes    = await authApi.me()
      setAuth(meRes.data, data.access_token)
      navigate('/app')
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Registration failed. Please try again.')
    } finally { setLoading(false) }
  }

  return (
    <div className="min-h-screen bg-slate-50 flex dark:bg-slate-900">
      {/* Left panel */}
      <div className="hidden lg:flex lg:w-1/2 bg-slate-900 p-12 flex-col justify-between">
        <Link to="/" className="flex items-center gap-2.5">
          <ThetaLogo size={48} />
        </Link>
        <div>
          <h2 className="text-3xl font-bold text-white mb-4 leading-snug">
            Start building your algorithmic edge today.
          </h2>
          <div className="space-y-3">
            {[
              'Full strategy builder — no coding required',
              '2+ years of ES & NQ historical data',
              'Paper trading included in your free trial',
              'Tradovate live execution when you\'re ready',
            ].map((item) => (
              <div key={item} className="flex items-center gap-3 text-sm text-slate-300 dark:text-slate-600">
                <CheckCircle2 size={15} className="text-blue-400 flex-shrink-0"/>
                {item}
              </div>
            ))}
          </div>
        </div>
        <div className="text-slate-500 text-xs dark:text-slate-400">
          No credit card required · Cancel anytime · 30-day free trial
        </div>
      </div>

      {/* Right panel */}
      <div className="flex-1 flex items-center justify-center p-8">
        <div className="w-full max-w-sm">
          <Link to="/" className="flex items-center gap-2 mb-8 lg:hidden">
            <ThetaLogo size={40} />
          </Link>

          <h1 className="text-2xl font-extrabold text-slate-900 mb-1 dark:text-slate-100">Create your account</h1>
          <p className="text-sm text-slate-500 mb-7 dark:text-slate-400">30-day free trial · No credit card needed</p>

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5 dark:text-slate-200">Email address</label>
              <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required
                className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm text-slate-900 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent dark:text-slate-100 dark:border-slate-700 dark:placeholder-slate-500"
                placeholder="you@example.com"/>
            </div>
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5 dark:text-slate-200">Username</label>
              <input type="text" value={username} onChange={(e) => setUsername(e.target.value)} required
                className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm text-slate-900 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent dark:text-slate-100 dark:border-slate-700 dark:placeholder-slate-500"
                placeholder="traderhandle"/>
            </div>
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5 dark:text-slate-200">Password</label>
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={8}
                className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm text-slate-900 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent dark:text-slate-100 dark:border-slate-700 dark:placeholder-slate-500"
                placeholder="At least 8 characters"/>
            </div>

                        {/* US-residency confirmation */}
            <div className={`rounded-lg p-3 border ${agreedUSOnly ? 'border-green-200 bg-green-50' : 'border-amber-200 bg-amber-50'}`}>
              <label className="flex items-start gap-2.5 cursor-pointer">
                <input type="checkbox" checked={agreedUSOnly}
                  onChange={e => setAgreedUSOnly(e.target.checked)}
                  className="mt-0.5 w-4 h-4"/>
                <span className="text-xs text-slate-700 leading-relaxed">
                  I confirm I am a <strong>resident of the United States</strong>. Theta Algos is currently
                  licensed only for US residents and will verify my identity (KYC) before enabling live trading.
                  Providing false residency information may result in account termination.
                </span>
              </label>
            </div>

                        {/* 18+ confirmation */}
            <div className={`rounded-lg p-3 border ${agreed18 ? 'border-green-200 bg-green-50' : 'border-amber-200 bg-amber-50'}`}>
              <label className="flex items-start gap-2.5 cursor-pointer">
                <input type="checkbox" checked={agreed18}
                  onChange={e => setAgreed18(e.target.checked)}
                  className="mt-0.5 w-4 h-4"/>
                <span className="text-xs text-slate-700 leading-relaxed">
                  I confirm I am <strong>at least 18 years old</strong>. US derivatives and securities regulations
                  (CFTC / SEC / FINRA) prohibit minors from trading. We will verify your date of birth against
                  your government-issued ID during the KYC step. Providing false age information may result in
                  account termination and forfeiture of any funds.
                </span>
              </label>
            </div>

            {/* Terms of Service gate — must read+agree before signup */}
            <div className={`rounded-lg p-3 border ${agreedTOS ? 'border-green-200 bg-green-50' : 'border-amber-200 bg-amber-50'}`}>
              <label className="flex items-start gap-2.5 cursor-pointer">
                <input type="checkbox" checked={agreedTOS}
                  onChange={e => {
                    if (e.target.checked && !agreedTOS) { setShowTOS(true); }
                    else { setAgreedTOS(e.target.checked) }
                  }}
                  className="mt-0.5 w-4 h-4"/>
                <span className="text-xs text-slate-700 leading-relaxed">
                  I have read and accept the <button type="button" onClick={() => setShowTOS(true)} className="font-bold text-blue-600 underline">Theta Algos Terms of Service & Risk Disclosure</button> (futures, options, prop-firm rules, and account-loss risk).
                </span>
              </label>
            </div>

            {error && (
              <div className="bg-red-50 border border-red-200 text-red-700 text-sm px-3.5 py-2.5 rounded-lg">{error}</div>
            )}

            <button type="submit" disabled={loading || !agreedTOS || !agreedUSOnly || !agreed18}
              className="w-full flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white font-semibold py-2.5 rounded-lg text-sm transition-colors shadow-sm shadow-blue-200 mt-1">
              {loading ? 'Creating account...' : (<>Start Free Trial <ArrowRight size={16}/></>)}
            </button>
          </form>

          {showTOS && (
            <AcknowledgmentModal
              kind="terms_of_service"
              title="Terms of Service & Risk Disclosure"
              body={TERMS_OF_SERVICE_TEXT}
              requireScroll={true}
              skipServerAck={true}
              acceptLabel="I agree — create my account"
              onDecline={() => setShowTOS(false)}
              onAccept={() => { setAgreedTOS(true); setShowTOS(false) }}
            />
          )}

          <p className="text-center text-xs text-slate-400 mt-4 dark:text-slate-500">
            Your acknowledgment is logged with timestamp and IP for compliance.
          </p>
          <p className="text-center text-sm text-slate-500 mt-4 dark:text-slate-400">
            Already have an account?{' '}
            <Link to="/login" className="text-blue-600 font-semibold hover:text-blue-700">Sign in</Link>
          </p>
        </div>
      </div>
    </div>
  )
}
