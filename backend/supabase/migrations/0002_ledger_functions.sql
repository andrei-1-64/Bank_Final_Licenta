-- Functiile ledger-ului, ca functii Postgres (RPC), apelate prin supabase-py.
-- PostgREST ruleaza fiecare apel RPC intr-o singura tranzactie reala - de
-- aceea toata logica de locking/idempotenta din vechiul
-- backend/app/modules/ledger/service.py (Python + SQLAlchemy) trebuie sa
-- traiasca aici, nu in Python: nu exista alt mod de a pastra row-locking
-- (SELECT ... FOR UPDATE) peste mai multe apeluri REST separate.
--
-- Coduri de eroare custom (RAISE EXCEPTION ... USING ERRCODE), mapate 1:1
-- pe exceptiile Python deja existente (vezi core/exceptions.py):
--   BK001 -> InsufficientFundsError
--   BK002 -> AccountNotFoundError
--   BK003 -> AccountClosedError
--   BK004 -> CurrencyMismatchError
--   BK005 -> InvalidLedgerLegsError
-- Se ruleaza dupa 0001_initial_schema.sql, in Supabase SQL Editor.

-- ============================================================================
-- get_account_balance - SUM(credite) - SUM(debite), calculat mereu din nou
-- ============================================================================
CREATE OR REPLACE FUNCTION get_account_balance(p_account_id UUID)
RETURNS BIGINT
LANGUAGE sql
STABLE
SET search_path = public, pg_temp
AS $$
    SELECT COALESCE(
        SUM(CASE WHEN direction = 'credit' THEN amount_minor ELSE -amount_minor END),
        0
    )::BIGINT
    FROM ledger_entries
    WHERE account_id = p_account_id;
$$;

-- ============================================================================
-- post_transaction - singurul "money-writer". Legs vin ca JSONB:
--   [{"account_id": "<uuid>", "direction": "debit"|"credit",
--     "amount_minor": 1234, "currency": "USD"}, ...]
--
-- Ordinea reproduce exact ledger/service.py::post_transaction:
--   1. valideaza legs (>=2, o singura moneda, sume pozitive, debits==credits)
--   2. blocheaza conturile UNUL CATE UNUL, sortate dupa id, cu FOR UPDATE
--      (ordine fixa -> previne deadlock intre A->B si B->A concurente)
--   3. re-verifica idempotency DUPA lock-uri (double-checked locking)
--   4. verificari per-leg: moneda potrivita, sold suficient la debit
--   5. insert journal+entries intr-un bloc EXCEPTION WHEN unique_violation
--      (echivalentul PL/pgSQL al SAVEPOINT-ului din Python)
--   6. insert audit_log, in ACEEASI tranzactie -> ramane atomic cu banii
-- ============================================================================
CREATE OR REPLACE FUNCTION post_transaction(
    p_idempotency_key TEXT,
    p_description TEXT,
    p_legs JSONB,
    p_reference TEXT DEFAULT NULL,
    p_actor_user_id UUID DEFAULT NULL
)
RETURNS journal_transactions
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    v_journal journal_transactions;
    v_existing journal_transactions;
    v_leg RECORD;
    v_account_id UUID;
    v_account accounts;
    v_debits BIGINT;
    v_credits BIGINT;
    v_currency_count INT;
    v_leg_count INT;
    v_balance BIGINT;
