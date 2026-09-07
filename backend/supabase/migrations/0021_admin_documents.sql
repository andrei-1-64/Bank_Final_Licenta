-- Admin-generated documents sent to a specific user for signing, plus the
-- OTP codes used to strongly authenticate that signature (see
-- app/modules/admin/service.py::generate_and_send_document and
-- app/modules/esign/service.py's request_signing_code/confirm_admin_document).
--
-- Se ruleaza dupa 0001-0020, in Supabase SQL Editor.

-- NULL means "the user uploaded this themselves" (the existing flow from
-- 0014_documents.sql, unchanged). Non-NULL marks a document an admin
-- generated and sent, and who - this is also the flag that routes signing
-- through the stronger OTP+Face confirm path instead of the ordinary
-- Face-or-password one (see esign/service.py).
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS issued_by_admin_id UUID REFERENCES users(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_documents_issued_by_admin_id
    ON documents (issued_by_admin_id) WHERE issued_by_admin_id IS NOT NULL;

-- One-time codes for the OTP half of an admin-document signature. Same
-- "store a hash, not the code" shape as password_reset_codes
-- (0007_password_reset_codes.sql) and face_confirmations
-- (0012_face_confirmations.sql) - deliberately its own table rather than
-- reusing password_reset_codes, so a failed signing attempt's attempt
-- counter can never affect a user's ability to reset their password (or
-- vice versa).
--
-- Scoped by proposal_id, not just user_id: a user can have more than one
-- admin-sent document pending signature at once, each a separate
-- `sign_document` proposal (see 0013_proposals.sql) - the code authorizes
-- ONE specific signature, not "this user's next signature of any kind".
CREATE TABLE IF NOT EXISTS document_signing_codes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    proposal_id UUID NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
    code_hash VARCHAR(64) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    attempts SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_document_signing_codes_proposal_id
    ON document_signing_codes (proposal_id);
ALTER TABLE document_signing_codes ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON document_signing_codes TO service_role;
