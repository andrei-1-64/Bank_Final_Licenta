-- Electronic signatures over uploaded documents (see 0014_documents.sql).
-- A signature is created by proposals_service._execute for
-- proposal_type = 'sign_document' - it goes through the SAME pending ->
-- step-up-confirm flow as a transfer or payment (see 0013_proposals.sql),
-- never a direct write. app/modules/esign holds the crypto + service code.
--
-- signing_keys: public keys only. The private key never touches the
-- database - it lives in the ESIGN_PRIVATE_KEY env var. Rows here are
-- NEVER deleted or overwritten: a signature made under key_id X must stay
-- verifiable for as long as the signature itself is kept, even after the
-- active key is rotated (revoked_at marks that, it doesn't remove the row).
--
-- Filed as 0020 rather than 0018: by the time this landed, 0018 and 0019
-- were already taken by the statements/merchant-category-cache migrations
-- from Dev2.1 (merged in separately) - same renumbering situation
-- 0018_statements.sql documents for its own original number.
-- Se ruleaza dupa 0001-0019, in Supabase SQL Editor.

CREATE TABLE IF NOT EXISTS signing_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    key_id VARCHAR NOT NULL UNIQUE,
    algorithm VARCHAR NOT NULL DEFAULT 'ed25519',
    public_key_b64 VARCHAR NOT NULL,
    revoked_at TIMESTAMPTZ
);
ALTER TABLE signing_keys ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON signing_keys TO service_role;

CREATE TABLE IF NOT EXISTS signatures (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One signature per proposal: the proposal's own id is already the
    -- idempotency key (see proposals_service._execute's docstring), so a
    -- proposal can be confirmed - and therefore signed - at most once.
    proposal_id UUID NOT NULL UNIQUE REFERENCES proposals(id) ON DELETE CASCADE,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_id VARCHAR NOT NULL REFERENCES signing_keys(key_id),
    algorithm VARCHAR NOT NULL,
    -- SHA-256 of documents.content, re-hashed at sign time (not copied from
    -- upload time) - see esign/service.py::sign_document.
    document_sha256 CHAR(64) NOT NULL,
    signed_at TIMESTAMPTZ NOT NULL,
    auth_method VARCHAR NOT NULL,
    intent TEXT NOT NULL,
    -- Exact bytes that were signed, so a verifier never has to guess how to
    -- reassemble them - see esign/canonical.py.
    canonical_payload TEXT NOT NULL,
    signature_b64 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signatures_user_id ON signatures (user_id);
CREATE INDEX IF NOT EXISTS idx_signatures_document_id ON signatures (document_id);
ALTER TABLE signatures ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT ON signatures TO service_role;
