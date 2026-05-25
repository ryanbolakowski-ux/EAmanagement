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
    if (status === 401) {
      localStorage.removeItem('access_token')
      window.location.href = '/login'
    } else if (status === 451) {
      if (window.location.pathname !== '/not-available') window.location.href = '/not-available'
    } else if (status === 403 && detail === 'kyc_required') {
      if (window.location.pathname !== '/kyc') window.location.href = '/app/kyc'
    }
    return Promise.reject(error)
  },
)

export default api
