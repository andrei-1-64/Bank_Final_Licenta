-- Bank statement ingestion (Step 13): extracted, unverified rows from an
-- uploaded statement PDF. Kept in its own tables, completely separate from
-- the ledger (accounts / journal_transactions / ledger_entries) - a
-- statement row is NEVER written into ledger_entries under any
-- circumstance. See app/modules/statements/service.py for the read/write
-- surface and app/ai/tools/insights/_shared.py's load_rows for how the AI
-- layer reads these rows alongside real ledger data without conflating
-- the two.
--
-- amount is SIGNED: positive = inflow/credit, negative = outflow/debit
-- (see app/modules/documents/statement_extractor.py's module docstring).
--
-- NOTE: this file was written from a schema the numbers 0015-0017 in this
-- repo were already using for other migrations by the time this step
-- landed, so it is filed as 0018 rather than 0015. If a 0015-numbered
-- version of this schema was already applied by hand via the Supabase SQL
-- Editor, diff it against this file before running this one - do not run
-- both.
-- Se ruleaza dupa 0017 migrations, in Supabase SQL Editor.

CREATE TABLE IF NOT EXISTS statements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
    document_id UUID REFERENCES documents(id) ON DELETE SET NULL,
    bank_name VARCHAR(255),
    period_start DATE,
    period_end DATE,
    currency VARCHAR(3) NOT NULL DEFAULT 'RON',
    opening_balance NUMERIC(18, 2),
    closing_balance NUMERIC(18, 2),
    row_count INTEGER NOT NULL DEFAULT 0,
    deleted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_statements_user ON statements (user_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_statements_conversation_id ON statements (conversation_id);
ALTER TABLE statements ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON statements TO service_role;

CREATE TABLE IF NOT EXISTS statement_rows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    statement_id UUID NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    posted_date DATE,
    description TEXT NOT NULL DEFAULT '',
    amount NUMERIC(18, 2) NOT NULL,
    currency VARCHAR(3) NOT NULL DEFAULT 'RON',
    balance_after NUMERIC(18, 2),
    row_index INTEGER NOT NULL,
    extracted_category VARCHAR(50)
);
CREATE INDEX IF NOT EXISTS idx_statement_rows_statement ON statement_rows (statement_id);
ALTER TABLE statement_rows ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON statement_rows TO service_role;
