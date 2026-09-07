-- Step-up auth: face confirmation required for large transfers/payments
-- (see app/modules/face_auth::enforce_face_confirmation, called from
-- transfers/service.py and payments/service.py). A token here is short-lived
-- and single-use, same "store a hash, not the secret" pattern as sessions -
-- see core/security.py's header comment.
--
-- Se ruleaza dupa 0001-0011, in Supabase SQL Editor.

CREATE TABLE IF NOT EXISTS face_confirmations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash VARCHAR(64) UNIQUE NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_face_confirmations_user_id ON face_confirmations (user_id);

ALTER TABLE face_confirmations ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON face_confirmations TO service_role;
