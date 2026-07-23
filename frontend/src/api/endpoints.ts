import api from './client'
import type {
  TokenResponse, User, Strategy, StrategyCreate,
  BacktestRun, BacktestMetrics, Trade, DashboardSummary,
} from '../types'
import type { MyAccess, BrokerAccountLite } from '../types/access'

// ─────────────────────────────────────────────────────────────────────────────
// Auth
// ─────────────────────────────────────────────────────────────────────────────

export type LoginResponse = {
  requires_2fa: boolean
  challenge_token: string | null
  access_token: string | null
  refresh_token: string | null
  token_type: string
  user_id: string | null
  email: string | null
  subscription_tier: string | null
}

export const authApi = {
  login: (email: string, password: string) =>
    api.post<LoginResponse>('/api/v1/auth/login', new URLSearchParams({ username: email, password }), {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    }),

  verify2FA: (challenge_token: string, code: string) =>
    api.post<TokenResponse>('/api/v1/auth/verify-2fa', { challenge_token, code }),

  register: (email: string, username: string, password: string) =>
    api.post<TokenResponse>('/api/v1/auth/register', { email, username, password }),

  me: () => api.get<User>('/api/v1/auth/me'),

  forgotPassword: (email: string) =>
    api.post<{ status: string; detail: string }>('/api/v1/auth/forgot-password', { email }),

  resetPassword: (token: string, new_password: string) =>
    api.post<{ status: string }>('/api/v1/auth/reset-password', { token, new_password }),

  setup2FA: () =>
    api.post<{ secret: string; otpauth_url: string }>('/api/v1/auth/2fa/setup'),

  confirm2FA: (code: string) =>
    api.post<{ status: string; totp_enabled: boolean }>('/api/v1/auth/2fa/confirm', { code }),

  disable2FA: (code: string) =>
    api.post<{ status: string; totp_enabled: boolean }>('/api/v1/auth/2fa/disable', { code }),
}

// ─────────────────────────────────────────────────────────────────────────────
// Strategies
// ─────────────────────────────────────────────────────────────────────────────

export const strategiesApi = {
  list: () => api.get<Strategy[]>('/api/v1/strategies/'),
  create: (data: StrategyCreate) => api.post<Strategy>('/api/v1/strategies/', data),
  get: (id: string) => api.get<Strategy>(`/api/v1/strategies/${id}`),
  update: (id: string, data: StrategyCreate) => api.put<Strategy>(`/api/v1/strategies/${id}`, data),
  delete: (id: string) => api.delete(`/api/v1/strategies/${id}`),
  setStarred: (id: string, starred: boolean) => api.patch<Strategy>(`/api/v1/strategies/${id}/star`, { starred }),
  share:         (id: string)    => api.post(`/api/v1/strategies/${id}/share`),
  revokeShare:   (id: string)    => api.delete(`/api/v1/strategies/${id}/share`),
  previewShared: (token: string) => api.get(`/api/v1/strategies/shared/${token}/preview`),
  importShared:  (token: string) => api.post<Strategy>(`/api/v1/strategies/shared/${token}/import`),
}

// ─────────────────────────────────────────────────────────────────────────────
// Backtests
// ─────────────────────────────────────────────────────────────────────────────

export const backtestsApi = {
  list: () => api.get<BacktestRun[]>('/api/v1/backtests/'),
  run: (data: {
    strategy_id: string
    instrument: string
    start_date: string
    end_date: string
    timeframe?: string
    initial_capital: number
    slippage_ticks: number
    commission_per_side: number
  }) => api.post<BacktestRun>('/api/v1/backtests/', data),
  getMetrics: (id: string) => api.get<BacktestMetrics>(`/api/v1/backtests/${id}/metrics`),
  delete: (id: string) => api.delete(`/api/v1/backtests/${id}`),
  getTrades: (id: string) => api.get(`/api/v1/backtests/${id}/trades`),
  getChartData: (id: string) => api.get(`/api/v1/backtests/${id}/chart-data`),
}

// ─────────────────────────────────────────────────────────────────────────────
// Optimization
// ─────────────────────────────────────────────────────────────────────────────

