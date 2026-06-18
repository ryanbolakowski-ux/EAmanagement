// ──────────────────────────────────────────────────────────────────────────────────
// useMyAccess — react-query hook for the user's plan capabilities + automation
// status (Phase G). One source of truth for the badge, plan modal and the
// automation activation flow. Cached briefly so the badge in the sidebar and the
// modal share a single network round-trip.
// ──────────────────────────────────────────────────────────────────────────────────
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { accountSignalsApi } from '../api/endpoints'
import type { MyAccess } from '../types/access'

export const MY_ACCESS_KEY = ['my-access'] as const

export function useMyAccess() {
  const q = useQuery<MyAccess>({
    queryKey: MY_ACCESS_KEY,
    queryFn: () => accountSignalsApi.myAccess().then(r => r.data),
    staleTime: 30_000,
    retry: false,
  })
  return {
    data: q.data,
    isLoading: q.isLoading,
    isError: q.isError,
    refetch: q.refetch,
  }
}

// Hook helper to invalidate the cached access so callers can force a refetch
// after enabling/disabling automation or accepting an agreement.
export function useInvalidateMyAccess() {
  const qc = useQueryClient()
  return () => qc.invalidateQueries({ queryKey: MY_ACCESS_KEY })
}
