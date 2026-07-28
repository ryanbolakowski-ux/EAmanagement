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
  // GOAL G: SL/TP are optional — null when the trader hasn't set that level.
  // These can be edited mid-trade (set/move/clear) from the trade panel or a
  // place-on-chart click; riskPoints below stays fixed at its ENTRY value.
  stopPrice: number | null
  targetPrice: number | null
  // Risk locked in at ENTRY time (|entry - initial stop|). 0 when the position
  // opened with no stop — in that case r math is undefined (null) forever, even
  // if a stop is added later.
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
  points: number         // per contract, signed
  r: number | null       // points / riskPoints, signed; null when no stop at entry
  dollars: number        // points * pointValue * qty, signed
}

// ── GOAL 2: resting LIMIT order (pure sim, additive) ─────────────────────────
// A single resting order can exist while FLAT. Unlike a market order it does not
// open immediately — the reveal loop tests each newly revealed bar with
// checkLimitFill(); on a fill the order becomes an OpenPosition via
// openFromLimit(). stopPrice / targetPrice are ABSOLUTE prices (already resolved
// off the limit), each null when the trader left that bracket blank — exactly
// the OpenPosition convention, so a filled order behaves identically thereafter.
export type PendingOrder = {
  direction: Direction
  qty: number
  limitPrice: number
  stopPrice: number | null
  targetPrice: number | null
}

// Build a resting limit order, resolving the OPTIONAL stop/target off the limit
// price the same way openPosition() resolves them off the entry close. This lets
// the ticket feed point-based inputs (stop distance in points, target as an
// r-multiple or points) and keep MARKET and LIMIT flows symmetric. Passing
// null/undefined (or a non-positive stop) yields a bare limit with no bracket;
// an 'r' target with no stop simply yields no target (same as market).
export function makePendingOrder(
  direction: Direction,
  qty: number,
  limitPrice: number,
  stopPoints?: number | null,
  target?: TargetSpec | null,
): PendingOrder {
  const dir = direction === 'long' ? 1 : -1
  const risk = stopPoints != null && stopPoints > 0 ? stopPoints : null
  let targetPrice: number | null = null
  if (target != null) {
    const tp = target.kind === 'r'
      ? (risk != null ? risk * target.value : null)
      : target.value
    if (tp != null && tp > 0) targetPrice = limitPrice + dir * tp
  }
  return {
    direction,
    qty,
    limitPrice,
    stopPrice: risk != null ? limitPrice - dir * risk : null,
    targetPrice,
  }
}

// Test a newly revealed bar for a resting-limit fill. A LONG limit rests BELOW
// price and fills when the bar trades down to it (bar.low <= limit); a SHORT
// limit rests ABOVE and fills when the bar trades up to it (bar.high >= limit).
// Fills exactly at the limit price (no slippage / no gap improvement in v1),
// matching the stop/target fill convention. null when the bar didn't reach it.
export function checkLimitFill(order: PendingOrder, bar: SimBar): { price: number } | null {
  if (order.direction === 'long') {
    if (bar.low <= order.limitPrice) return { price: order.limitPrice }
  } else {
    if (bar.high >= order.limitPrice) return { price: order.limitPrice }
  }
  return null
}

// Promote a FILLED limit order into an OpenPosition. Entry is the limit price
// (v1: exact fill); risk is |entry − stop| when a stop was set, else 0 so r math
// stays null forever — identical semantics to openPosition(). The filled trade
// then records normally through checkExit / closePosition, so stats + markers
// include it with no special-casing.
export function openFromLimit(
  order: PendingOrder,
  fillTime: number,
  fillPrice: number = order.limitPrice,
): OpenPosition {
  const risk = order.stopPrice != null ? Math.abs(fillPrice - order.stopPrice) : null
  return {
    direction: order.direction,
    qty: order.qty,
    entryPrice: fillPrice,
    entryTime: fillTime,
    stopPrice: order.stopPrice,
    targetPrice: order.targetPrice,
    riskPoints: risk ?? 0,
  }
}