// Single source of truth for the default optimization grid. Both the
// standalone Optimization page and the backtest "Optimize" button use this
// so they test identical parameter combinations on the same engine.
export const DEFAULT_OPT_GRID: Record<string, number[]> = {
  risk_reward_ratio: [1.5, 2.0, 2.5, 3.0],
  stop_loss_ticks: [8, 10, 12, 16],
  fvg_min_size_ticks: [2, 4, 6],
}

export const optimizationApi = {
  list: () => api.get('/api/v1/optimization/'),
  start: (data: {
    strategy_id: string
    instrument: string
    start_date: string
    end_date: string
    parameter_grid: Record<string, number[] | string[]>
    optimization_metric: string
  }) => api.post('/api/v1/optimization/', data),
  getResults: (runId: string) => api.get(`/api/v1/optimization/${runId}/results`),
  delete: (runId: string) => api.delete(`/api/v1/optimization/${runId}`),
  apply: (runId: string, rank: number = 1) => api.post(`/api/v1/optimization/${runId}/apply?rank=${rank}`),
  retry: (runId: string) => api.post(`/api/v1/optimization/${runId}/retry`),
}

// ─────────────────────────────────────────────────────────────────────────────
// Paper & Live Trading
// ─────────────────────────────────────────────────────────────────────────────

export const paperTradingApi = {
  listSessions: () => api.get('/api/v1/paper-trading/sessions'),
  startSession: (data: { strategy_id: string; instruments: string[]; daily_loss_limit?: number }) =>
    api.post('/api/v1/paper-trading/sessions', data),
  stopSession: (id: string) => api.post(`/api/v1/paper-trading/sessions/${id}/stop`),
  stopAllSessions: () => api.post('/api/v1/paper-trading/sessions/stop-all'),
  closeAllOpenPositions: () => api.post<{ closed: number }>('/api/v1/paper-trading/positions/close-all'),
  deleteSession: (id: string) => api.delete(`/api/v1/paper-trading/sessions/${id}`),
  setLabel: (id: string, label: string | null) =>
    api.patch(`/api/v1/paper-trading/sessions/${id}/label`, { label }),
  setAllocation: (id: string, starting_balance: number) =>
    api.patch(`/api/v1/paper-trading/sessions/${id}/allocation`, { starting_balance }),
  getSessionDetail: (id: string) =>
    api.get(`/api/v1/paper-trading/sessions/${id}`),
  getTradeChart: (tradeId: string) =>
    api.get(`/api/v1/paper-trading/trades/${tradeId}/chart`),
}

// Row shape of GET /api/v1/live-trading/sessions (see backend
// live_trading.list_live_sessions — active + recently stopped, max 50).
export type LiveSessionRow = {
  id: string
  strategy_id: string
  strategy_name: string
  broker_account_id: string | null
  broker_account_name: string
  broker: string
  instrument: string
  is_active: boolean
  started_at: string | null
  ended_at: string | null
  total_trades: number
  net_pnl: number
  daily_loss_limit: number | null
}

