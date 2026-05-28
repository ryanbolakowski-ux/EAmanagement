-- Bug 4 (idempotency + duplicate suppression) and Bug 5 (delivery tracking).
-- Idempotent: safe to re-run. No Alembic in this project, applied via psql.
ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS duplicate_suppressed_at TIMESTAMPTZ;
ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS duplicate_suppressed_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS detected_at TIMESTAMPTZ;
ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS queued_at TIMESTAMPTZ;
ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS provider_sent_at TIMESTAMPTZ;
ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;
ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS provider_message_id TEXT;
ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS provider_status VARCHAR(40);
ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS latency_seconds DOUBLE PRECISION;
ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS error_message TEXT;

-- Fast lookup of recent signals by idempotency key within the cooldown window.
CREATE INDEX IF NOT EXISTS ix_account_signals_idempotency
    ON account_signals (idempotency_key, fired_at);
