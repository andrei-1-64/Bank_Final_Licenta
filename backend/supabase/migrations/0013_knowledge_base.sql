-- Knowledge base for the RAG-backed docs agent (app/ai/agents/docs_agent.py).
-- Source documents live in backend/knowledge_base/ and are ingested offline
-- via `python -m scripts.ingest_knowledge_base` (PDF -> Azure Document
-- Intelligence prebuilt-layout -> chunks -> Azure OpenAI embeddings) - never
-- written to from the request path.
--
-- Not user-owned data (it's shared product documentation, same for every
-- user), so unlike the banking tables there is no per-owner RLS policy to
-- write - access is service_role only, same as the rest of this app.
--
-- Se ruleaza dupa 0001-0012, in Supabase SQL Editor.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS knowledge_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Path relative to backend/knowledge_base/, e.g.
    -- "ghid-produse-si-comisioane.html". The stable identity of a document
    -- across re-ingestion runs.
    source_path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    -- SHA-256 of the extracted text. Re-ingestion compares this before
    -- re-embedding, so an unchanged document is a no-op, not a full re-embed.
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    document_id UUID NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    -- The heading/table this chunk came from, if any - shown to the user as
    -- a citation, never used for access control.
    section_title TEXT,
    content TEXT NOT NULL,
    -- text-embedding-3-small's dimension. If the embedding deployment ever
    -- changes to a different model family, this column (and the index below)
    -- must be rebuilt to match - it is not model-agnostic.
    embedding VECTOR(1536) NOT NULL,
    UNIQUE (document_id, chunk_index)
);

-- HNSW over IVFFlat: no need to size a `lists` parameter against row count,
-- and this table is small (a handful of documents), so build/query cost
-- differences between the two don't matter here - HNSW is just the better
-- default absent a reason to tune otherwise.
CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_idx
    ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);

ALTER TABLE knowledge_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_chunks ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge_documents TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge_chunks TO service_role;

-- Cosine similarity search, called from app/ai/knowledge/service.py::search
-- via supabase-py's .rpc(). `match_count` mirrors the banking tools'
-- `limit` pattern (bounded in the caller, not unbounded here).
CREATE OR REPLACE FUNCTION match_knowledge_chunks(
    query_embedding VECTOR(1536),
    match_count INT DEFAULT 5
)
RETURNS TABLE (
    chunk_id UUID,
    document_id UUID,
    document_title TEXT,
    section_title TEXT,
    content TEXT,
    similarity FLOAT
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        c.id AS chunk_id,
        c.document_id,
        d.title AS document_title,
        c.section_title,
        c.content,
        1 - (c.embedding <=> query_embedding) AS similarity
    FROM knowledge_chunks c
    JOIN knowledge_documents d ON d.id = c.document_id
    ORDER BY c.embedding <=> query_embedding
    LIMIT match_count;
$$;
