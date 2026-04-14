// ─────────────────────────────────────────────────────────────────────────────
// Auth
// ─────────────────────────────────────────────────────────────────────────────

export interface User {
  id: string
  email: string
  username: string
  subscription_tier: SubscriptionTier
  is_active: boolean
  trial_ends_at: string | null
}

export type SubscriptionTier = 'free_trial' | 'tier_1' | 'tier_3' | 'tier_4' | 'tier_5'

export interface TokenResponse {
  access_token: string
  refresh_token: string
  token_type: string
  user_id: string
  email: string
  subscription_tier: SubscriptionTier
}

// ─────────────────────────────────────────────────────────────────────────────
// Strategies
// ─────────────────────────────────────────────────────────────────────────────

export interface Strategy {
  id: string
  name: string
  description: string | null
  status: 'draft' | 'active' | 'paused' | 'archived'
  instruments: string[]
  primary_timeframe: string
  execution_timeframe: string
  risk_reward_ratio: number
  stop_loss_type: 'ticks' | 'structure'
  session_filters: string[]
  created_at: string
}

export interface StrategyCreate {
  name: string
  description?: string
  instruments: string[]
  primary_timeframe: string
  execution_timeframe: string
  higher_timeframes: string[]
  risk_reward_ratio: number
  stop_loss_type: string
  stop_loss_ticks?: number
  max_contracts: number
  session_filters: string[]
  fvg_min_size_ticks: number
  fvg_max_size_ticks?: number
  max_daily_loss?: number
  max_trades_per_day?: number
}

// ─────────────────────────────────────────────────────────────────────────────
// Backtests
// ─────────────────────────────────────────────────────────────────────────────

export type BacktestStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface BacktestRun {
  id: string
  strategy_id: string
  instrument: string
  start_date: string
  end_date: string
  status: BacktestStatus
  created_at: string
  completed_at: string | null
}

export interface BacktestMetrics {
  total_trades: number
  win_rate: number
  net_profit: number
  profit_factor: number
  max_drawdown: number
  max_drawdown_pct: number
  sharpe_ratio: number | null
  avg_rr: number
  equity_curve: { timestamp: string; equity: number }[]
  monthly_returns: Record<string, number>
}

// ─────────────────────────────────────────────────────────────────────────────
// Trades
// ─────────────────────────────────────────────────────────────────────────────

export type TradingMode = 'paper' | 'live'
export type TradeStatus = 'pending' | 'open' | 'closed' | 'cancelled' | 'error'

export interface Trade {
  id: string
  strategy_id: string
  instrument: string
  direction: 'long' | 'short'
  mode: TradingMode
  status: TradeStatus
  entry_price: number | null
  exit_price: number | null
  stop_loss: number
  take_profit: number
  contracts: number
  pnl: number | null
  net_pnl: number | null
  entry_time: string | null
  exit_time: string | null
  exit_reason: string | null
}

// ─────────────────────────────────────────────────────────────────────────────
// Dashboard
// ─────────────────────────────────────────────────────────────────────────────

export interface DashboardSummary {
  strategy_count: number
  backtest_count: number
  subscription_tier: SubscriptionTier
  paper_trading: {
    total_trades: number
    net_pnl: number
    win_rate: number
  }
  live_trading: {
    total_trades: number
    net_pnl: number
    win_rate: number
  }
}
