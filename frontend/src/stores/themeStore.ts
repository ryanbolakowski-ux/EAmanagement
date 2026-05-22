import { create } from 'zustand'

export type Theme = 'light' | 'dark'

interface ThemeState {
  theme: Theme
  setTheme: (t: Theme) => void
  toggle: () => void
}

const STORAGE_KEY = 'edge-theme'

function readInitial(): Theme {
  if (typeof window === 'undefined') return 'light'
  const saved = window.localStorage.getItem(STORAGE_KEY)
  if (saved === 'light' || saved === 'dark') return saved
  return 'light'
}

function applyToDocument(t: Theme) {
  if (typeof document === 'undefined') return
  const root = document.documentElement
  if (t === 'dark') root.classList.add('dark')
  else root.classList.remove('dark')
}

const initial = readInitial()
applyToDocument(initial)

export const useThemeStore = create<ThemeState>((set, get) => ({
  theme: initial,
  setTheme: (t) => {
    window.localStorage.setItem(STORAGE_KEY, t)
    applyToDocument(t)
    set({ theme: t })
  },
  toggle: () => {
    const next: Theme = get().theme === 'light' ? 'dark' : 'light'
    window.localStorage.setItem(STORAGE_KEY, next)
    applyToDocument(next)
    set({ theme: next })
  },
}))
