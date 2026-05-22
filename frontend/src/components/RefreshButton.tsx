import { useState } from 'react'
import { RefreshCw } from 'lucide-react'

interface Props {
  onClick: () => void | Promise<unknown>
  label?: string
}

/** Small refresh button with a brief spin animation so it looks active even
 *  when the underlying refetch resolves instantly. */
export default function RefreshButton({ onClick, label = 'Refresh' }: Props) {
  const [spinning, setSpinning] = useState(false)
  const handle = async () => {
    setSpinning(true)
    try {
      await onClick()
    } finally {
      // Keep the spinner up briefly so it reads as "I refreshed", even on cache hits
      setTimeout(() => setSpinning(false), 600)
    }
  }
  return (
    <button
      onClick={handle}
      className="flex items-center gap-2 border border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800 px-4 py-2.5 rounded-xl text-sm font-medium transition-colors"
    >
      <RefreshCw size={14} className={spinning ? 'animate-spin' : undefined}/>
      {label}
    </button>
  )
}
