-- Per-user uploaded documents (PDFs for now).
-- Content stored as bytea directly in Postgres - 5MB hard cap enforced
-- at the API layer. extracted_text is populated at upload time by
-- pymupdf so the DocumentAgent doesn't re-extract on every question.
-- The AI treats extracted_text as UNTRUSTED input - see
-- app/ai/agents/document_agent.py for the isolation model.
-- Se ruleaza dupa 0013 migrations, in Supabase SQL Editor.

CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
    filename VARCHAR(255) NOT NULL,
    mime_type VARCHAR(100) NOT NULL,
    size_bytes INTEGER NOT NULL,
    content BYTEA NOT NULL,
    extracted_text TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_documents_user_id ON documents (user_id);
CREATE INDEX IF NOT EXISTS idx_documents_conversation_id ON documents (conversation_id);
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON documents TO service_role;
