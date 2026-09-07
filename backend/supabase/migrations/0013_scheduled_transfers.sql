-- Scheduled/recurring transfers between a user's own accounts. Same "no
-- cron, execute lazily on read" philosophy as savings/term-deposit interest
-- (see 0012_savings_and_term_deposits.sql's grant_interest) - due transfers
-- run the next time the owner's accounts are listed, not on a background
-- timer. next_run_at is when a transfer becomes due; frequency is NULL for
-- a one-time future-dated transfer, or 'weekly'/'monthly' to keep firing.
--
-- Se ruleaza dupa 0001-0012, in Supabase SQL Editor.

CREATE TABLE IF NOT EXISTS scheduled_transfers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    from_account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    to_account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    currency VARCHAR(3) NOT NULL,
    description VARCHAR(500),
    frequency VARCHAR(10) CHECK (frequency IN ('weekly', 'monthly')),
    next_run_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'cancelled', 'completed')),
    last_run_at TIMESTAMPTZ,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_scheduled_transfers_due
    ON scheduled_transfers (user_id, next_run_at)
    WHERE status = 'active';

ALTER TABLE scheduled_transfers ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON scheduled_transfers TO service_role;