export const liveTradingApi = {
  listAccounts: () => api.get<BrokerAccountLite[]>('/api/v1/live-trading/accounts'),
  addAccount: (data: { account_name: string; broker: string; is_demo: boolean; credentials: object }) =>
    api.post('/api/v1/live-trading/accounts', data),
  testConnection: (data: { broker: string; is_demo: boolean; credentials: object }) =>
    api.post('/api/v1/live-trading/accounts/test-connection', data),
  setSandboxMode: (accountId: string, sandbox_mode: boolean) => api.patch(`/api/v1/live-trading/accounts/${accountId}/sandbox-mode`, { sandbox_mode }),
  setTradingEnabled: (accountId: string, trading_enabled: boolean) =>
    api.patch(`/api/v1/live-trading/accounts/${accountId}/trading-enabled`, { trading_enabled }),
  setConsistency: (accountId: string, profit_target: number | null, consistency_pct: number | null) =>
    api.patch(`/api/v1/live-trading/accounts/${accountId}/consistency`, { profit_target, consistency_pct }),
  checkConsistency: (accountId: string) =>
    api.post(`/api/v1/live-trading/accounts/${accountId}/check-consistency`),
  setAccountLabel: (accountId: string, label: string) =>
    api.patch(`/api/v1/live-trading/accounts/${accountId}/label`, { label }),
  getAccountDetail: (accountId: string) =>
    api.get(`/api/v1/live-trading/accounts/${accountId}/detail`),
  startSession: (data: { strategy_id: string; broker_account_id: string; instrument: string }) =>
    api.post('/api/v1/live-trading/sessions', data),
  // Typed wrapper for the existing GET /sessions route. LiveTrading.tsx was
  // already calling this via an `as any` cast — the method just didn't exist
  // here, so that call resolved to undefined at runtime. Now it's real.
  listSessions: () => api.get<LiveSessionRow[]>('/api/v1/live-trading/sessions'),
  killSwitch: (sessionId: string) => api.post(`/api/v1/live-trading/sessions/${sessionId}/kill-switch`),
  pauseSession: (sessionId: string) => api.post(`/api/v1/live-trading/sessions/${sessionId}/pause`),
  resumeSession: (sessionId: string) => api.post(`/api/v1/live-trading/sessions/${sessionId}/resume`),
  unrealizedPnl: () => api.get("/api/v1/live-trading/unrealized-pnl"),
  // Sizing
  getBalance: (accountId: string) =>
    api.get(`/api/v1/live-trading/accounts/${accountId}/balance`),
  portfolioSummary: () => api.get("/api/v1/live-trading/portfolio-summary"),
  getSizing: (accountId: string) =>
    api.get(`/api/v1/live-trading/accounts/${accountId}/sizing`),
  saveSizing: (accountId: string, data: {
    account_type: string;
    risk_per_trade_usd: number | null;
    risk_per_trade_pct: number | null;
    max_position_usd: number | null;
  }) => api.patch(`/api/v1/live-trading/accounts/${accountId}/sizing`, data),
}

// ─────────────────────────────────────────────────────────────────────────────
// Trades & Dashboard
// ─────────────────────────────────────────────────────────────────────────────

export const tradesApi = {
  list: (params?: { mode?: string; strategy_id?: string; limit?: number }) =>
    api.get<Trade[]>('/api/v1/trades/', { params }),
  getChartData: (mode: string, instrument: string) =>
    api.get('/api/v1/trades/chart-data', { params: { mode, instrument } }),
  openPositions: () => api.get('/api/v1/trades/open-positions'),
}

export type DrawTarget = {
  label: string
  level: number
  side: 'above' | 'below'
}

export type DailyBias = {
  instrument: string
  bias: 'strong_bullish' | 'bullish' | 'neutral' | 'bearish' | 'strong_bearish'
  strength_pct: number
  last_close: number | null
  ema_fast: number | null
  ema_slow: number | null
  as_of: string | null
  // ── ICT enrichment (all optional for backward-compat) ───────────────────
  trend?: 'strong_bullish' | 'bullish' | 'neutral' | 'bearish' | 'strong_bearish'
  trend_strength_pct?: number
  pdh?: number | null
  pdl?: number | null
  pdc?: number | null
  position_vs_pd?: 'above_pdh' | 'below_pdl' | 'inside' | 'unknown'
  opening_type?: 'gap_up' | 'gap_down' | 'inside' | 'pending' | 'unknown'
  asian_high?: number | null
  asian_low?: number | null
  pdh_swept?: boolean
  pdl_swept?: boolean
  asian_swept_high?: boolean
  asian_swept_low?: boolean
  current_session?: 'asian' | 'london' | 'ny' | 'overnight' | 'unknown'
  draw_target?: DrawTarget | null
  narrative?: string
}

export type BiasFvg = {
  direction: 'bullish' | 'bearish'
  high: number
  low: number
  ce: number
  size_ticks: number
  filled: boolean
  timestamp: string
  respected: boolean
}

