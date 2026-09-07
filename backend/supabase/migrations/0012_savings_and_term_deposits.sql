-- Real savings & fixed-term deposit accounts. Extends `accounts` in place
-- rather than a new table - same row shape, different product_type, so
-- existing balance/transfer/payment reads need no changes, just a lock
-- check on outgoing movement for term deposits (see
-- accounts/service.py::assert_not_locked_for_debit) and the grant_interest
-- RPC below for crediting interest.
--
-- product_type: 'checking' (default, unchanged behaviour) | 'savings'
-- (flexible, interest_rate_bps only) | 'term_deposit' (locked until
-- maturity_date, interest_rate_bps + term_months + maturity_date all set).
--
-- Se ruleaza dupa 0001-0011, in Supabase SQL Editor.

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS product_type VARCHAR(20) NOT NULL DEFAULT 'checking';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS interest_rate_bps INT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS term_months INT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS maturity_date DATE;

-- grant_interest - same locking/idempotency/replay pattern as
-- grant_opening_balance (see 0004), just a different reference/description
-- and audit action so the ledger/audit trail can tell interest credits
-- apart from the opening-balance grant.
CREATE OR REPLACE FUNCTION grant_interest(
    p_account_id UUID,
    p_amount_minor BIGINT,
    p_currency TEXT,
    p_idempotency_key TEXT,
    p_actor_user_id UUID DEFAULT NULL
)
RETURNS journal_transactions
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    v_journal journal_transactions;
    v_existing journal_transactions;
    v_account accounts;
BEGIN
    SELECT * INTO v_existing FROM journal_transactions WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        RETURN v_existing;
    END IF;

    IF p_amount_minor <= 0 THEN
        RAISE EXCEPTION 'p_amount_minor must be positive.' USING ERRCODE = 'BK005';
    END IF;

    SELECT * INTO v_account FROM accounts WHERE id = p_account_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Account % not found.', p_account_id USING ERRCODE = 'BK002';
    END IF;
    IF v_account.status <> 'active' THEN
        RAISE EXCEPTION 'Account % is closed.', p_account_id USING ERRCODE = 'BK003';
    END IF;
    IF UPPER(v_account.currency) <> UPPER(p_currency) THEN
        RAISE EXCEPTION 'Account % is %, grant is %.', p_account_id, v_account.currency, p_currency USING ERRCODE = 'BK004';
    END IF;

    BEGIN
        INSERT INTO journal_transactions (reference, idempotency_key, description)
        VALUES ('INTEREST', p_idempotency_key, 'Dobanda acumulata')
        RETURNING * INTO v_journal;

        INSERT INTO ledger_entries (journal_id, account_id, direction, amount_minor, currency)
        VALUES (v_journal.id, p_account_id, 'credit', p_amount_minor, UPPER(p_currency));
    EXCEPTION WHEN unique_violation THEN
        SELECT * INTO v_existing FROM journal_transactions WHERE idempotency_key = p_idempotency_key;
        IF NOT FOUND THEN
            RAISE;
        END IF;
        RETURN v_existing;
    END;

    INSERT INTO audit_log (user_id, action, entity, metadata_json)
    VALUES (
        p_actor_user_id,
        'ledger.grant_interest',
        'journal_transactions:' || v_journal.id,
        jsonb_build_object('account_id', p_account_id, 'amount_minor', p_amount_minor, 'currency', p_currency)
    );

    RETURN v_journal;
END;
$$;

GRANT EXECUTE ON FUNCTION grant_interest(UUID, BIGINT, TEXT, TEXT, UUID) TO service_role;
