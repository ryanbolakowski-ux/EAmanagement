import { useEffect, useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { ShieldAlert } from 'lucide-react'

// Listens for the global "twofa-required" event dispatched by the axios
// interceptor when the backend returns 403 detail.code='requires_2fa_setup'.
// Renders a blocking modal that cannot be dismissed; only path forward is
// the "Set up 2FA now" button which takes the user to /app/settings/2fa.
export default function TwoFactorRequiredModal() {
  const [open, setOpen] = useState(false)
  const navigate = useNavigate()
  const location = useLocation()

  useEffect(() => {
    const onTriggered = () => setOpen(true)
    window.addEventListener('twofa-required', onTriggered)
    return () => window.removeEventListener('twofa-required', onTriggered)
  }, [])

  // Auto-close when the user is already on the setup page.
  useEffect(() => {
    if (location.pathname === '/app/settings/2fa') setOpen(false)
  }, [location.pathname])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/60 backdrop-blur-sm"
      // Intentionally NO onClick close handler — the modal is blocking.
    >
      <div className="max-w-md w-full mx-4 bg-white dark:bg-slate-800 rounded-2xl shadow-2xl border border-slate-200 dark:border-slate-700 p-6">
        <div className="flex items-center gap-3 mb-3">
          <div className="w-10 h-10 bg-amber-100 dark:bg-amber-900/30 rounded-full flex items-center justify-center">
            <ShieldAlert size={22} className="text-amber-600" />
          </div>
          <h2 className="text-lg font-bold text-slate-900 dark:text-slate-100">
            2FA required
          </h2>
        </div>
        <p className="text-sm text-slate-600 dark:text-slate-300 mb-5 leading-relaxed">
          Two-factor authentication is now required for all paid and trial
          accounts. Set up an authenticator code to continue using the app.
        </p>
        <button
          onClick={() => {
            setOpen(false)
            navigate('/app/settings/2fa')
          }}
          className="w-full px-4 py-2.5 bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold rounded-lg"
        >
          Set up 2FA now
        </button>
      </div>
    </div>
  )
}
