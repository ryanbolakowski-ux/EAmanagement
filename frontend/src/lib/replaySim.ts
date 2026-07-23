// ─────────────────────────────────────────────────────────────────────────────
// Replay order simulation — pure TypeScript, no React/DOM imports, so it can
// be unit-tested directly. Used by pages/Replay.tsx.
//
// House conventions honored here:
//  * Market fills at the CURRENT bar close (the last revealed bar).
//  * On each newly revealed bar, the STOP is checked BEFORE the target — if a
//    single bar's range spans both levels we assume the worst (conservative).
//  * Fills happen exactly at the level price (no slippage in v1).
// ─────────────────────────────────────────────────────────────────────────────

export type SimBar = { time: number; open: number; high: number; low: number; close: number }
export type Direction = 'long' | 'short'
export type ExitReason = 'stop' | 'target' | 'manual' | 'session_end'
export type TargetSpec = { kind: 'r' | 'points'; value: number }

// $ per 1.00 point of the underlying future, per contract.
export const POINT_VALUES: Record<string, number> = {
  ES: 50,
  NQ: 20,
  YM: 5,
  RTY: 50,
}

export type OpenPosition = {
  direction: Direction
  qty: number
  entryPrice: number
  entryTime: number
  stopPrice: number
  targetPrice: number
  riskPoints: number
}

export type ClosedTrade = {
  direction: Direction
  qty: number
  entryPrice: number
  entryTime: number
  exitPrice: number
  exitTime: number
  exitReason: ExitReason
  points: number   // per contract, signed
  r: number        // points / riskPoints, signed
  dollars: number  // points * pointValue * qty, signed
}

// Open a market position at the current bar's close.
export function openPosition(
  direction: Direction,
  qty: number,
  entryBar: SimBar,
  stopPoints: number,
  target: TargetSpec,
): OpenPosition {
  const dir = direction === 'long' ? 1 : -1
  const entryPrice = entryBar.close
  const targetPoints = target.kind === 'r' ? stopPoints * target.value : target.value
  return {
    direction,
    qty,
    entryPrice,
    entryTime: entryBar.time,
    stopPrice: entryPrice - dir * stopPoints,
    targetPrice: entryPrice + dir * targetPoints,
    riskPoints: stopPoints,
  }
}

// Check a newly revealed bar against the open position's levels.
// Stop before target on the same bar (conservative, house convention).
export function checkExit(pos: OpenPosition, bar: SimBar): { price: number; reason: 'stop' | 'target' } | null {
  if (pos.direction === 'long') {
    if (bar.low <= pos.stopPrice) return { price: pos.stopPrice, reason: 'stop' }
    if (bar.high >= pos.targetPrice) return { price: pos.targetPrice, reason: 'target' }
  } else {
    if (bar.high >= pos.stopPrice) return { price: pos.stopPrice, reason: 'stop' }
    if (bar.low <= pos.targetPrice) return { price: pos.targetPrice, reason: 'target' }
  }
  return null
}

// Signed P&L in points per contract at a given price.
export function pnlPoints(pos: OpenPosition, price: number): number {
  return (price - pos.entryPrice) * (pos.direction === 'long' ? 1 : -1)
}

export function unrealized(pos: OpenPosition, price: number, pointValue: number): { points: number; r: number; dollars: number } {
  const points = pnlPoints(pos, price)
  return {
    points,
    r: pos.riskPoints > 0 ? points / pos.riskPoints : 0,
    dollars: points * pointValue * pos.qty,
  }
}

export function closePosition(
  pos: OpenPosition,
  exitPrice: number,
  exitTime: number,
  reason: ExitReason,
  pointValue: number,
): ClosedTrade {
  const points = pnlPoints(pos, exitPrice)
  return {
    direction: pos.direction,
    qty: pos.qty,
    entryPrice: pos.entryPrice,
    entryTime: pos.entryTime,
    exitPrice,
    exitTime,
    exitReason: reason,
    points,
    r: pos.riskPoints > 0 ? points / pos.riskPoints : 0,
    dollars: points * pointValue * pos.qty,
  }
}

// Aggregate 1-minute bars into tfMinutes-bucket OHLC bars. Called with the
// REVEALED slice only, so a partially formed bucket renders as a live,
// still-forming candle — exactly what a real chart would show.
export function aggregateBars(bars: SimBar[], tfMinutes: number): SimBar[] {
  if (tfMinutes <= 1) return bars
  const bucketSecs = tfMinutes * 60
  const out: SimBar[] = []
  let cur: SimBar | null = null
  let curBucket = Number.NaN
  for (const b of bars) {
    const bucket = Math.floor(b.time / bucketSecs)
    if (bucket !== curBucket) {
      if (cur) out.push(cur)
      curBucket = bucket
      cur = { time: bucket * bucketSecs, open: b.open, high: b.high, low: b.low, close: b.close }
    } else if (cur) {
      if (b.high > cur.high) cur.high = b.high
      if (b.low < cur.low) cur.low = b.low
      cur.close = b.close
    }
  }
  if (cur) out.push(cur)
  return out
}

export type SessionStats = {
  trades: number
  wins: number
  losses: number
  winRate: number | null  // null when no trades
  totalR: number
  totalDollars: number
  avgR: number | null
}

export function sessionStats(trades: ClosedTrade[]): SessionStats {
  const wins = trades.filter((t) => t.dollars > 0).length
  const losses = trades.filter((t) => t.dollars < 0).length
  const totalR = trades.reduce((a, t) => a + t.r, 0)
  const totalDollars = trades.reduce((a, t) => a + t.dollars, 0)
  return {
    trades: trades.length,
    wins,
    losses,
    winRate: trades.length > 0 ? (wins / trades.length) * 100 : null,
    totalR,
    totalDollars,
    avgR: trades.length > 0 ? totalR / trades.length : null,
  }
}