BEGIN
    -- 1. Validare legs
    SELECT COUNT(*) INTO v_leg_count FROM jsonb_array_elements(p_legs);
    IF v_leg_count < 2 THEN
        RAISE EXCEPTION 'A journal needs at least two legs.' USING ERRCODE = 'BK005';
    END IF;

    SELECT COUNT(DISTINCT UPPER(x.currency)) INTO v_currency_count
    FROM jsonb_to_recordset(p_legs) AS x(account_id UUID, direction TEXT, amount_minor BIGINT, currency TEXT);
    IF v_currency_count > 1 THEN
        RAISE EXCEPTION 'All legs of one journal must share a currency.' USING ERRCODE = 'BK004';
    END IF;

    IF EXISTS (
        SELECT 1 FROM jsonb_to_recordset(p_legs) AS x(account_id UUID, direction TEXT, amount_minor BIGINT, currency TEXT)
        WHERE x.amount_minor <= 0
    ) THEN
        RAISE EXCEPTION 'leg.amount_minor must be positive.' USING ERRCODE = 'BK005';
    END IF;

    SELECT
        COALESCE(SUM(x.amount_minor) FILTER (WHERE x.direction = 'debit'), 0),
        COALESCE(SUM(x.amount_minor) FILTER (WHERE x.direction = 'credit'), 0)
    INTO v_debits, v_credits
    FROM jsonb_to_recordset(p_legs) AS x(account_id UUID, direction TEXT, amount_minor BIGINT, currency TEXT);

    IF v_debits != v_credits THEN
        RAISE EXCEPTION 'Unbalanced journal: debits=% != credits=%', v_debits, v_credits USING ERRCODE = 'BK005';
    END IF;

    -- 2. Blocheaza conturile, unul cate unul, in ordine sortata dupa id::text
    -- (acelasi ordine ca Python: sorted(set(account_ids), key=str))
    FOR v_account_id IN
        SELECT d.account_id FROM (
            SELECT DISTINCT x.account_id
            FROM jsonb_to_recordset(p_legs) AS x(account_id UUID, direction TEXT, amount_minor BIGINT, currency TEXT)
        ) AS d
        ORDER BY d.account_id::text
    LOOP
        SELECT * INTO v_account FROM accounts WHERE id = v_account_id FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Account % not found.', v_account_id USING ERRCODE = 'BK002';
        END IF;
        IF v_account.status <> 'active' THEN
            RAISE EXCEPTION 'Account % is closed.', v_account_id USING ERRCODE = 'BK003';
        END IF;
    END LOOP;

    -- 3. Re-verificare idempotency DUPA ce avem lock-urile (double-checked)
    SELECT * INTO v_existing FROM journal_transactions WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        RETURN v_existing;
    END IF;

    -- 4. Verificari per-leg: moneda potrivita cu contul, sold suficient la debit
    -- (contul e deja blocat mai sus, deci un simplu re-SELECT e sigur si consistent)
    FOR v_leg IN
        SELECT x.account_id, x.direction, x.amount_minor, x.currency
        FROM jsonb_to_recordset(p_legs) AS x(account_id UUID, direction TEXT, amount_minor BIGINT, currency TEXT)
    LOOP
        SELECT * INTO v_account FROM accounts WHERE id = v_leg.account_id;
        IF UPPER(v_account.currency) <> UPPER(v_leg.currency) THEN
            RAISE EXCEPTION 'Account % is %, leg is %.', v_leg.account_id, v_account.currency, v_leg.currency USING ERRCODE = 'BK004';
        END IF;
        IF v_leg.direction = 'debit' THEN
            v_balance := get_account_balance(v_leg.account_id);
            IF v_balance < v_leg.amount_minor THEN
                RAISE EXCEPTION 'Account % has insufficient funds.', v_leg.account_id USING ERRCODE = 'BK001';
            END IF;
        END IF;
    END LOOP;

    -- 5. Insert journal + entries. unique_violation pe idempotency_key ->
    -- alta cerere concurenta a castigat cursa -> raspuns replay, nu eroare.
    BEGIN
        INSERT INTO journal_transactions (reference, idempotency_key, description)
        VALUES (
            COALESCE(p_reference, UPPER(SUBSTRING(REPLACE(gen_random_uuid()::text, '-', ''), 1, 12))),
            p_idempotency_key,
            p_description
        )
        RETURNING * INTO v_journal;

        FOR v_leg IN
            SELECT x.account_id, x.direction, x.amount_minor, x.currency
            FROM jsonb_to_recordset(p_legs) AS x(account_id UUID, direction TEXT, amount_minor BIGINT, currency TEXT)
        LOOP
            INSERT INTO ledger_entries (journal_id, account_id, direction, amount_minor, currency)
            VALUES (v_journal.id, v_leg.account_id, v_leg.direction, v_leg.amount_minor, UPPER(v_leg.currency));
        END LOOP;
    EXCEPTION WHEN unique_violation THEN
        SELECT * INTO v_existing FROM journal_transactions WHERE idempotency_key = p_idempotency_key;
        IF NOT FOUND THEN
            RAISE;
        END IF;
        RETURN v_existing;
    END;

    -- 6. Audit, in aceeasi tranzactie ca banii
    INSERT INTO audit_log (user_id, action, entity, metadata_json)
    VALUES (
        p_actor_user_id,
        'ledger.post_transaction',
        'journal_transactions:' || v_journal.id,
        jsonb_build_object('idempotency_key', p_idempotency_key, 'legs', p_legs)
    );

    RETURN v_journal;
