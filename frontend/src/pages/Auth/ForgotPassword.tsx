import { useState } from 'react'
import { Link } from 'react-router-dom'
import { authApi } from '../../api/endpoints'
import { ArrowLeft, Mail } from 'lucide-react'

export default function ForgotPassword() {
  const [email, setEmail] = useState('')
  const [submitted, setSubmitted] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true); setError('')
    try {
      await authApi.forgotPassword(email)
      setSubmitted(true)
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Something went wrong. Please try again.')
    } finally { setLoading(false) }
  }

  return (
    <div className="min-h-screen bg-slate-200 flex items-center justify-center p-8 dark:bg-slate-800">
      <div className="w-full max-w-md bg-white rounded-2xl shadow-sm border border-slate-200 p-8 dark:bg-slate-900 dark:border-slate-700">
        <Link to="/login" className="inline-flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-800 mb-6 transition-colors dark:text-slate-400">
          <ArrowLeft size={14}/> Back to sign in
        </Link>

        {!submitted ? (
          <>
            <div className="w-11 h-11 rounded-xl bg-blue-50 text-blue-600 flex items-center justify-center mb-4">
              <Mail size={20}/>
            </div>
            <h1 className="text-2xl font-extrabold text-slate-900 mb-1 dark:text-slate-100">Forgot your password?</h1>
            <p className="text-sm text-slate-500 mb-7 dark:text-slate-400">
              Enter the email tied to your account and we'll send a reset link. The link expires in 1 hour.
            </p>

            <form onSubmit={handleSubmit} className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-slate-700 mb-1.5 dark:text-slate-200">Email address</label>
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm text-slate-900 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent dark:text-slate-100 dark:border-slate-700 dark:placeholder-slate-500"
                  placeholder="you@example.com"/>
              </div>

              {error && (
                <div className="bg-red-50 border border-red-200 text-red-700 text-sm px-3.5 py-2.5 rounded-lg">
                  {error}
                </div>
              )}

              <button type="submit" disabled={loading}
                className="w-full bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white font-semibold py-2.5 rounded-lg text-sm transition-colors shadow-sm shadow-blue-200">
                {loading ? 'Sending...' : 'Send reset link'}
              </button>
            </form>
          </>
        ) : (
          <>
            <div className="w-11 h-11 rounded-xl bg-green-50 text-green-600 flex items-center justify-center mb-4">
              <Mail size={20}/>
            </div>
            <h1 className="text-2xl font-extrabold text-slate-900 mb-1 dark:text-slate-100">Check your inbox</h1>
            <p className="text-sm text-slate-500 mb-6 dark:text-slate-400">
              If an account exists for <span className="font-medium text-slate-700 dark:text-slate-200">{email}</span>, a password reset link is on its way. The link expires in 1 hour.
            </p>
            <Link to="/login" className="block text-center bg-blue-600 hover:bg-blue-700 text-white font-semibold py-2.5 rounded-lg text-sm transition-colors">
              Back to sign in
            </Link>
          </>
        )}
      </div>
    </div>
  )
}
