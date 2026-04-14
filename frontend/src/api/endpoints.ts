import api from './client'
import type {
  TokenResponse, User, Strategy, StrategyCreate,
  BacktestRun, BacktestMetrics, Trade, DashboardSummary,
} from '../types'

// ─────────────────────────────────────────────────────────────────────────────
// Auth
// ─────────────────────────────────────────────────────────────────────────────

export const authApi = {
  login: (email: string, password: string) =>
    api.post<TokenResponse>('/api/v1/auth/login', new URLSearchParams({ username: email, password }), {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    }),

  register: (email: string, username: string, password: string) =>
    api.post<TokenResponse>('/api/v1/auth/register', { email, username, password }),

  me: () => api.get<User>('/api/v1/auth/me'),
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
    timeframe: string
    initial_capital: number
    slippage_ticks: number
    commission_per_side: number
  }) => api.post<BacktestRun>('/api/v1/backtests/', data),
  getMetrics: (id: string) => api.get<BacktestMetrics>(`/api/v1/backtests/${id}/metrics`),
}

// ─────────────────────────────────────────────────────────────────────────────
// Optimization
// ─────────────────────────────────────────────────────────────────────────────

export const optimizationApi = {
  start: (data: {
    strategy_id: string
    instrument: string
    start_date: string
    end_date: string
    parameter_grid: Record<string, number[] | string[]>
    optimization_metric: string
  }) => api.post('/api/v1/optimization/', data),
  getResults: (runId: string) => api.get(`/api/v1/optimization/${runId}/results`),
}

// ─────────────────────────────────────────────────────────────────────────────
// Paper & Live Trading
// ─────────────────────────────────────────────────────────────────────────────

export const paperTradingApi = {
  listSessions: () => api.get('/api/v1/paper-trading/sessions'),
  startSession: (data: { strategy_id: string; instrument: string; daily_loss_limit?: number }) =>
    api.post('/api/v1/paper-trading/sessions', data),
  stopSession: (id: string) => api.post(`/api/v1/paper-trading/sessions/${id}/stop`),
}

export const liveTradingApi = {
  listAccounts: () => api.get('/api/v1/live-trading/accounts'),
  addAccount: (data: { account_name: string; broker: string; is_demo: boolean; credentials: object }) =>
    api.post('/api/v1/live-trading/accounts', data),
  startSession: (data: { strategy_id: string; broker_account_id: string; instrument: string }) =>
    api.post('/api/v1/live-trading/sessions', data),
  killSwitch: (sessionId: string) => api.post(`/api/v1/live-trading/sessions/${sessionId}/kill-switch`),
}

// ─────────────────────────────────────────────────────────────────────────────
// Trades & Dashboard
// ─────────────────────────────────────────────────────────────────────────────

export const tradesApi = {
  list: (params?: { mode?: string; strategy_id?: string; limit?: number }) =>
    api.get<Trade[]>('/api/v1/trades/', { params }),
}

export const dashboardApi = {
  summary: () => api.get<DashboardSummary>('/api/v1/dashboard/summary'),
}
