-- Password reset via OTP, delivered through a Microsoft Teams webhook (see
-- app/core/teams.py). A code is a random 6-digit string; only its SHA-256
-- hash is stored (same "never persist the raw secret" pattern as sessions -
-- see core/security.py's header comment). Hashing a 6-digit code isn't real
-- cryptographic protection (only 1e6 possibilities to brute-force offline),
-- it's just hygiene against a plain `SELECT * FROM password_reset_codes`
-- leaking usable codes; the actual defenses are expires_at and attempts.
--
-- Se ruleaza dupa 0001-0006, in Supabase SQL Editor.

CREATE TABLE IF NOT EXISTS password_reset_codes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash VARCHAR(64) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    attempts SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_password_reset_codes_user_id ON password_reset_codes (user_id);

ALTER TABLE password_reset_codes ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON password_reset_codes TO service_role;