export type BiasDetail = {
  instrument: string
  bias: DailyBias['bias']
  strength_pct: number
  last_close: number | null
  ema_fast: number | null
  ema_slow: number | null
  candles: { time: number; open: number; high: number; low: number; close: number }[]
  ema_fast_series: number[]
  ema_slow_series: number[]
  htf_fvgs: BiasFvg[]
  summary: string
  // ── ICT enrichment ─────────────────────────────────────────────────────
  trend?: DailyBias['bias']
  trend_strength_pct?: number
  pdh?: number | null
  pdl?: number | null
  pdc?: number | null
  position_vs_pd?: 'above_pdh' | 'below_pdl' | 'inside' | 'unknown'
  opening_type?: 'gap_up' | 'gap_down' | 'inside' | 'pending' | 'unknown'
  asian_high?: number | null
  asian_low?: number | null
  pdh_swept?: boolean
  pdl_swept?: boolean
  asian_swept_high?: boolean
  asian_swept_low?: boolean
  current_session?: 'asian' | 'london' | 'ny' | 'overnight' | 'unknown'
  draw_target?: DrawTarget | null
  narrative?: string
}

export const dashboardApi = {
  summary:    () => api.get<DashboardSummary>('/api/v1/dashboard/summary'),
  bias:       () => api.get<{ biases: DailyBias[] }>('/api/v1/dashboard/bias'),
  biasDetail: () => api.get<{ instruments: BiasDetail[] }>('/api/v1/dashboard/bias/detail'),
}


// ─────────────────────────────────────────────────────────────────────────────
// Profile
// ─────────────────────────────────────────────────────────────────────────────

export const profileApi = {
  getProfile: () => api.get('/api/v1/profile/me'),
  upgrade: (data: { tier: string; promo_code?: string }) =>
    api.post('/api/v1/profile/upgrade', data),
}

// ─────────────────────────────────────────────────────────────────────────────
// Billing (Stripe)
// ─────────────────────────────────────────────────────────────────────────────

export const billingApi = {
  createCheckout: (tier: string) => api.post('/api/v1/billing/create-checkout', { tier }),
  cancelSubscription: () => api.post('/api/v1/billing/cancel'),
  getPortal: () => api.get('/api/v1/billing/portal'),
}

// ─────────────────────────────────────────────────────────────────────────────
// Legal disclosures
// ─────────────────────────────────────────────────────────────────────────────

export type LegalKind =
  | 'terms_of_service'
  | 'risk_disclosure'
  | 'live_trading_consent'
  | 'options_trading_consent'
  | 'fully_automated_trading'
  | 'signals_disclosure'
  | 'risk_change'

export type LegalDocument = {
  kind: LegalKind
  title: string
  version: string
  html: string
}

export type LegalAckStatus = {
  acknowledgments: Record<LegalKind, { current_version: string; accepted: boolean }>
}

export const legalApi = {
  document: (kind: LegalKind) =>
    api.get<LegalDocument>(`/api/v1/legal/documents/${kind}`),
  status: () =>
    api.get<LegalAckStatus>('/api/v1/legal/status'),
  acknowledge: (kind: LegalKind, detail?: string) =>
    api.post('/api/v1/legal/acknowledge', { kind, detail }),
}

// ─────────────────────────────────────────────────────────────────────────────
// Plan access (Phase G) — tier capabilities + automation status
// ─────────────────────────────────────────────────────────────────────────────

export const accountSignalsApi = {
  myAccess: () => api.get<MyAccess>('/api/v1/account-signals/my-access'),
}

// ─────────────────────────────────────────────────────────────────────────────
// Security — email verification codes (e.g. enable_automation)
// ─────────────────────────────────────────────────────────────────────────────

export type VerifyPurpose = 'enable_automation' | string

export type VerifyCodeRequestResult = {
  sent: boolean
  purpose: string
  expires_in_min: number
}

export type VerifyCodeConfirmResult = {
  verified: boolean
  purpose: string
  valid_for_min: number
}

export const securityApi = {
  requestCode: (purpose: VerifyPurpose) =>
    api.post<VerifyCodeRequestResult>('/api/v1/security/verify-code/request', { purpose }),
  confirmCode: (purpose: VerifyPurpose, code: string) =>
    api.post<VerifyCodeConfirmResult>('/api/v1/security/verify-code/confirm', { purpose, code }),
}

