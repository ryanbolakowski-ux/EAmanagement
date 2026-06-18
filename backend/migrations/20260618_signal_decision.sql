-- Phase F: per-signal approve/decline decision (non-automated users review trade
-- ideas before any placement). Idempotent. Applied via psql.
ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS decision            VARCHAR(12);  -- 'approved' | 'declined' (NULL = undecided)
ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS decided_at          TIMESTAMPTZ;
ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS decided_by          UUID;         -- user who decided
ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS decided_via         VARCHAR(20);  -- 'app' | 'email'
ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS decision_ip         TEXT;
ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS decision_user_agent TEXT;
ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS placed_ref          TEXT;         -- result of place-on-approval
CREATE INDEX IF NOT EXISTS ix_account_signals_decision ON account_signals (user_id, decision, fired_at DESC);