END;
$$;

-- ============================================================================
-- create_transfer - wrapper subtire peste post_transaction + insert in
-- transfers (are propriul UNIQUE(idempotency_key), separat de cel de pe
-- journal_transactions). Apelul catre post_transaction() din interiorul
-- aceleiasi functii PL/pgSQL ramane in ACEEASI tranzactie - lock-urile nu
-- se pierd.
-- ============================================================================
CREATE OR REPLACE FUNCTION create_transfer(
    p_from_account_id UUID,
    p_to_account_id UUID,
    p_amount_minor BIGINT,
    p_currency TEXT,
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
    v_journal journal_transactions;
    v_legs JSONB;
    v_transfer transfers;
BEGIN
    -- Pre-verificare pe cheia de idempotenta proprie transferurilor, inainte
    -- sa atingem ledger-ul deloc (scurtatura ieftina, ca in Python).
    SELECT * INTO v_existing_transfer FROM transfers WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        RETURN v_existing_transfer;
    END IF;

    v_legs := jsonb_build_array(
        jsonb_build_object('account_id', p_from_account_id, 'direction', 'debit', 'amount_minor', p_amount_minor, 'currency', p_currency),
        jsonb_build_object('account_id', p_to_account_id, 'direction', 'credit', 'amount_minor', p_amount_minor, 'currency', p_currency)
    );

    v_journal := post_transaction(
        p_idempotency_key := p_idempotency_key,
        p_description := p_description,
        p_legs := v_legs,
        p_actor_user_id := p_actor_user_id
    );

    -- Daca post_transaction a returnat un journal deja existent (replay),
    -- s-ar putea sa existe deja si un rand in transfers pentru el.
    SELECT * INTO v_existing_transfer FROM transfers WHERE journal_id = v_journal.id;
    IF FOUND THEN
        RETURN v_existing_transfer;
    END IF;

    BEGIN
        INSERT INTO transfers (journal_id, from_account_id, to_account_id, amount_minor, currency, idempotency_key)
        VALUES (v_journal.id, p_from_account_id, p_to_account_id, p_amount_minor, UPPER(p_currency), p_idempotency_key)
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

-- --------------------------------------------------------------------------
-- Permisiuni: functiile trebuie sa fie executabile de service_role (Supabase
-- revoca implicit EXECUTE de la PUBLIC pe functii noi, din motive de
-- securitate).
-- --------------------------------------------------------------------------
GRANT EXECUTE ON FUNCTION get_account_balance(UUID) TO service_role;
GRANT EXECUTE ON FUNCTION post_transaction(TEXT, TEXT, JSONB, TEXT, UUID) TO service_role;
GRANT EXECUTE ON FUNCTION create_transfer(UUID, UUID, BIGINT, TEXT, TEXT, TEXT, UUID) TO service_role;
