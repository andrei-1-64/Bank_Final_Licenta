-- The FX desk: how a cross-currency transfer moves money without breaking
-- double-entry.
--
-- THE PROBLEM. post_transaction (0002) enforces three invariants that a
-- cross-currency transfer violates all at once: every leg of a journal must
-- share one currency (BK004), each leg's currency must equal its account's
-- currency (BK004 again), and debits must equal credits (BK005). Debiting
-- 500 EUR and crediting 2.480 RON is not expressible as one journal, and it
-- should not be - "debits equal credits" is the invariant that guarantees a
-- journal cannot create money out of nothing, which is the whole point of
-- having a ledger.
--
-- THE SHAPE. What a real bank does: the customer does not trade with the
-- other account, they trade with the bank's FX desk, twice.
--
--     Journal 1 (EUR, balanced)   debit  user's EUR account    50000 EUR
--                                 credit desk's EUR account    50000 EUR
--     Journal 2 (RON, balanced)   debit  desk's RON account   248000 RON
--                                 credit user's RON account   248000 RON
--
-- Two ordinary single-currency journals, each balanced, each passing every
-- check post_transaction already makes. NOTHING in 0002 changes. The desk
-- ends up long EUR and short RON, which is exactly what an FX desk is for.
--
-- THE DESK'S ACCOUNTS are ordinary `accounts` rows owned by an ordinary
-- `users` row. No schema change and no is_internal flag is needed: every
-- query in the app is scoped by user_id, so the desk is invisible to
-- customers for free, and its accounts get no IBAN, so the payments flow
-- cannot reach them either.
--
-- Se ruleaza dupa 0001-0023, in Supabase SQL Editor.

-- ==========================================================================
-- 1. The desk's owner.
-- ==========================================================================
-- A fixed id so the RPC below and any future migration can find it without
-- a lookup by email.
--
-- password_hash is deliberately NOT a valid hash. verify_password() rejects
-- a malformed hash (see core/security.py and its test), so there is no
-- password that authenticates as this user - it exists to own accounts, not
-- to be logged into. email_verified stays FALSE for the same reason.
INSERT INTO users (id, email, password_hash, first_name, last_name, email_verified)
VALUES (
    'ffffffff-0000-0000-0000-000000000001',
    'fx-desk@bank.internal',
    'not-a-valid-hash-this-user-cannot-log-in',
    'BanK',
    'Trezorerie FX',
    FALSE
)
ON CONFLICT (id) DO NOTHING;

-- ==========================================================================
-- 2. One desk account per currency, funded.
-- ==========================================================================
-- Covers every currency customers already hold, plus the majors BNR
-- publishes that they might open an account in later. Re-running this
-- migration adds any currency that has appeared since; it never duplicates
-- one, and it never re-grants liquidity (grant_opening_balance is
-- idempotent on its key).
--
-- LIQUIDITY. Journal 2 above DEBITS the desk, and post_transaction refuses a
-- debit the account cannot cover (BK001). So the desk has to be funded, the
-- same way any customer account is funded - through grant_opening_balance,
-- which 0004 introduced as the single deliberate exception to "every journal
-- balances" and the only way money enters this system at all. 10 million
-- major units per currency is far past anything a demo will move; if a
-- currency ever ran dry, the customer's transfer would fail with the ordinary
-- insufficient-funds error rather than anything corrupt.
DO $$
DECLARE
    v_currency TEXT;
    v_account_id UUID;
BEGIN
    FOR v_currency IN
        SELECT DISTINCT UPPER(currency) AS currency
        FROM accounts
        WHERE user_id <> 'ffffffff-0000-0000-0000-000000000001'
        UNION
        SELECT unnest(ARRAY['RON', 'EUR', 'USD', 'GBP', 'CHF', 'HUF'])
    LOOP
        SELECT id INTO v_account_id
        FROM accounts
        WHERE user_id = 'ffffffff-0000-0000-0000-000000000001'
          AND UPPER(currency) = v_currency;

        IF NOT FOUND THEN
            INSERT INTO accounts (user_id, name, currency, status)
            VALUES (
                'ffffffff-0000-0000-0000-000000000001',
                'Trezorerie FX ' || v_currency,
                v_currency,
                'active'
            )
            RETURNING id INTO v_account_id;
        END IF;

        PERFORM grant_opening_balance(
            p_account_id := v_account_id,
            p_amount_minor := 1000000000,
            p_currency := v_currency,
            p_idempotency_key := 'fx-desk-opening:' || v_currency,
            p_actor_user_id := 'ffffffff-0000-0000-0000-000000000001'
        );
    END LOOP;
