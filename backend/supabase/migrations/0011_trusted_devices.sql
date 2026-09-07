-- Device-trust flow: after registering, a user is asked (optional) to
-- verify a Teams-delivered OTP once, which enrolls the current browser as
-- a trusted device. From then on, that browser can log in with just a
-- password (no email retyping, no OTP) - see app/modules/trusted_devices.
--
-- This is deliberately NOT WebAuthn/passkeys - no cryptographic device
-- key, just a long-lived signed cookie the backend recognizes. A website
-- cannot verify a user's real OS/Windows password; this only ever checks
-- the BanK account password again.
--
-- Se ruleaza dupa 0001-0010, in Supabase SQL Editor.

CREATE TABLE IF NOT EXISTS device_enrollment_codes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash VARCHAR(64) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    attempts SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_device_enrollment_codes_user_id ON device_enrollment_codes (user_id);

CREATE TABLE IF NOT EXISTS trusted_devices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_token_hash VARCHAR(64) NOT NULL UNIQUE,
    label TEXT
);
CREATE INDEX IF NOT EXISTS idx_trusted_devices_user_id ON trusted_devices (user_id);

ALTER TABLE device_enrollment_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE trusted_devices ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON device_enrollment_codes TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON trusted_devices TO service_role;
