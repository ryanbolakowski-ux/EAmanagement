// ──────────────────────────────────────────────────────────────────────────────
// useEventStream — EventSource wrapper for the backend SSE feeds
// (GET /api/v1/stream/dashboard). Why SSE over WebSocket: it rides plain HTTP
// through the existing nginx/Vercel-origin setup — full tradeoff note lives in
// backend app/api/routes/stream.py.
//
//  * EventSource cannot set an Authorization header, so the JWT is passed as a
//    ?token= query param; the backend validates it exactly like
//    get_current_user does (expired token ⇒ 401 ⇒ onerror ⇒ backoff below).
//  * Reconnects with exponential backoff (1s → 30s cap). The browser's
//    built-in retry is disabled by closing the source on error so the backoff
//    schedule stays under OUR control (and re-reads a fresh token each try).
//  * Exposes the latest payload per named event + a `connected` flag; the
//    stream is closed on unmount. Consumers (DashboardV2) write payloads into
//    the react-query cache so the queries stay the single source of truth.
// ──────────────────────────────────────────────────────────────────────────────
import { useEffect, useState } from 'react'
import { API_BASE } from '../api/client'

export type EventStreamPayloads = Record<string, unknown>

const BACKOFF_BASE_MS = 1_000
const BACKOFF_CAP_MS = 30_000

export function useEventStream(path: string, events: readonly string[]) {
  const [connected, setConnected] = useState(false)
  const [payloads, setPayloads] = useState<EventStreamPayloads>({})

  // Stable key so a caller passing a fresh array literal each render doesn't
  // tear the connection down and back up on every render.
  const eventsKey = events.join(',')

  useEffect(() => {
    let disposed = false
    let source: EventSource | null = null
    let retryTimer: number | null = null
    let attempt = 0

    const scheduleReconnect = () => {
      if (disposed || retryTimer !== null) return
      const delay = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** attempt)
      attempt += 1
      retryTimer = window.setTimeout(() => {
        retryTimer = null
        connect()
      }, delay)
    }

    const connect = () => {
      if (disposed) return
      const token = localStorage.getItem('access_token')
      if (!token) {
        // Not logged in (or the 401 interceptor just cleared the token) —
        // keep retrying on the backoff schedule instead of erroring out.
        scheduleReconnect()
        return
      }
      const es = new EventSource(
        `${API_BASE}${path}?token=${encodeURIComponent(token)}`,
      )
      source = es

      es.onopen = () => {
        attempt = 0 // healthy again — next failure starts backoff from 1s
        setConnected(true)
      }

      for (const name of eventsKey.split(',')) {
        es.addEventListener(name, (ev: Event) => {
          const msg = ev as MessageEvent<string>
          try {
            const data: unknown = JSON.parse(msg.data)
            setPayloads(prev => ({ ...prev, [name]: data }))
          } catch {
            // Malformed frame — drop it rather than blank a panel; the
            // polling fallback still guarantees eventual consistency.
          }
        })
      }

      es.onerror = () => {
        // Fires for auth rejects, network drops and server restarts alike —
        // EventSource exposes no status code, so every failure takes the
        // same road: close, flag disconnected (polling resumes), back off.
        es.close()
        if (source === es) source = null
        setConnected(false)
        scheduleReconnect()
      }
    }

    connect()
    return () => {
      disposed = true
      if (retryTimer !== null) window.clearTimeout(retryTimer)
      source?.close()
      setConnected(false)
    }
  }, [path, eventsKey])

  return { connected, payloads }
}
