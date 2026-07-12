-- iOS push device tokens (2026-07-12). REFERENCE ONLY — not auto-run.
-- The app provisions this table via Base.metadata.create_all from the ORM
-- model in app/models/device.py; this file exists so a manual/external DB
-- can be brought in line. Fully guarded: safe to run repeatedly.

CREATE TABLE IF NOT EXISTS device_tokens (
    id           uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id      uuid        NOT NULL,
    token        text        UNIQUE NOT NULL,
    platform     text        DEFAULT 'ios',
    created_at   timestamptz DEFAULT now(),
    last_seen_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_device_tokens_user_id ON device_tokens (user_id);
