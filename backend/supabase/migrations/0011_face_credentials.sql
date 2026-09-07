-- Face login (DIY, not WebAuthn - see app/modules/face_auth). One face
-- encoding per user: a 128-value vector from face_recognition/dlib, stored
-- as JSON, never the raw photo itself. This is demo-grade biometric auth,
-- not a real security boundary - no liveness detection, so a printed photo
-- can pass it. Good enough for a hackathon-style feature, not for a real
-- bank; see the module docstring for the full caveat.
--
-- Se ruleaza dupa 0001-0010, in Supabase SQL Editor.

CREATE TABLE IF NOT EXISTS face_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    embedding JSONB NOT NULL
);

ALTER TABLE face_credentials ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON face_credentials TO service_role;
