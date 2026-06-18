-- Email Signals stuck-pending fix: record WHY a signal resolved the way it did
-- (target_hit / stop_hit / no_hit_after_Nh / no_price_data / r_explosion).
-- Idempotent: safe to re-run. Applied via psql.
ALTER TABLE account_signals ADD COLUMN IF NOT EXISTS outcome_reason TEXT;
