-- Adauga IBAN pe accounts (pentru plati - vezi validate_iban in
-- auth/validation.py, tinut anume pentru asta) si o functie RPC pentru
-- soldul initial acordat conturilor noi.
--
-- Coloana iban e NULLABLE: tabela poate avea deja randuri de dinainte de
-- aceasta migratie, fara IBAN - invariantul "mereu completat la creare" e
-- impus in app (accounts/service.py::open_account), nu in schema.
--
-- Se ruleaza dupa 0001/0002/0003, in Supabase SQL Editor.

-- --------------------------------------------------------------------------
-- accounts: IBAN unic
-- --------------------------------------------------------------------------
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS iban VARCHAR(34) UNIQUE;

-- --------------------------------------------------------------------------
-- grant_opening_balance - singura exceptie deliberata de la regula "orice
-- jurnal trebuie sa fie balansat" din post_transaction: un cont nou primeste
-- un sold initial care nu vine din alt cont, ci "din banca" (nu exista alt
-- mod de a introduce bani in sistem - vezi nota din design_decisions).
-- Foloseste acelasi tipar de locking + idempotenta ca post_transaction, ca
-- sa ramana singurul alt loc care scrie in ledger_entries.
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION grant_opening_balance(
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
        VALUES ('OPENING-BALANCE', p_idempotency_key, 'Sold initial la deschiderea contului')
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
        'ledger.grant_opening_balance',
        'journal_transactions:' || v_journal.id,
        jsonb_build_object('account_id', p_account_id, 'amount_minor', p_amount_minor, 'currency', p_currency)
    );

    RETURN v_journal;
END;
$$;

GRANT EXECUTE ON FUNCTION grant_opening_balance(UUID, BIGINT, TEXT, TEXT, UUID) TO service_role;