// Open a market position at the current bar's close. GOAL G: both the stop and
// the target are OPTIONAL — pass null/undefined (or a non-positive stop) to open
// a bare market order, exactly like FX Replay. An 'r'-based target needs a stop
// to size off; with no stop it simply yields no target.
export function openPosition(
  direction: Direction,
  qty: number,
  entryBar: SimBar,
  stopPoints?: number | null,
  target?: TargetSpec | null,
): OpenPosition {
  const dir = direction === 'long' ? 1 : -1
  const entryPrice = entryBar.close
  const risk = stopPoints != null && stopPoints > 0 ? stopPoints : null
  let targetPrice: number | null = null
  if (target != null) {
    const tp = target.kind === 'r'
      ? (risk != null ? risk * target.value : null)
      : target.value
    if (tp != null && tp > 0) targetPrice = entryPrice + dir * tp
  }
  return {
    direction,
    qty,
    entryPrice,
    entryTime: entryBar.time,
    stopPrice: risk != null ? entryPrice - dir * risk : null,
    targetPrice,
    riskPoints: risk ?? 0,
  }
}

// Check a newly revealed bar against the open position's levels. An absent
// level (null) is simply skipped, so a bare market order never exits on price.
// Stop before target on the same bar (conservative, house convention).
export function checkExit(pos: OpenPosition, bar: SimBar): { price: number; reason: 'stop' | 'target' } | null {
  if (pos.direction === 'long') {
    if (pos.stopPrice != null && bar.low <= pos.stopPrice) return { price: pos.stopPrice, reason: 'stop' }
    if (pos.targetPrice != null && bar.high >= pos.targetPrice) return { price: pos.targetPrice, reason: 'target' }
  } else {
    if (pos.stopPrice != null && bar.high >= pos.stopPrice) return { price: pos.stopPrice, reason: 'stop' }
    if (pos.targetPrice != null && bar.low <= pos.targetPrice) return { price: pos.targetPrice, reason: 'target' }
  }
  return null
}

// Signed P&L in points per contract at a given price.
export function pnlPoints(pos: OpenPosition, price: number): number {
  return (price - pos.entryPrice) * (pos.direction === 'long' ? 1 : -1)
}

export function unrealized(pos: OpenPosition, price: number, pointValue: number): { points: number; r: number | null; dollars: number } {
  const points = pnlPoints(pos, price)
  return {
    points,
    r: pos.riskPoints > 0 ? points / pos.riskPoints : null,
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
    r: pos.riskPoints > 0 ? points / pos.riskPoints : null,
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
  winRate: number | null  // null when no trades. GOAL D/G: win = dollars > 0.
  totalR: number          // sums only trades that HAVE an r (initial stop)
  rCount: number          // how many trades contributed to totalR / avgR
  totalDollars: number
  avgR: number | null     // averaged over rCount, not over all trades
}

export function sessionStats(trades: ClosedTrade[]): SessionStats {
  // GOAL G: a win is any trade that made money (works for every trade, incl.
  // those opened without a stop, whose r is null). R totals only aggregate
  // trades that carried an initial stop so an unmanaged runner can't skew them.
  const wins = trades.filter((t) => t.dollars > 0).length
  const losses = trades.filter((t) => t.dollars < 0).length
  const rTrades = trades.filter((t) => t.r != null) as (ClosedTrade & { r: number })[]
  const totalR = rTrades.reduce((a, t) => a + t.r, 0)
  const totalDollars = trades.reduce((a, t) => a + t.dollars, 0)
  return {
    trades: trades.length,
    wins,
    losses,
    winRate: trades.length > 0 ? (wins / trades.length) * 100 : null,
    totalR,
    rCount: rTrades.length,
    totalDollars,
    avgR: rTrades.length > 0 ? totalR / rTrades.length : null,
  }
}
