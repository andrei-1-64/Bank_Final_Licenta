-- Caches a merchant description -> spending category mapping, once it's
-- been classified by the few-shot LLM classifier (see
-- app/ai/tools/insights/categorize_transactions.py). GLOBAL, not per-user:
-- "netflix" means Divertisment no matter whose transaction it is, so one
-- classification benefits every user, and the fast keyword-matched majority
-- of transactions never touch this table at all - only the ones that fall
-- through to "Altele" do.
--
-- This is what lets the LLM step stay OUT of the synchronous dashboard
-- read path (GET /insights/spending-by-category never calls the model -
-- see that router's own docstring): the chat tool is the only thing that
-- ever WRITES here (it already pays LLM latency for the user's question
-- anyway), and the dashboard just reads whatever's already cached.
--
-- Se ruleaza dupa 0001-0018, in Supabase SQL Editor.

CREATE TABLE IF NOT EXISTS merchant_category_cache (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Lowercased, trimmed transaction description/reference text - same
    -- normalization the keyword matcher itself uses.
    description_key VARCHAR(255) NOT NULL UNIQUE,
    category VARCHAR(100) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_merchant_category_cache_key ON merchant_category_cache (description_key);
ALTER TABLE merchant_category_cache ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON merchant_category_cache TO service_role;
