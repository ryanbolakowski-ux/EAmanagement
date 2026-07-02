import React, { useEffect, useState } from 'react'

/**
 * TickerTape — a Wall-Street-building style LED price crawl.
 *
 * Two of these bands frame the LandingV2 hero (top band scrolls left, bottom
 * band scrolls right) to evoke the NYSE ticker wrapping a building. Pure CSS
 * animation (see v2.css §19): the row is rendered twice inside one track and
 * translated -50% for a seamless infinite loop; prefers-reduced-motion gets a
 * static row.
 *
 * DATA IS LIVE-WITH-FALLBACK: on mount (and every 60s) the band fetches
 * GET /api/v1/public/tape — a public, no-auth endpoint (prod:
 * backend/app/api/routes/public_tape.py; dev preview: the vite middleware in
 * vite.config.ts). While the fetch hasn't succeeded (offline dev, endpoint
 * not deployed, Yahoo down) the static DEFAULT_QUOTES snapshot below is shown
 * and the LIVE pip stays hidden — we never fake "liveness". The pip also drops
 * when the API answers with live:false (stale >15min last-good quotes: still
 * shown, just not called live). The fetch is deduped module-wide so the two
 * hero bands share one request.
 */

export interface TickerQuote {
  symbol: string
  price: string
  changePct: number // signed, e.g. +0.62 / -1.14
}

// FALLBACK ONLY — a fixed, realistic snapshot (July 2026 ballpark) shown
// until the live endpoint answers. Futures roots first (the Theta book),
// then the megacap tape everyone scans for.
const DEFAULT_QUOTES: TickerQuote[] = [
  { symbol: 'ES',    price: '7,548.25',  changePct: 0.42 },
  { symbol: 'NQ',    price: '30,528.75', changePct: 0.67 },
  { symbol: 'YM',    price: '52,214',    changePct: 0.18 },
  { symbol: 'RTY',   price: '2,689.40',  changePct: -0.23 },
  { symbol: 'SPY',   price: '752.31',    changePct: 0.39 },
  { symbol: 'QQQ',   price: '736.84',    changePct: 0.61 },
  { symbol: 'NVDA',  price: '214.57',    changePct: 1.24 },
  { symbol: 'AAPL',  price: '283.42',    changePct: 0.31 },
  { symbol: 'MSFT',  price: '568.10',    changePct: -0.12 },
  { symbol: 'TSLA',  price: '412.88',    changePct: -1.06 },
  { symbol: 'AMZN',  price: '296.73',    changePct: 0.54 },
  { symbol: 'META',  price: '842.19',    changePct: 0.88 },
  { symbol: 'GOOGL', price: '241.05',    changePct: 0.22 },
  { symbol: 'AMD',   price: '198.34',    changePct: -0.47 },
  { symbol: 'CL',    price: '81.62',     changePct: 0.95 },
  { symbol: 'GC',    price: '3,412.80',  changePct: -0.29 },
]

// On Vercel/staging the frontend lives at a DIFFERENT origin than the API and
// the SPA rewrite answers any bare relative /api/* fetch with index.html — so
// prefix VITE_API_URL exactly like every other raw fetch() in this codebase
// (see api/client.ts). Empty on the Hetzner box and the dev preview, where
// /api/* is same-origin proxied and a relative URL Just Works.
const API_BASE = ((import.meta.env.VITE_API_URL as string | undefined) || '').replace(/\/+$/, '')
const TAPE_URL = `${API_BASE}/api/v1/public/tape`
const REFRESH_MS = 60_000

/** API price may arrive as a preformatted string or a raw number. */
function formatPrice(p: string | number): string {
  if (typeof p === 'number') {
    return p.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  }
  return p
}

// ── module-level fetch dedupe ────────────────────────────────────────────────
// Both hero bands mount together and poll on the same 60s cadence; share one
// in-flight request + a short freshness window so we hit the endpoint once
// per cycle, not once per band.

/** Parsed tape payload: quotes + the API's own liveness attestation. The
 * backend flips `live` to false once its last-good quotes are too stale to
 * honestly call "live" — we still show those real quotes, but drop the pip. */
interface TapeResult {
  quotes: TickerQuote[]
  live: boolean
}

