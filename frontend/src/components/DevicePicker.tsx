import { useEffect, useState } from 'react'
import { Monitor, Smartphone } from 'lucide-react'

const KEY = 'edge_device_pref'  // 'browser' | 'mobile'

function applyClass(pref: 'browser' | 'mobile') {
  document.body.classList.toggle('device-mobile', pref === 'mobile')
  document.body.classList.toggle('device-browser', pref === 'browser')
}

// Read at module load so the body class is right before React mounts —
// avoids a flash of desktop layout for returning mobile users.
const initial = (typeof window !== 'undefined' ? localStorage.getItem(KEY) : null) as 'browser' | 'mobile' | null
if (initial === 'mobile' || initial === 'browser') {
  if (typeof document !== 'undefined') applyClass(initial)
}

export function getDevicePref(): 'browser' | 'mobile' | null {
  if (typeof window === 'undefined') return null
  const v = localStorage.getItem(KEY)
  return v === 'mobile' || v === 'browser' ? v : null
}

export default function DevicePicker() {
  const [pref, setPref] = useState<'browser' | 'mobile' | null>(() => getDevicePref())

  // Re-sync on mount in case other tabs changed it.
  useEffect(() => {
    const v = getDevicePref()
    if (v) applyClass(v)
  }, [])

  if (pref) return null

  const choose = (p: 'browser' | 'mobile') => {
    localStorage.setItem(KEY, p)
    applyClass(p)
    setPref(p)
  }

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-slate-950/85 backdrop-blur-sm p-4">
      <div className="w-full max-w-lg rounded-2xl bg-white dark:bg-slate-800 shadow-2xl border border-slate-200 overflow-hidden">
        <div className="px-6 py-5 border-b border-slate-100 text-center">
          <div className="text-xs font-bold text-blue-600 uppercase tracking-widest">Welcome to Theta Algos</div>
          <h2 className="text-xl font-extrabold text-slate-900 mt-1">How are you viewing?</h2>
          <p className="text-xs text-slate-500 mt-1">Pick the layout that matches your device — we'll optimize spacing, navigation and chart sizes accordingly.</p>
        </div>
        <div className="grid grid-cols-2 gap-3 p-5">
          <button
            onClick={() => choose('browser')}
            className="group flex flex-col items-center gap-3 rounded-xl border-2 border-slate-200 hover:border-blue-500 hover:bg-blue-50 px-4 py-6 transition"
          >
            <Monitor size={36} className="text-slate-500 group-hover:text-blue-600" />
            <div>
              <div className="text-sm font-bold text-slate-900">Web Browser</div>
              <div className="text-[11px] text-slate-500 mt-0.5">Desktop / laptop</div>
            </div>
          </button>
          <button
            onClick={() => choose('mobile')}
            className="group flex flex-col items-center gap-3 rounded-xl border-2 border-slate-200 hover:border-blue-500 hover:bg-blue-50 px-4 py-6 transition"
          >
            <Smartphone size={36} className="text-slate-500 group-hover:text-blue-600" />
            <div>
              <div className="text-sm font-bold text-slate-900">Mobile</div>
              <div className="text-[11px] text-slate-500 mt-0.5">Phone / tablet</div>
            </div>
          </button>
        </div>
        <div className="px-5 pb-4 text-center">
          <p className="text-[10px] text-slate-400">You can change this anytime from Profile → Display.</p>
        </div>
      </div>
    </div>
  )
}
