import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * DEV-PREVIEW ONLY — live quotes for the LandingV2 hero TickerTape.
 *
 * Production serves GET /api/v1/public/tape from the FastAPI backend
 * (backend/app/api/routes/public_tape.py). The preview backend container is
 * the LIVE prod image and does not have that route yet, so this tiny
 * middleware answers the same path with the same JSON shape, fetched
 * node-side from Yahoo's public chart API (global fetch, Node >= 18 — no
 * new dependencies).
 *
 * This CANNOT leak into production: `configureServer` only runs for the
 * dev server (`vite dev`); `vite build` / Vercel ignore it entirely.
 *
 * Registered inside configureServer (not the returned post-hook), so it runs
 * BEFORE Vite's internal middlewares — including the /api proxy below —
 * otherwise the proxy would swallow the path and the prod backend would 404.
 */

// Same fixed symbol map as the backend route: yahoo ticker -> display symbol.
const TAPE_SYMBOLS: Array<[string, string]> = [
  ['ES=F', 'ES'], ['NQ=F', 'NQ'], ['YM=F', 'YM'], ['RTY=F', 'RTY'],
  ['SPY', 'SPY'], ['QQQ', 'QQQ'], ['NVDA', 'NVDA'], ['AAPL', 'AAPL'],
  ['MSFT', 'MSFT'], ['TSLA', 'TSLA'], ['AMZN', 'AMZN'], ['META', 'META'],
  ['GOOGL', 'GOOGL'], ['AMD', 'AMD'], ['CL=F', 'CL'], ['GC=F', 'GC'],
]

interface TapeQuote { symbol: string; price: string; change_pct: number }

/** Fetch one symbol's meta from Yahoo; null on any problem (caller skips). */
async function fetchYahooQuote(yahooSym: string, display: string): Promise<TapeQuote | null> {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), 5000) // ~5s per-symbol budget
  try {
    // range=1d, NOT 2d: at range=1d Yahoo's meta.chartPreviousClose IS the
    // true prior-session close, so change_pct is a real daily change and
    // matches the shipped backend (last two daily closes). At range=2d
    // chartPreviousClose is the close BEFORE the 2-day window — a 2-session
    // change, roughly double the daily move.
    const url =
      `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(yahooSym)}` +
      `?range=1d&interval=1d`
    const res = await fetch(url, {
      signal: ctrl.signal,
      // Yahoo 429s UA-less clients; any browser-ish UA passes.
      headers: { 'User-Agent': 'Mozilla/5.0 (dev-preview ticker)' },
    })
    if (!res.ok) return null
    const json: any = await res.json()
    const meta = json?.chart?.result?.[0]?.meta
    const last = Number(meta?.regularMarketPrice)
    const prev = Number(meta?.chartPreviousClose)
    if (!Number.isFinite(last) || !Number.isFinite(prev) || prev <= 0) return null
    return {
      symbol: display,
      // Match the backend's comma-grouped 2dp formatting exactly.
      price: last.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
      change_pct: Math.round(((last - prev) / prev) * 10000) / 100,
    }
  } catch {
    return null // timeout / network / parse — symbol is simply skipped
  } finally {
    clearTimeout(timer)
  }
}

function devTapeQuotes(): Plugin {
  // 60s in-memory cache (mirrors the backend TTL) so hot-reloads and the two
  // hero bands don't hammer Yahoo.
  let cache: { at: number; body: string } | null = null
  return {
    name: 'theta-dev-tape-quotes',
    configureServer(server) {
      server.middlewares.use('/api/v1/public/tape', async (req, res, next) => {
        if ((req.method || 'GET').toUpperCase() !== 'GET') return next()
        try {
          if (cache && Date.now() - cache.at < 60_000) {
            res.statusCode = 200
            res.setHeader('Content-Type', 'application/json')
            res.end(cache.body)
            return
          }
          const settled = await Promise.allSettled(
            TAPE_SYMBOLS.map(([y, d]) => fetchYahooQuote(y, d)),
          )
          const quotes: TapeQuote[] = []
          for (const r of settled) {
            if (r.status === 'fulfilled' && r.value) quotes.push(r.value)
          }
          const body = JSON.stringify({
            as_of: new Date().toISOString(),
            live: quotes.length > 0,
            quotes,
          })
          if (quotes.length > 0) cache = { at: Date.now(), body }
          res.statusCode = 200
          res.setHeader('Content-Type', 'application/json')
          res.end(body)
        } catch {
          // Decorative endpoint: ALWAYS 200, never a dev-server stack trace.
          res.statusCode = 200
          res.setHeader('Content-Type', 'application/json')
          res.end(JSON.stringify({ as_of: new Date().toISOString(), live: false, quotes: [] }))
        }
      })
    },
  }
}

export default defineConfig({
  plugins: [react(), devTapeQuotes()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    allowedHosts: ['thetaalgos.com', 'www.thetaalgos.com'],
    proxy: {
      '/api': {
        target: process.env.VITE_API_URL || 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