let _tapeCache: { at: number; data: TapeResult } | null = null
let _tapeInflight: Promise<TapeResult | null> | null = null

async function fetchTapeQuotes(): Promise<TapeResult | null> {
  if (_tapeCache && Date.now() - _tapeCache.at < REFRESH_MS / 2) {
    return _tapeCache.data
  }
  if (!_tapeInflight) {
    _tapeInflight = (async () => {
      try {
        const res = await fetch(TAPE_URL, { headers: { Accept: 'application/json' } })
        if (!res.ok) return null
        const json = await res.json()
        if (!Array.isArray(json?.quotes)) return null
        const parsed: TickerQuote[] = []
        for (const q of json.quotes) {
          if (!q || typeof q.symbol !== 'string' || q.price == null) continue
          if (typeof q.change_pct !== 'number' || !isFinite(q.change_pct)) continue
          parsed.push({ symbol: q.symbol, price: formatPrice(q.price), changePct: q.change_pct })
        }
        if (parsed.length === 0) return null // empty -> keep fallback
        const data: TapeResult = { quotes: parsed, live: json.live === true }
        _tapeCache = { at: Date.now(), data }
        return data
      } catch {
        return null // decorative surface: no error UI, caller keeps current quotes
      } finally {
        _tapeInflight = null
      }
    })()
  }
  return _tapeInflight
}

export interface TickerTapeProps {
  /** Explicit quotes disable the live fetch (band becomes fully static). */
  quotes?: TickerQuote[]
  /** Scroll direction. The hero uses left on top, right on bottom. */
  direction?: 'left' | 'right'
  /** Loop duration in seconds — lower = faster crawl. */
  speed?: number
  className?: string
}

function TickerItem({ q }: { q: TickerQuote }) {
  const up = q.changePct >= 0
  return (
    <span className="v2-ticker__item">
      <span className="v2-ticker__sym">{q.symbol}</span>
      <span className="v2-ticker__price">{q.price}</span>
      <span className={up ? 'v2-ticker__chg v2-ticker__chg--up' : 'v2-ticker__chg v2-ticker__chg--dn'}>
        {up ? '▲' : '▼'} {Math.abs(q.changePct).toFixed(2)}%
      </span>
      <span className="v2-ticker__sep" aria-hidden="true" />
    </span>
  )
}

export default function TickerTape({ quotes: quotesProp, direction = 'left', speed = 46, className }: TickerTapeProps) {
  const [liveData, setLiveData] = useState<TapeResult | null>(null)

  useEffect(() => {
    if (quotesProp) return // caller pinned the data — nothing to fetch
    let cancelled = false
    const load = () => {
      fetchTapeQuotes().then((r) => {
        // Success only — on any failure we silently keep whatever is showing
        // (fallback snapshot or the previous live set): no flicker, no errors.
        if (!cancelled && r) setLiveData(r)
      })
    }
    load()
    const id = window.setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [quotesProp])

  const quotes = quotesProp ?? liveData?.quotes ?? DEFAULT_QUOTES
  // NEVER faked: pip only when a real fetch succeeded AND the API itself says
  // live:true (the backend flips it false once its quotes go stale >15 min).
  const isLive = !quotesProp && liveData?.live === true

  // Row rendered twice inside the track => translateX(-50%) loops seamlessly.
  const row = (dup: boolean) => (
    <span className="v2-ticker__row" aria-hidden={dup || undefined}>
      {quotes.map((q, i) => <TickerItem key={(dup ? 'b' : 'a') + i} q={q} />)}
    </span>
  )
  return (
    <div
      className={['v2-ticker', direction === 'right' ? 'v2-ticker--rev' : '', className || ''].join(' ').trim()}
      role="presentation"
      aria-hidden="true"
    >
      {/* Edge-fade mask lives on the viewport (not the band) so the pip
          below stays fully opaque at the left edge. */}
      <div className="v2-ticker__viewport">
        <div className="v2-ticker__track" style={{ animationDuration: `${speed}s` }}>
          {row(false)}
          {row(true)}
        </div>
      </div>
      {isLive && (
        <span className="v2-ticker__live">
          <span className="v2-ticker__live-dot" />
          LIVE
        </span>
      )}
    </div>
  )
}
