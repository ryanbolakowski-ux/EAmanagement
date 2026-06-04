import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { QRCodeSVG } from 'qrcode.react'
import { useNavigate } from 'react-router-dom'
import { ShieldCheck, ArrowLeft } from 'lucide-react'

import { authApi } from '../api/endpoints'

// Dedicated 2FA setup page. Routed at /app/settings/2fa.
// Mounted by the requires_2fa_setup interceptor so paid/trial users land
// here on first visit after the gate goes live. Once they enable 2FA the
// gate opens automatically (backend re-checks totp_enabled per request).
export default function TwoFactorSetup() {
  const navigate = useNavigate()
  const qc = useQueryClient()

  const { data: me } = useQuery({
    queryKey: ['auth-me'],
    queryFn: () => authApi.me().then(r => r.data),
  })

  const [setupData, setSetupData] = useState<{ secret: string; otpauth_url: string } | null>(null)
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [success, setSuccess] = useState(false)

  const refreshMe = () => qc.invalidateQueries({ queryKey: ['auth-me'] })

  const startSetup = async () => {
    setBusy(true); setError('')
    try {
      const { data } = await authApi.setup2FA()
      setSetupData(data)
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Could not start 2FA setup.')
    } finally { setBusy(false) }
  }

  const confirm = async () => {
    setBusy(true); setError('')
    try {
      await authApi.confirm2FA(code.trim())
      setSetupData(null); setCode('')
      setSuccess(true)
      refreshMe()
      // Bounce to dashboard after a brief moment so the user sees the toast.
      setTimeout(() => navigate('/app'), 1500)
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Invalid code.')
    } finally { setBusy(false) }
  }

  const enabled = !!me?.totp_enabled

  if (enabled || success) {
    return (
      <div className="max-w-2xl mx-auto p-6">
        <div className="bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-xl p-6">
          <div className="flex items-center gap-3 mb-2">
            <ShieldCheck size={24} className="text-green-600" />
            <h1 className="text-xl font-bold text-green-800 dark:text-green-100">
              2FA is enabled
            </h1>
          </div>
          <p className="text-sm text-green-700 dark:text-green-200 mb-4">
            Your account is now protected by two-factor authentication.
            Returning to the dashboard...
          </p>
          <button
            onClick={() => navigate('/app')}
            className="px-4 py-2 bg-green-600 hover:bg-green-700 text-white text-sm font-semibold rounded-lg"
          >
            Go to dashboard
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="max-w-2xl mx-auto p-6">
      <button
        onClick={() => navigate(-1)}
        className="text-sm text-slate-500 hover:text-slate-700 dark:text-slate-400 mb-4 flex items-center gap-1"
      >
        <ArrowLeft size={16} /> Back
      </button>

      <div className="bg-white rounded-xl border border-slate-200 p-6 dark:bg-slate-800 dark:border-slate-700">
        <div className="flex items-center gap-2 mb-4">
          <ShieldCheck size={22} className="text-blue-600" />
          <h1 className="text-xl font-bold text-slate-900 dark:text-slate-100">
            Two-Factor Authentication required
          </h1>
        </div>

        <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-xl p-4 mb-5">
          <p className="text-sm text-amber-800 dark:text-amber-200">
            2FA is required for all paid and trial accounts. Set it up below to continue using the app.
          </p>
        </div>

        {!setupData && (
          <>
            <p className="text-sm text-slate-600 dark:text-slate-400 mb-4">
              You will need an authenticator app like Google Authenticator, Authy, or 1Password.
              Once you scan the QR code and verify the 6-digit code, future logins will require 2FA.
            </p>
            <button
              onClick={startSetup}
              disabled={busy}
              className="px-5 py-2.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-semibold rounded-lg"
            >
              {busy ? 'Starting...' : 'Set up 2FA now'}
            </button>
          </>
        )}

        {setupData && (
          <>
            <p className="text-sm text-slate-600 dark:text-slate-400 mb-4">
              Scan this QR code in your authenticator app, then enter the 6-digit code it shows.
            </p>
            <div className="flex flex-col sm:flex-row gap-5 mb-4">
              <div className="bg-white border border-slate-200 rounded-xl p-3 self-start dark:bg-slate-800 dark:border-slate-700">
                <QRCodeSVG value={setupData.otpauth_url} size={180} />
              </div>
              <div className="flex-1">
                <div className="text-xs text-slate-500 mb-1 dark:text-slate-400">Or enter this code manually:</div>
                <code className="block bg-slate-100 rounded-lg px-3 py-2 text-xs font-mono text-slate-700 break-all mb-4 dark:bg-slate-800 dark:text-slate-200">
                  {setupData.secret}
                </code>
                <label className="block text-sm font-medium text-slate-700 mb-1.5 dark:text-slate-200">
                  6-digit code
                </label>
                <input
                  type="text"
                  inputMode="numeric"
                  value={code}
                  onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                  className="w-full border border-slate-300 rounded-lg px-3 py-2 text-base font-mono tracking-[0.4em] text-center focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700"
                  placeholder="123456"
                />
              </div>
            </div>
            {error && (
              <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 text-red-700 text-sm px-3 py-2 rounded-lg mb-3">
                {error}
              </div>
            )}
            <div className="flex gap-2">
              <button
                onClick={confirm}
                disabled={busy || code.length !== 6}
                className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-semibold rounded-lg"
              >
                {busy ? 'Verifying...' : 'Verify and enable'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
