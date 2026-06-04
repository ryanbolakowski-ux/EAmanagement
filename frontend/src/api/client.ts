import axios from 'axios'

// On Vercel/staging the frontend lives at a different origin than the API.
// On the Hetzner box, nginx proxies /api/* to the backend on the same host,
// so leaving VITE_API_URL empty Just Works there.
const API_BASE = (import.meta.env.VITE_API_URL || '').replace(/\/+$/, '')

export const api = axios.create({
  baseURL: API_BASE,
  headers: { 'Content-Type': 'application/json' },
})

// Attach JWT token on every request
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// Auto-logout on 401
api.interceptors.response.use(
  (res) => res,
  (error) => {
    const status = error.response?.status
    const detail = error.response?.data?.detail
    // FastAPI structured detail can be either a string ("kyc_required") or
    // an object ({"code": "requires_2fa_setup", "message": "..."}).
    const detailCode = typeof detail === 'object' && detail ? (detail as any).code : null
    if (status === 401) {
      localStorage.removeItem('access_token')
      window.location.href = '/login'
    } else if (status === 451) {
      if (window.location.pathname !== '/not-available') window.location.href = '/not-available'
    } else if (status === 403 && detail === 'kyc_required') {
      if (window.location.pathname !== '/kyc') window.location.href = '/app/kyc'
    } else if (status === 403 && detailCode === 'requires_2fa_setup') {
      // Mandatory 2FA gate (paid + trial users without totp_enabled).
      // Fire a custom event so TwoFactorRequiredModal can render the
      // blocking dialog. We do NOT redirect here — the modal handles
      // navigation so we don't trap the user mid-form-submit.
      try {
        window.dispatchEvent(new CustomEvent('twofa-required'))
      } catch {
        // Browsers without CustomEvent constructor (none we ship to, but
        // belt-and-suspenders) fall back to a hard redirect.
        if (window.location.pathname !== '/app/settings/2fa') {
          window.location.href = '/app/settings/2fa'
        }
      }
    }
    return Promise.reject(error)
  },
)

export default api
