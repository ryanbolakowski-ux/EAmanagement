import axios from 'axios'

export const api = axios.create({
  baseURL: '',
  headers: { 'Content-Type': 'application/json' },
})

// Attach JWT token on every request
api.interceptors.request.use((config) => {
  const token = sessionStorage.getItem('access_token')
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
      sessionStorage.removeItem('access_token')
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
