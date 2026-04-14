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
  tier_1: 1,
  tier_3: 3,
  tier_4: 4,
  tier_5: 5,
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  token: localStorage.getItem('access_token'),
  isAuthenticated: !!localStorage.getItem('access_token'),

  setAuth: (user, token) => {
    localStorage.setItem('access_token', token)
    set({ user, token, isAuthenticated: true })
  },

  logout: () => {
    localStorage.removeItem('access_token')
    set({ user: null, token: null, isAuthenticated: false })
  },

  hasAccess: (requiredTier) => {
    const { user } = get()
    if (!user) return false
    return TIER_ORDER[user.subscription_tier] >= TIER_ORDER[requiredTier]
  },
}))
