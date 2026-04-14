-- Edge Asset Management — initial database schema
-- This is run once by Docker on first container start.
-- SQLAlchemy/Alembic handles subsequent migrations.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Enums
CREATE TYPE subscription_tier AS ENUM ('free_trial', 'tier_1', 'tier_3', 'tier_4', 'tier_5');
CREATE TYPE strategy_status   AS ENUM ('draft', 'active', 'paused', 'archived');
CREATE TYPE backtest_status   AS ENUM ('queued', 'running', 'completed', 'failed', 'cancelled');
CREATE TYPE trading_mode      AS ENUM ('paper', 'live');
CREATE TYPE trade_status      AS ENUM ('pending', 'open', 'closed', 'cancelled', 'error');
CREATE TYPE trade_direction   AS ENUM ('long', 'short');
CREATE TYPE optimization_status AS ENUM ('queued', 'running', 'completed', 'failed');

-- Users
CREATE TABLE IF NOT EXISTS users (
    id                    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email                 VARCHAR(255) UNIQUE NOT NULL,
    username              VARCHAR(100) UNIQUE NOT NULL,
    hashed_password       VARCHAR(255) NOT NULL,
    is_active             BOOLEAN DEFAULT TRUE,
    is_verified           BOOLEAN DEFAULT FALSE,
    subscription_tier     subscription_tier DEFAULT 'free_trial',
    trial_started_at      TIMESTAMPTZ,
    trial_ends_at         TIMESTAMPTZ,
    subscription_started_at TIMESTAMPTZ,
    subscription_ends_at  TIMESTAMPTZ,
    stripe_customer_id    VARCHAR(255),
    created_at            TIMESTAMPTZ DEFAULT NOW(),
    updated_at            TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_users_email ON users(email);

-- Broker accounts
CREATE TABLE IF NOT EXISTS broker_accounts (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id                  UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    broker                   VARCHAR(50) NOT NULL DEFAULT 'tradovate',
    account_name             VARCHAR(100) NOT NULL,
    encrypted_credentials    TEXT NOT NULL,
    is_demo                  BOOLEAN DEFAULT TRUE,
    is_active                BOOLEAN DEFAULT TRUE,
    account_id_at_broker     VARCHAR(100),
    created_at               TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_broker_accounts_user ON broker_accounts(user_id);

-- Strategies
CREATE TABLE IF NOT EXISTS strategies (
    id                    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id               UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name                  VARCHAR(200) NOT NULL,
    description           TEXT,
    status                strategy_status DEFAULT 'draft',
    instruments           JSONB DEFAULT '[]',
    primary_timeframe     VARCHAR(10) DEFAULT '15m',
    execution_timeframe   VARCHAR(10) DEFAULT '1m',
    higher_timeframes     JSONB DEFAULT '[]',
    risk_reward_ratio     FLOAT DEFAULT 2.0,
    stop_loss_type        VARCHAR(20) DEFAULT 'structure',
    stop_loss_ticks       INTEGER,
    max_contracts         INTEGER DEFAULT 1,
    session_filters       JSONB DEFAULT '[]',
    fvg_min_size_ticks    INTEGER DEFAULT 4,
    fvg_max_size_ticks    INTEGER,
    rule_tree             JSONB DEFAULT '{}',
    max_daily_loss        FLOAT,
    max_trades_per_day    INTEGER,
    kill_switch_enabled   BOOLEAN DEFAULT TRUE,
    created_at            TIMESTAMPTZ DEFAULT NOW(),
    updated_at            TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_strategies_user ON strategies(user_id);

-- Backtest runs
CREATE TABLE IF NOT EXISTS backtest_runs (
    id                        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    strategy_id               UUID NOT NULL REFERENCES strategies(id),
    user_id                   UUID NOT NULL REFERENCES users(id),
    instrument                VARCHAR(20) NOT NULL,
    start_date                TIMESTAMPTZ NOT NULL,
    end_date                  TIMESTAMPTZ NOT NULL,
    timeframe                 VARCHAR(10) NOT NULL,
    initial_capital           FLOAT DEFAULT 100000,
    commission_per_side       FLOAT DEFAULT 2.25,
    slippage_ticks            INTEGER DEFAULT 1,
    strategy_params_snapshot  JSONB DEFAULT '{}',
    status                    backtest_status DEFAULT 'queued',
    celery_task_id            VARCHAR(255),
    error_message             TEXT,
    started_at                TIMESTAMPTZ,
    completed_at              TIMESTAMPTZ,
    created_at                TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_backtest_runs_user ON backtest_runs(user_id);
CREATE INDEX idx_backtest_runs_strategy ON backtest_runs(strategy_id);

-- Backtest metrics
CREATE TABLE IF NOT EXISTS backtest_metrics (
    id                        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    backtest_run_id           UUID UNIQUE NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    total_trades              INTEGER DEFAULT 0,
    winning_trades            INTEGER DEFAULT 0,
    losing_trades             INTEGER DEFAULT 0,
    win_rate                  FLOAT DEFAULT 0,
    net_profit                FLOAT DEFAULT 0,
    gross_profit              FLOAT DEFAULT 0,
    gross_loss                FLOAT DEFAULT 0,
    profit_factor             FLOAT DEFAULT 0,
    max_drawdown              FLOAT DEFAULT 0,
    max_drawdown_pct          FLOAT DEFAULT 0,
    sharpe_ratio              FLOAT,
    sortino_ratio             FLOAT,
    avg_win                   FLOAT DEFAULT 0,
    avg_loss                  FLOAT DEFAULT 0,
    avg_rr                    FLOAT DEFAULT 0,
    largest_win               FLOAT DEFAULT 0,
    largest_loss              FLOAT DEFAULT 0,
    avg_trade_duration_minutes FLOAT DEFAULT 0,
    equity_curve              JSONB DEFAULT '[]',
    monthly_returns           JSONB DEFAULT '{}'
);

-- Backtest trades
CREATE TABLE IF NOT EXISTS backtest_trades (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    backtest_run_id   UUID NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    instrument        VARCHAR(20) NOT NULL,
    direction         trade_direction NOT NULL,
    entry_price       FLOAT NOT NULL,
    exit_price        FLOAT NOT NULL,
    contracts         INTEGER DEFAULT 1,
    stop_loss         FLOAT NOT NULL,
    take_profit       FLOAT NOT NULL,
    entry_time        TIMESTAMPTZ NOT NULL,
    exit_time         TIMESTAMPTZ NOT NULL,
    pnl               FLOAT NOT NULL,
    pnl_ticks         FLOAT NOT NULL,
    commission        FLOAT DEFAULT 0,
    slippage          FLOAT DEFAULT 0,
    net_pnl           FLOAT NOT NULL,
    is_winner         BOOLEAN NOT NULL,
    exit_reason       VARCHAR(50) DEFAULT 'tp_hit',
    conditions_snapshot JSONB DEFAULT '{}'
);
CREATE INDEX idx_backtest_trades_run ON backtest_trades(backtest_run_id);

-- Optimization runs
CREATE TABLE IF NOT EXISTS optimization_runs (
    id                    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    strategy_id           UUID NOT NULL REFERENCES strategies(id),
    user_id               UUID NOT NULL REFERENCES users(id),
    instrument            VARCHAR(20) NOT NULL,
    start_date            TIMESTAMPTZ NOT NULL,
    end_date              TIMESTAMPTZ NOT NULL,
    parameter_grid        JSONB NOT NULL,
    optimization_metric   VARCHAR(50) DEFAULT 'profit_factor',
    total_combinations    INTEGER DEFAULT 0,
    completed_combinations INTEGER DEFAULT 0,
    status                optimization_status DEFAULT 'queued',
    celery_task_id        VARCHAR(255),
    error_message         TEXT,
    started_at            TIMESTAMPTZ,
    completed_at          TIMESTAMPTZ,
    created_at            TIMESTAMPTZ DEFAULT NOW()
);

-- Optimization results
CREATE TABLE IF NOT EXISTS optimization_results (
    id                    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    optimization_run_id   UUID NOT NULL REFERENCES optimization_runs(id) ON DELETE CASCADE,
    parameters            JSONB NOT NULL,
    rank                  INTEGER NOT NULL,
    net_profit            FLOAT DEFAULT 0,
    profit_factor         FLOAT DEFAULT 0,
    win_rate              FLOAT DEFAULT 0,
    max_drawdown          FLOAT DEFAULT 0,
    total_trades          INTEGER DEFAULT 0,
    sharpe_ratio          FLOAT,
    backtest_run_id       UUID REFERENCES backtest_runs(id)
);

-- Trade sessions
CREATE TABLE IF NOT EXISTS trade_sessions (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    strategy_id       UUID NOT NULL REFERENCES strategies(id),
    user_id           UUID NOT NULL REFERENCES users(id),
    broker_account_id UUID REFERENCES broker_accounts(id),
    mode              trading_mode NOT NULL,
    is_active         BOOLEAN DEFAULT TRUE,
    started_at        TIMESTAMPTZ DEFAULT NOW(),
    ended_at          TIMESTAMPTZ,
    daily_loss_limit  FLOAT,
    max_trades_today  INTEGER,
    kill_switch_triggered BOOLEAN DEFAULT FALSE,
    total_trades      INTEGER DEFAULT 0,
    net_pnl           FLOAT DEFAULT 0
);

-- Trades
CREATE TABLE IF NOT EXISTS trades (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    strategy_id         UUID NOT NULL REFERENCES strategies(id),
    user_id             UUID NOT NULL REFERENCES users(id),
    broker_account_id   UUID REFERENCES broker_accounts(id),
    session_id          UUID REFERENCES trade_sessions(id),
    mode                trading_mode NOT NULL,
    status              trade_status DEFAULT 'pending',
    instrument          VARCHAR(20) NOT NULL,
    direction           trade_direction NOT NULL,
    contracts           INTEGER DEFAULT 1,
    entry_price         FLOAT,
    exit_price          FLOAT,
    stop_loss           FLOAT NOT NULL,
    take_profit         FLOAT NOT NULL,
    entry_time          TIMESTAMPTZ,
    exit_time           TIMESTAMPTZ,
    broker_order_id     VARCHAR(100),
    broker_sl_order_id  VARCHAR(100),
    broker_tp_order_id  VARCHAR(100),
    pnl                 FLOAT,
    commission          FLOAT DEFAULT 0,
    net_pnl             FLOAT,
    exit_reason         VARCHAR(50),
    notes               JSONB DEFAULT '{}',
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_trades_user ON trades(user_id);
CREATE INDEX idx_trades_strategy ON trades(strategy_id);
CREATE INDEX idx_trades_mode ON trades(mode);