// ─────────────────────────────────────────────────────────────────────────────
// Options paper trading
// ─────────────────────────────────────────────────────────────────────────────

export type StrikePreview = {
  underlying: string
  spot: number
  side: 'call' | 'put'
  strategy_name: string
  config: {
    delta_band: [number, number]
    dte_band: [number, number]
    prefer_itm: boolean
    options_mode: string | null
    spread_width: number | null
  }
  iv_assumption_used: number
  pick: {
    long: {
      ticker: string
      strike: number
      expiration: string
      right: 'call' | 'put'
      theoretical_premium: number
      delta: number
      gamma: number
      theta: number
      vega: number
      cost_per_contract_usd: number
    }
    short: null | {
      ticker: string
      strike: number
      expiration: string
      right: 'call' | 'put'
      theoretical_premium: number
      delta: number
    }
    days_to_expiration: number
    band_missed: boolean
    reason: string
  }
}

export type OptionsSession = {
  session_id: string
  strategy_id: string | null
  underlyings: string[]
  label: string
  started_at: string | null
  ended_at: string | null
  is_active: boolean
  total_trades: number
  net_pnl: number
}

export const optionsApi = {
  previewStrike: (strategyId: string, underlying = 'SPY', spot?: number, ivAssumption?: number) =>
    api.get<StrikePreview>(`/api/v1/options/preview-strike/${strategyId}`, {
      params: { underlying, ...(spot != null ? { spot } : {}), ...(ivAssumption != null ? { iv_assumption: ivAssumption } : {}) },
    }),
  startSession: (data: { strategy_id: string; underlyings: string[]; starting_balance?: number }) =>
    api.post('/api/v1/options/sessions', data),
  stopSession: (sessionId: string) =>
    api.post(`/api/v1/options/sessions/${sessionId}/stop`),
  listSessions: () =>
    api.get<{ sessions: OptionsSession[] }>('/api/v1/options/sessions'),
}

export type OptionsSessionTrade = {
  id: string
  instrument: string
  direction: 'call' | 'put' | string
  contracts: number
  entry_price: number | null
  exit_price: number | null
  stop_loss: number | null
  take_profit: number | null
  entry_time: string | null
  exit_time: string | null
  pnl: number | null
  commission: number | null
  net_pnl: number | null
  exit_reason: string | null
  status: string
  notes: Record<string, any>
}

export type OptionsSessionDetail = {
  session_id: string
  strategy_id: string | null
  mode: 'paper' | 'live' | string
  underlyings: string[]
  label: string
  is_active: boolean
  started_at: string | null
  ended_at: string | null
  total_trades: number
  net_pnl: number
  broker_account_id: string | null
  trades: OptionsSessionTrade[]
}

// Extend optionsApi with the new detail endpoint
;(optionsApi as any).sessionDetail = (sessionId: string) =>
  api.get<OptionsSessionDetail>(`/api/v1/options/sessions/${sessionId}`)

export type PendingTrade = {
  id: string
  strategy_id: string
  strategy_name: string
  instrument: string
  direction: 'long' | 'short' | string
  contracts: number
  entry_price: number | null
  stop_loss:   number | null
  take_profit: number | null
  bias:   string | null
  reason: string | null
  status: 'pending' | 'confirmed' | 'declined' | 'executed' | 'expired'
  is_intraday: boolean
  created_at: string | null
  expires_at: string | null
  confirmed_at: string | null
  executed_at:  string | null
}

;(optionsApi as any).getPendingByToken = (token: string) =>
  api.get<PendingTrade>(`/api/v1/options/pending/${token}`)
;(optionsApi as any).confirmPendingByToken = (token: string) =>
  api.post(`/api/v1/options/pending/${token}/confirm`)
;(optionsApi as any).declinePendingByToken = (token: string) =>
  api.post(`/api/v1/options/pending/${token}/decline`)
;(optionsApi as any).listPending = () =>
  api.get<{ pending_trades: PendingTrade[] }>('/api/v1/options/pending')


