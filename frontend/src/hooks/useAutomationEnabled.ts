// ──────────────────────────────────────────────────────────────────────────────
// useAutomationEnabled — single source of truth for the global fully-automated
// master-switch (app_config.automation_enabled). Reads the auth-free
// GET /api/v1/config/public endpoint so it is safe on logged-out marketing pages
// and inside the authenticated app alike.
//
// FAIL-SAFE: defaults to FALSE while the query is loading OR errors, so the
// public never flashes the live automation UI before the flag confirms it is on.
// When the flag resolves to true, every consumer reverts to today's exact live
// behavior. Cached 60s so the marketing pages, dashboard, and activation modal
// share a single round-trip.
//
// Invalidate the ['public-config'] query (PUBLIC_CONFIG_KEY) after the admin
// toggles the switch so every mounted consumer refetches.
// ──────────────────────────────────────────────────────────────────────────────
import { useQuery } from '@tanstack/react-query'
import { configApi } from '../api/endpoints'

export const PUBLIC_CONFIG_KEY = ['public-config'] as const

export function useAutomationEnabled(): boolean {
  const { data } = useQuery({
    queryKey: PUBLIC_CONFIG_KEY,
    queryFn: () => configApi.public().then(r => r.data),
    staleTime: 60_000,
    retry: false,
  })
  // Undefined while loading or on error → coming-soon (safe) state.
  return data?.automation_enabled ?? false
}