END $$;

-- ==========================================================================
-- 3. transfers: record what actually landed.
-- ==========================================================================
-- A transfers row carries ONE amount and ONE currency. For an FX transfer
-- that is the amount that LEFT the source account, which on its own makes
-- the history misleading - "500 EUR into a RON account" reads as though 500
-- RON arrived. These four nullable columns say what the other side received
-- and at what rate. NULL for an ordinary same-currency transfer, which is
-- every transfer written before this migration.
ALTER TABLE transfers
    ADD COLUMN IF NOT EXISTS converted_amount_minor BIGINT,
    ADD COLUMN IF NOT EXISTS converted_currency VARCHAR(3),
    ADD COLUMN IF NOT EXISTS exchange_rate NUMERIC,
    ADD COLUMN IF NOT EXISTS exchange_rate_date DATE;

-- ==========================================================================
-- 4. create_fx_transfer - the two journals, atomically.
-- ==========================================================================
-- Deliberately a sibling of create_transfer rather than a widening of it:
-- an ordinary transfer keeps its exact existing code path, and a reader of
-- create_transfer does not have to hold the FX case in their head. Both
-- funnel into post_transaction, which stays the only thing that writes
-- ledger_entries.
--
-- The RATE IS AN ARGUMENT, not something this function looks up. It is
-- recorded, not recomputed: the caller locked it when the user was shown the
-- proposal, and the number the user confirmed is the number that executes.
CREATE OR REPLACE FUNCTION create_fx_transfer(
    p_from_account_id UUID,
    p_to_account_id UUID,
    p_from_amount_minor BIGINT,
    p_from_currency TEXT,
    p_to_amount_minor BIGINT,
    p_to_currency TEXT,
    p_exchange_rate NUMERIC,
    p_exchange_rate_date DATE,
    p_description TEXT,
    p_idempotency_key TEXT,
    p_actor_user_id UUID DEFAULT NULL
)
RETURNS transfers
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    v_existing_transfer transfers;
    v_from_account accounts;
    v_to_account accounts;
    v_desk_from_id UUID;
    v_desk_to_id UUID;
    v_journal_out journal_transactions;
    v_journal_in journal_transactions;
    v_transfer transfers;
