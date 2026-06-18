/**
 * AutomationStatusBadge — tiny clickable pill (Phase G) that surfaces the
 * user's plan/automation state and opens PlanAccessModal on click. Adaptive
 * copy + tone per tier. Hidden for admins and while the user/access is loading.
 * Designed to sit in the sidebar footer and the mobile drawer.
 */
import { useState } from 'react'
import { Bot, Signal, ClipboardCheck, FlaskConical } from 'lucide-react'
import { useAuthStore } from '../stores/authStore'
import { useMyAccess } from '../hooks/useMyAccess'
import PlanAccessModal from './PlanAccessModal'

type Tone = 'badge-green' | 'badge-amber' | 'badge-grey'

function describe(access: NonNullable<ReturnType<typeof useMyAccess>['data']>): { label: string; tone: Tone; Icon: any } {
  const { tier, automation_status } = access
  if (tier === 'tier_5') {
    if (automation_status === 'enabled') return { label: 'Automation ON', tone: 'badge-green', Icon: Bot }
    if (automation_status === 'agreement_required' || automation_status === 'pending') {
      return { label: 'Activate automation', tone: 'badge-amber', Icon: Bot }
    }
    // disabled / not_eligible fallback
    return { label: 'Automation off', tone: 'badge-grey', Icon: Bot }
  }
  if (tier === 'tier_4') return { label: 'Approve to place', tone: 'badge-grey', Icon: ClipboardCheck }
  if (tier === 'tier_2' || tier === 'tier_3') return { label: 'Signals', tone: 'badge-grey', Icon: Signal }
  // free_trial / tier_1 / anything else
  return { label: 'Paper', tone: 'badge-grey', Icon: FlaskConical }
}

export default function AutomationStatusBadge() {
  const { user } = useAuthStore()
  const { data: access, isLoading } = useMyAccess()
  const [open, setOpen] = useState(false)

  // Hidden for admins and while user is null/loading.
  if (!user || user.is_admin) return null
  if (isLoading || !access) {
    return <div className="h-5 w-24 rounded-full bg-slate-200 dark:bg-slate-800 animate-pulse"/>
  }

  const { label, tone, Icon } = describe(access)

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title="View your plan & access"
        className={`badge ${tone} gap-1 cursor-pointer hover:opacity-80 transition-opacity`}
      >
        <Icon size={12} className="flex-shrink-0"/>
        {label}
      </button>
      {open && <PlanAccessModal onClose={() => setOpen(false)}/>}
    </>
  )
}
