import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { paperTradingApi } from '../api/endpoints'

// ── Per-session $ allocation editor (ALLOC-V1) ─────────────────────
// Inline editor on every futures paper (mode='paper') session row —
// active AND stopped, so an allocation can be staged before the next
// start. PATCHes /sessions/{id}/allocation (clamped server-side to
// $1k–$1M); the engine only reads it when the session's runner
// (re)starts. options_paper sessions never render this editor — they
// are sized by the Tradier sandbox (see AllocationNote below).
export default function AllocationEditor({ session, extraInvalidateKeys = [] }: { session: any; extraInvalidateKeys?: any[][] }) {
  const qc = useQueryClient()
  const [value, setValue] = useState<string>(String(session.starting_balance ?? 10000))
  const [saved, setSaved] = useState(false)
  // Re-sync the input when the server value changes (e.g. the clamped value
  // comes back on refetch) — render-time sync keyed on the incoming prop.
  const [syncedFrom, setSyncedFrom] = useState<any>(session.starting_balance)
  if (session.starting_balance !== syncedFrom) {
    setSyncedFrom(session.starting_balance)
    setValue(String(session.starting_balance ?? 10000))
  }
  const num = parseFloat(value)
  const valid = Number.isFinite(num) && num >= 1000 && num <= 1000000
  const saveMut = useMutation({
    mutationFn: (amt: number) => paperTradingApi.setAllocation(session.id, amt),
    onMutate: async (amt: number) => {
      // Optimistic: patch the cached sessions list immediately.
      await qc.cancelQueries({ queryKey: ['paper-sessions'] })
      const prev = qc.getQueryData(['paper-sessions'])
      qc.setQueryData(['paper-sessions'], (old: any) =>
        Array.isArray(old) ? old.map((x: any) => (x.id === session.id ? { ...x, starting_balance: amt } : x)) : old)
      return { prev }
    },
    onError: (_e: any, _amt: number, ctx: any) => {
      if (ctx?.prev) qc.setQueryData(['paper-sessions'], ctx.prev)
    },
    onSuccess: (resp: any) => {
      const clamped = resp?.data?.starting_balance
      if (clamped != null) setValue(String(clamped))
      setSaved(true)
      setTimeout(() => setSaved(false), 4000)
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['paper-sessions'] })
      for (const key of extraInvalidateKeys) qc.invalidateQueries({ queryKey: key })
    },
  })
  return (
    <div
      className="mt-2.5 pt-2.5 border-t border-slate-100 dark:border-slate-700/60"
      onClick={(e) => { e.preventDefault(); e.stopPropagation() }}
    >
      <div className="flex items-center gap-1.5">
        <span className="text-[10px] uppercase tracking-wider text-slate-400 dark:text-slate-500 font-bold whitespace-nowrap">$ allocation</span>
        <input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          inputMode="numeric"
          className="flex-1 min-w-0 px-2 py-1 text-xs font-semibold rounded-lg border border-slate-200 dark:border-slate-600 bg-white dark:bg-slate-900 text-slate-800 dark:text-slate-100 tabular-nums focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <button
          onClick={() => valid && saveMut.mutate(num)}
          disabled={!valid || saveMut.isPending}
          className={`px-2 py-1 rounded-lg text-[11px] font-bold transition-colors ${saved ? 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300' : 'bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-40 disabled:cursor-not-allowed'}`}
        >
          {saveMut.isPending ? 'Saving…' : saved ? 'Saved ✓' : 'Save'}
        </button>
      </div>
      {!valid && value !== '' && (
        <div className="text-[10px] text-rose-500 mt-1">Enter an amount between $1,000 and $1,000,000</div>
      )}
      <div className="text-[10px] text-slate-400 dark:text-slate-500 mt-1">
        {session.is_active ? 'Applies when the session restarts' : 'Applies when the session next starts'}
      </div>
    </div>
  )
}

// Read-only companion for options_paper sessions: there is no per-session
// $ allocation to edit — position sizing comes from the Tradier sandbox.
export function AllocationNote() {
  return (
    <div className="text-[10px] text-slate-400 dark:text-slate-500 mt-0.5">
      $ allocation — sized by Tradier sandbox
    </div>
  )
}
