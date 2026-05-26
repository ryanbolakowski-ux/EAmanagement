/**
 * VersionBanner — polls / every 60s, parses the current bundle hash from the
 * served HTML, compares it to the hash that was running when this tab loaded.
 * If they differ, shows a toast inviting the user to reload. One click → fresh
 * bundle, no more "I clicked Sign In and it didn't do anything" because the
 * cached old bundle is broken.
 */
import { useEffect, useState } from 'react'

function extractBundleHash(html: string): string | null {
  // Matches <script src="/assets/index-XXX.js"> — Vite's content-hashed entry
  const m = html.match(/\/assets\/index-([A-Za-z0-9_-]+)\.js/)
  return m ? m[1] : null
}

export default function VersionBanner() {
  const [currentHash, setCurrentHash] = useState<string | null>(null)
  const [newHash, setNewHash] = useState<string | null>(null)
  const [dismissedFor, setDismissedFor] = useState<string | null>(null)

  // Capture our own bundle hash at mount — that's the version this tab is running
  useEffect(() => {
    const scripts = Array.from(document.querySelectorAll<HTMLScriptElement>('script[src]'))
    for (const s of scripts) {
      const m = s.src.match(/\/assets\/index-([A-Za-z0-9_-]+)\.js/)
      if (m) { setCurrentHash(m[1]); break }
    }
  }, [])

  useEffect(() => {
    if (!currentHash) return
    let cancelled = false
    const tick = async () => {
      try {
        const r = await fetch(`/?_v=${Date.now()}`, { cache: 'no-store' })
        const html = await r.text()
        const h = extractBundleHash(html)
        if (h && h !== currentHash && !cancelled) setNewHash(h)
      } catch { /* offline — try again next tick */ }
    }
    tick()
    const id = setInterval(tick, 60000)
    return () => { cancelled = true; clearInterval(id) }
  }, [currentHash])

  if (!newHash || newHash === currentHash || newHash === dismissedFor) return null

  return (
    <div className="fixed bottom-4 right-4 z-[200] max-w-sm rounded-2xl bg-gradient-to-br from-violet-600 to-indigo-700 text-white p-4 shadow-2xl shadow-violet-900/40 border border-white/10">
      <div className="text-sm font-bold mb-1">New version available</div>
      <p className="text-xs opacity-90 mb-3">
        We shipped an update. Reload now to make sure your dashboard, signals, and login flow are on the current version.
      </p>
      <div className="flex gap-2">
        <button
          onClick={() => window.location.reload()}
          className="bg-white text-violet-700 font-bold text-xs px-3 py-1.5 rounded-lg hover:bg-violet-50"
        >
          Reload
        </button>
        <button
          onClick={() => setDismissedFor(newHash)}
          className="bg-white/15 text-white text-xs px-3 py-1.5 rounded-lg hover:bg-white/25"
        >
          Later
        </button>
      </div>
    </div>
  )
}
