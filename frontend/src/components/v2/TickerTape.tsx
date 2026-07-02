import React from 'react'

/**
 * TickerTape — a Wall-Street-building style LED price crawl.
 *
 * Two of these bands frame the LandingV2 hero (top band scrolls left, bottom
 * band scrolls right) to evoke the NYSE ticker wrapping a building. Pure CSS
 * animation (see v2.css §19): the row is rendered twice inside one track and
 * translated -50% for a seamless infinite loop; prefers-reduced-motion gets a
 * static row.
 *
 * DATA IS DECORATIVE: a fixed, realistic snapshot (July 2026 ballpark), not a
 * live feed. The landing page is public/unauthenticated so there is no quote
 * endpoint to call; when a real-time feed lands this can be wired to it
 * (TODO-LIVE-TICKER). Values are deliberately static per build so we never
 * fake "liveness".
 */

export interface TickerQuote {
  symbol: string
  price: string
  changePct: number // signed, e.g. +0.62 / -1.14
}

// Futures roots first (the Theta book), then the megacap tape everyone scans for.
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

export interface TickerTapeProps {
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
      <span className="v2-ticker__dot" aria-hidden="true">•</span>
    </span>
  )
}

export default function TickerTape({ quotes = DEFAULT_QUOTES, direction = 'left', speed = 46, className }: TickerTapeProps) {
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
      <div className="v2-ticker__track" style={{ animationDuration: `${speed}s` }}>
        {row(false)}
        {row(true)}
      </div>
    </div>
  )
}
