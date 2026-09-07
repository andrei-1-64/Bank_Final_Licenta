-- Admin role + the aggregate the admin dashboard needs.
--
-- HOW THE FIRST ADMIN IS MADE: manually, right here in the SQL Editor:
--
--     UPDATE users SET role = 'admin' WHERE email = 'you@example.com';
--
-- This is the ONLY way to create an admin from nothing, and it stays that
-- way: the admin endpoints (app/modules/admin) read every user's data by
-- design - they skip the per-user scoping that protects every other module -
-- so bootstrapping must not be reachable over HTTP.
--
-- Once an admin exists, they can promote others through
-- PATCH /admin/users/{id}/role (added later, in 0017's change set). That
-- endpoint deliberately refuses to change the CALLER's own role, so an admin
-- can never demote themselves and leave the system with zero admins -
-- recovering from that would need direct database access again.
--
-- Se ruleaza dupa 0001-0015, in Supabase SQL Editor.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'customer';

-- Separate from the ADD COLUMN so re-running this file is safe: ADD COLUMN
-- has IF NOT EXISTS, ADD CONSTRAINT does not.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'users_role_check'
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT users_role_check CHECK (role IN ('customer', 'admin'));
    END IF;
END $$;

-- --------------------------------------------------------------------------
-- Totals for the admin dashboard.
--
-- An RPC rather than N per-account get_account_balance calls from the app:
-- the dashboard wants one number per currency across EVERY account, and
-- doing that over PostgREST would be an N+1 that grows with the user base.
-- Same reasoning as 0002_ledger_functions.sql - aggregate work belongs in
-- the database.
--
-- Balance follows the same rule as get_account_balance: credits minus
-- debits, never a stored column.
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION admin_totals_by_currency()
RETURNS TABLE (
    currency VARCHAR(3),
    account_count BIGINT,
    total_minor BIGINT
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        a.currency,
        COUNT(DISTINCT a.id) AS account_count,
        COALESCE(SUM(
            CASE WHEN e.direction = 'credit' THEN e.amount_minor
                 WHEN e.direction = 'debit'  THEN -e.amount_minor
                 ELSE 0 END
        ), 0)::BIGINT AS total_minor
    FROM accounts a
    LEFT JOIN ledger_entries e ON e.account_id = a.id
    GROUP BY a.currency
    ORDER BY a.currency;
$$;
