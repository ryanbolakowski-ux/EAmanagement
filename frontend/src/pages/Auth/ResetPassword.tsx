import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { authApi } from '../../api/endpoints'
import { ArrowLeft, KeyRound } from 'lucide-react'

export default function ResetPassword() {
  const [params] = useSearchParams()
  const token = params.get('token') || ''
  const navigate = useNavigate()

  const [password, setPassword] = useState('')
  const [confirm, setConfirm]   = useState('')
  const [error, setError]       = useState('')
  const [loading, setLoading]   = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (password.length < 8) { setError('Password must be at least 8 characters.'); return }
    if (password !== confirm) { setError('Passwords don\'t match.'); return }
    if (!token) { setError('Reset link is missing the token. Request a new email.'); return }
    setLoading(true); setError('')
    try {
      await authApi.resetPassword(token, password)
      navigate('/login?reset=1')
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Could not reset password.')
    } finally { setLoading(false) }
  }

  return (
    <div className="min-h-screen bg-slate-200 flex items-center justify-center p-8 dark:bg-slate-800">
      <div className="w-full max-w-md bg-white rounded-2xl shadow-sm border border-slate-200 p-8 dark:bg-slate-900 dark:border-slate-700">
        <Link to="/login" className="inline-flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-800 mb-6 transition-colors dark:text-slate-400">
          <ArrowLeft size={14}/> Back to sign in
        </Link>

        <div className="w-11 h-11 rounded-xl bg-blue-50 text-blue-600 flex items-center justify-center mb-4">
          <KeyRound size={20}/>
        </div>
        <h1 className="text-2xl font-extrabold text-slate-900 mb-1 dark:text-slate-100">Set a new password</h1>
        <p className="text-sm text-slate-500 mb-7 dark:text-slate-400">Pick something at least 8 characters long.</p>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5 dark:text-slate-200">New password</label>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required
              className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700"
              placeholder="At least 8 characters"/>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5 dark:text-slate-200">Confirm new password</label>
            <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required
              className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700"
              placeholder="Repeat your new password"/>
          </div>

          {error && (
            <div className="bg-red-50 border border-red-200 text-red-700 text-sm px-3.5 py-2.5 rounded-lg">
              {error}
            </div>
          )}

          <button type="submit" disabled={loading}
            className="w-full bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white font-semibold py-2.5 rounded-lg text-sm transition-colors shadow-sm shadow-blue-200">
            {loading ? 'Saving...' : 'Reset password'}
          </button>
        </form>
      </div>
    </div>
  )
}
