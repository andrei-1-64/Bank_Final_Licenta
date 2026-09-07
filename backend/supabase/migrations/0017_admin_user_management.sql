-- Blocking a user, and recording which card a transaction was made with.
--
-- Se ruleaza dupa 0016, in Supabase SQL Editor.
--
-- IMPORTANT: apply this BEFORE deploying the matching backend. Unlike 0016
-- (whose `role` column is only read by the admin endpoints), `blocked_at` is
-- read by core/dependencies.py::get_current_user on EVERY authenticated
-- request - that is the whole point, a blocked user must not get past the
-- session check. Until this migration runs, that select fails and no one can
-- authenticate.

-- --------------------------------------------------------------------------
-- Blocking
--
-- A nullable timestamp rather than a boolean: "blocked" and "blocked since
-- when" are the same question for support, and NULL/NOT NULL is exactly as
-- cheap to filter on as TRUE/FALSE.
-- --------------------------------------------------------------------------
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS blocked_at TIMESTAMPTZ;

-- --------------------------------------------------------------------------
-- Which card a transaction was made with.
--
-- Nullable, and NULL is a legitimate, permanent state - not "missing data":
-- transfers between own accounts, incoming payments and the dev seeding
-- scripts involve no card at all. Every row that exists before this
-- migration is therefore NULL too, and the admin UI shows those as "fara
-- card" rather than pretending otherwise.
--
-- ON DELETE SET NULL, never CASCADE: cards get cancelled, and the ledger is
-- append-only and immutable (see 0001's header) - deleting a card must never
-- take financial history with it.
-- --------------------------------------------------------------------------
ALTER TABLE ledger_entries
    ADD COLUMN IF NOT EXISTS card_id UUID REFERENCES cards(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_ledger_entries_card_id
    ON ledger_entries (card_id);
