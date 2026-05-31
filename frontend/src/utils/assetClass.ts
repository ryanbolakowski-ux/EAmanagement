// TypeScript mirror of backend/app/engines/strategy_classification.py.
// MUST be kept in sync with that file — the live-deploy UI uses these to
// pre-filter strategy choices, and the backend uses the Python copy to reject
// any deploy that slipped past the UI filter.
//
// The DB has no `asset_class` column on strategies; the class is implicit in
// the instrument list. classifyAssetClass() derives it; brokerSupports() says
// whether a given broker can route that class.

// Major US futures roots (CME group + a few common others) plus their micros.
export const FUTURES_SYMBOLS: ReadonlySet<string> = new Set([
  // Equity index
  'ES', 'NQ', 'RTY', 'YM',
  'MES', 'MNQ', 'M2K', 'MYM',
  // Energy
  'CL', 'NG', 'RB', 'HO', 'MCL',
  // Metals
  'GC', 'SI', 'HG', 'PL', 'MGC', 'SIL',
  // Treasuries
  'ZB', 'ZN', 'ZF', 'ZT', 'UB',
  // FX (alphabetic CME roots, not ISO pairs)
  '6E', '6J', '6B', '6A', '6C', '6S', '6N',
  // Ag
  'ZC', 'ZS', 'ZW', 'ZL', 'ZM',
  // Crypto
  'BTC', 'MBT', 'ETH', 'MET',
])

// OCC 21-character option symbol: 1-6 char root + YYMMDD + C/P + 8-digit strike.
// Example: SPY240517C00500000 → SPY, 2024-05-17, call, strike $500.00.
export const OCC_OPTION_RE = /^[A-Z]{1,6}\d{6}[CP]\d{8}$/

export type AssetClass = 'futures' | 'options' | 'stock' | 'unknown'

/** Derive a strategy's asset class from its instruments list.
 *
 * 'unknown' indicates an empty list — template strategies with no symbols
 * configured cannot be deployed live.
 *
 * Order matters: options wins over futures wins over stock. A strategy that
 * mixes ES with SPY is classified as futures so the stricter routing rule
 * applies — better to reject than to fire a futures order at a stock-only
 * Tradier account.
 */
export function classifyAssetClass(instruments: readonly string[] | null | undefined): AssetClass {
  if (!instruments || instruments.length === 0) return 'unknown'
  const syms = instruments.map(s => (s || '').toUpperCase().trim()).filter(Boolean)
  if (syms.length === 0) return 'unknown'
  if (syms.some(s => OCC_OPTION_RE.test(s))) return 'options'
  if (syms.some(s => FUTURES_SYMBOLS.has(s))) return 'futures'
  return 'stock'
}

// Which asset classes each broker can route. Schwab/IBKR are listed for
// forward compatibility; on the prod stack only Tradier (stock/options) and
// Tradovate (futures) are wired end-to-end today.
export const BROKER_ASSET_CLASSES: Readonly<Record<string, ReadonlyArray<AssetClass>>> = {
  tradier:   ['stock', 'options'],
  alpaca:    ['stock', 'options'],
  tradovate: ['futures'],
  schwab:    ['stock', 'options', 'futures'],
  webull:    ['stock', 'options'],
  ibkr:      ['stock', 'options', 'futures'],
}

export function brokerSupports(broker: string, assetClass: AssetClass): boolean {
  const supported = BROKER_ASSET_CLASSES[(broker || '').toLowerCase()] || []
  return supported.includes(assetClass)
}

export function supportedClasses(broker: string): ReadonlyArray<AssetClass> {
  return BROKER_ASSET_CLASSES[(broker || '').toLowerCase()] || []
}
