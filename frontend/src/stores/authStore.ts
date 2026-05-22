import { create } from 'zustand'
import type { User, SubscriptionTier } from '../types'

interface AuthState {
  user: User | null
  token: string | null
  isAuthenticated: boolean
  setAuth: (user: User, token: string) => void
  logout: () => void
  hasAccess: (requiredTier: SubscriptionTier) => boolean
}

const TIER_ORDER: Record<SubscriptionTier, number> = {
  free_trial: 0,
  tier_2: 2,
  tier_3: 3,
  tier_4: 4,
  tier_5: 5,
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  token: sessionStorage.getItem('access_token'),
  isAuthenticated: !!sessionStorage.getItem('access_token'),

  setAuth: (user, token) => {
    sessionStorage.setItem('access_token', token)
    set({ user, token, isAuthenticated: true })
  },

  logout: () => {
    // Fire-and-forget: invalidate the admin safe-word flag on the server
    // so re-login requires the passcode again. Don't await — we want
    // logout to feel instant.
    const t = sessionStorage.getItem('access_token')
    if (t) {
      try {
        fetch('/api/v1/admin/lock', {
          method: 'POST',
          headers: { Authorization: `Bearer ${t}` },
        }).catch(() => {/* server unreachable — local logout still proceeds */})
      } catch { /* ignore */ }
    }
    sessionStorage.removeItem('access_token')
    set({ user: null, token: null, isAuthenticated: false })
  },

  hasAccess: (requiredTier) => {
    const { user } = get()
    if (!user) return false
    return TIER_ORDER[user.subscription_tier] >= TIER_ORDER[requiredTier]
  },
}))
