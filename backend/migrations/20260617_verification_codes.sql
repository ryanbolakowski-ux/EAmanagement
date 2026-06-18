-- Phase C: email-code verification + security audit log.
-- Shared foundation for risk-increasing changes AND fully-automated activation.
-- Idempotent: safe to re-run. No Alembic in this project — applied via psql.

-- Short-lived, single-use, HASHED email verification codes. We never store the
-- plaintext code; only its sha256. A code is consumed on first successful match
-- and expires after a TTL; downstream sensitive actions check for a recent
-- CONSUMED code for the same purpose (require_recent_verification).
CREATE TABLE IF NOT EXISTS verification_codes (
    id            UUID PRIMARY KEY,
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose       VARCHAR(64) NOT NULL,        -- 'enable_automation' | 'risk_change' | ...
    code_hash     TEXT NOT NULL,               -- sha256(code) — never the plaintext
    context       JSONB,                       -- {setting, old, new, account_id, ...}
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    consumed_at   TIMESTAMPTZ,                 -- set on successful verification
    attempts      INTEGER NOT NULL DEFAULT 0,  -- failed attempts (lock after N)
    ip_address    TEXT,
    user_agent    TEXT
);
CREATE INDEX IF NOT EXISTS ix_verification_codes_user_purpose
    ON verification_codes (user_id, purpose, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_verification_codes_consumed
    ON verification_codes (user_id, purpose, consumed_at);

-- Append-only audit trail for every sensitive security/trading decision:
-- agreement acceptance, automation enable/disable, risk-increasing changes,
-- trade approvals/declines, and BLOCKED auto-trade attempts.
CREATE TABLE IF NOT EXISTS security_audit_log (
    id            UUID PRIMARY KEY,
    user_id       UUID REFERENCES users(id) ON DELETE SET NULL,
    event_type    VARCHAR(48) NOT NULL,        -- see EVENT_* in security.py
    detail        JSONB,                       -- {setting, old, new, signal_id, reason, ...}
    ip_address    TEXT,
    user_agent    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_security_audit_user      ON security_audit_log (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_security_audit_eventtype ON security_audit_log (event_type, created_at DESC);