BEGIN
    -- Same cheap idempotency short-circuit create_transfer opens with.
    SELECT * INTO v_existing_transfer FROM transfers WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        RETURN v_existing_transfer;
    END IF;

    IF p_from_account_id = p_to_account_id THEN
        RAISE EXCEPTION 'from and to must differ.' USING ERRCODE = 'BK005';
    END IF;
    IF p_from_amount_minor <= 0 OR p_to_amount_minor <= 0 THEN
        RAISE EXCEPTION 'amounts must be positive.' USING ERRCODE = 'BK005';
    END IF;
    IF p_exchange_rate IS NULL OR p_exchange_rate <= 0 THEN
        RAISE EXCEPTION 'exchange rate must be positive.' USING ERRCODE = 'BK005';
    END IF;
    IF UPPER(p_from_currency) = UPPER(p_to_currency) THEN
        -- Not an FX transfer at all. Refused rather than quietly handled, so
        -- the ordinary path cannot drift into this one unnoticed.
        RAISE EXCEPTION 'Same-currency transfers must use create_transfer.' USING ERRCODE = 'BK005';
    END IF;

    SELECT * INTO v_from_account FROM accounts WHERE id = p_from_account_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Account % not found.', p_from_account_id USING ERRCODE = 'BK002';
    END IF;
    SELECT * INTO v_to_account FROM accounts WHERE id = p_to_account_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Account % not found.', p_to_account_id USING ERRCODE = 'BK002';
    END IF;

    -- Each side's stated currency must be its account's own. post_transaction
    -- would catch this too; catching it here names which side is wrong.
    IF UPPER(v_from_account.currency) <> UPPER(p_from_currency) THEN
        RAISE EXCEPTION 'Source account % is %, transfer says %.',
            p_from_account_id, v_from_account.currency, p_from_currency USING ERRCODE = 'BK004';
    END IF;
    IF UPPER(v_to_account.currency) <> UPPER(p_to_currency) THEN
        RAISE EXCEPTION 'Target account % is %, transfer says %.',
            p_to_account_id, v_to_account.currency, p_to_currency USING ERRCODE = 'BK004';
    END IF;

    SELECT id INTO v_desk_from_id FROM accounts
    WHERE user_id = 'ffffffff-0000-0000-0000-000000000001'
      AND UPPER(currency) = UPPER(p_from_currency)
      AND status = 'active';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No FX desk account for %.', p_from_currency USING ERRCODE = 'BK002';
    END IF;

    SELECT id INTO v_desk_to_id FROM accounts
    WHERE user_id = 'ffffffff-0000-0000-0000-000000000001'
      AND UPPER(currency) = UPPER(p_to_currency)
      AND status = 'active';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No FX desk account for %.', p_to_currency USING ERRCODE = 'BK002';
    END IF;

    -- Journal 1: the source currency leaves the customer and reaches the desk.
    -- Suffixed keys so each journal keeps its own idempotency, derived from
    -- the caller's one key - a replay of this function replays both.
    v_journal_out := post_transaction(
        p_idempotency_key := p_idempotency_key || ':fx-out',
        p_description := p_description,
        p_legs := jsonb_build_array(
            jsonb_build_object('account_id', p_from_account_id, 'direction', 'debit',
                               'amount_minor', p_from_amount_minor, 'currency', UPPER(p_from_currency)),
            jsonb_build_object('account_id', v_desk_from_id, 'direction', 'credit',
                               'amount_minor', p_from_amount_minor, 'currency', UPPER(p_from_currency))
        ),
        p_actor_user_id := p_actor_user_id
    );

    -- Journal 2: the target currency leaves the desk and reaches the customer.
    -- If this raises (desk out of liquidity, target account closed), the whole
    -- function is one statement from the caller's point of view, so journal 1
    -- rolls back with it - the customer is never debited without being
    -- credited.
    v_journal_in := post_transaction(
        p_idempotency_key := p_idempotency_key || ':fx-in',
        p_description := p_description,
        p_legs := jsonb_build_array(
            jsonb_build_object('account_id', v_desk_to_id, 'direction', 'debit',
                               'amount_minor', p_to_amount_minor, 'currency', UPPER(p_to_currency)),
            jsonb_build_object('account_id', p_to_account_id, 'direction', 'credit',
                               'amount_minor', p_to_amount_minor, 'currency', UPPER(p_to_currency))
        ),
        p_actor_user_id := p_actor_user_id
    );

    -- The transfers row points at the OUT journal: it is the one whose
    -- from_account_id/amount/currency the row already describes.
    SELECT * INTO v_existing_transfer FROM transfers WHERE journal_id = v_journal_out.id;
    IF FOUND THEN
        RETURN v_existing_transfer;
    END IF;

    BEGIN
        INSERT INTO transfers (
            journal_id, from_account_id, to_account_id, amount_minor, currency,
            idempotency_key, converted_amount_minor, converted_currency,
            exchange_rate, exchange_rate_date
        )
        VALUES (
            v_journal_out.id, p_from_account_id, p_to_account_id,
            p_from_amount_minor, UPPER(p_from_currency), p_idempotency_key,
            p_to_amount_minor, UPPER(p_to_currency), p_exchange_rate, p_exchange_rate_date
        )
        RETURNING * INTO v_transfer;
    EXCEPTION WHEN unique_violation THEN
        SELECT * INTO v_existing_transfer FROM transfers WHERE idempotency_key = p_idempotency_key;
        IF NOT FOUND THEN
            RAISE;
        END IF;
        RETURN v_existing_transfer;
    END;

    RETURN v_transfer;
END;
$$;

GRANT EXECUTE ON FUNCTION create_fx_transfer(
    UUID, UUID, BIGINT, TEXT, BIGINT, TEXT, NUMERIC, DATE, TEXT, TEXT, UUID
) TO service_role;