export const optionsPaperApi = {
  listSessions: () => api.get("/api/v1/options-paper/sessions"),
  startSession: (data: { strategy_id: string; underlying: string; daily_loss_limit?: number | null }) =>
    api.post("/api/v1/options-paper/sessions", data),
  stopSession: (sessionId: string) => api.post(`/api/v1/options-paper/sessions/${sessionId}/stop`),
}


// ─────────────────────────────────────────────────────────────────────────────
// Scanner — on-demand ticker analysis (structure levels + gate verdict)
// ─────────────────────────────────────────────────────────────────────────────

// One row of GET /api/v1/scanner/history — an email_signals_history pick.
// outcome is null until the resolver walks daily candles past the pick
// (win = target hit, loss = stop hit, expired = 5 trading days elapsed).
export type ScannerPick = {
  id: number | string
  picked_at: string
  ticker: string
  asset_type: 'options' | 'futures' | 'stocks' | string
  direction: string
  entry: number | null
  stop: number | null
  target: number | null
  gap_pct: number | null
  rel_vol: number | null
  today_vol: number | null
  score: number | null
  catalyst_reason: string | null
  outcome: 'win' | 'loss' | 'expired' | null
  outcome_pct: number | null
  resolved_at: string | null
}

export type ScannerHistory = {
  days: number
  asset_type: string
  count: number
  picks: ScannerPick[]
}

export const scannerApi = {
  analyze: (ticker: string, direction: string = 'long') =>
    api.get('/api/v1/scanner/analyze', { params: { ticker, direction } }),
  // Theta Scanner pick history (newest first, non-shadow only). Backend
  // clamps days to 1..90. Drives the V2 dashboard "Today's Pick" card.
  history: (days: number = 30, assetType: 'options' | 'futures' | 'stocks' | 'all' = 'all') =>
    api.get<ScannerHistory>('/api/v1/scanner/history', { params: { days, asset_type: assetType } }),
}

// ─────────────────────────────────────────────────────────────────────────────
// Replay — FX-Replay-style practice trading on historical 1m bars
// ─────────────────────────────────────────────────────────────────────────────

// GET /api/v1/replay/meta — available instruments + the date range with data.
export type ReplayMeta = {
  instruments: string[]
  min_date: string  // YYYY-MM-DD
  max_date: string  // YYYY-MM-DD
}

// GET /api/v1/replay/day — one trading day of 1m candles. 404 on holidays /
// days with no data. pdh/pdl (prior day high/low) are optional: the chart
// overlays them only when the backend supplies them.
export type ReplayDay = {
  instrument: string
  date: string  // YYYY-MM-DD
  candles: { time: number; open: number; high: number; low: number; close: number }[]
  pdh?: number | null
  pdl?: number | null
}

// The backend speaks per-instrument /meta {first_date,last_date,...} and day
// payloads of {bars:[{t,o,h,l,c,v}], levels:{pdh,pdl,...}} — adapt those wire
// shapes here so the Replay page only ever sees ReplayMeta/ReplayDay.
const adaptReplayDay = (d: any): ReplayDay => ({
  instrument: d?.instrument,
  date: d?.date,
  candles: (d?.bars ?? []).map((b: any) => ({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c })),
  pdh: d?.levels?.pdh ?? null,
  pdl: d?.levels?.pdl ?? null,
})

export const replayApi = {
  meta: async (instrument = 'NQ') => {
    const res = await api.get<any>('/api/v1/replay/meta', { params: { instrument } })
    const d = res.data ?? {}
    const data: ReplayMeta = { instruments: ['ES', 'NQ', 'YM', 'RTY'], min_date: d.first_date, max_date: d.last_date }
    return { ...res, data }
  },
  day: async (instrument: string, date: string) => {
    const res = await api.get<any>('/api/v1/replay/day', { params: { instrument, date, include_overnight: 0 } })
    return { ...res, data: adaptReplayDay(res.data) }
  },
  random: async (instrument: string) => {
    const res = await api.get<any>('/api/v1/replay/random', { params: { instrument, include_overnight: 0 } })
    return { ...res, data: adaptReplayDay(res.data) }
  },
}
