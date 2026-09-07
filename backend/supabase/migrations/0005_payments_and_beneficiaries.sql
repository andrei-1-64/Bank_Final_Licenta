-- Plati intre useri diferiti, prin IBAN - spre deosebire de `transfers`
-- (strict intre conturile aceluiasi user), o plata poate ajunge la orice
-- cont din sistem identificat prin IBAN. Adauga si `beneficiaries` (agenda
-- de contacte a fiecarui user), populata automat cand o plata reuseste.
--
-- Se ruleaza dupa 0001/0002/0003/0004, in Supabase SQL Editor.

-- --------------------------------------------------------------------------
-- payments
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    journal_id UUID NOT NULL REFERENCES journal_transactions(id) ON DELETE RESTRICT,
    from_account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    to_account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    to_iban VARCHAR(34) NOT NULL,
    amount_minor BIGINT NOT NULL,
    currency VARCHAR(3) NOT NULL,
    status VARCHAR(10) NOT NULL DEFAULT 'completed' CHECK (status IN ('completed', 'failed')),
    idempotency_key VARCHAR(255) UNIQUE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_from_account_id ON payments (from_account_id);
CREATE INDEX IF NOT EXISTS idx_payments_to_account_id ON payments (to_account_id);

-- --------------------------------------------------------------------------
-- beneficiaries (agenda de contacte - IBAN + nume, per user)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS beneficiaries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    iban VARCHAR(34) NOT NULL,
    display_name VARCHAR(200) NOT NULL,
    UNIQUE (user_id, iban)
);
CREATE INDEX IF NOT EXISTS idx_beneficiaries_user_id ON beneficiaries (user_id);

-- --------------------------------------------------------------------------
-- create_payment - ca create_transfer, dar fara conditia "acelasi user":
-- to_account_id poate apartine oricui, gasit dupa IBAN (vezi
-- payments/service.py). Apeleaza post_transaction() din interiorul
-- aceleiasi functii PL/pgSQL, deci lock-urile raman in aceeasi tranzactie.
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION create_payment(
    p_from_account_id UUID,
    p_to_account_id UUID,
    p_to_iban TEXT,
    p_amount_minor BIGINT,
    p_currency TEXT,
    p_description TEXT,
    p_idempotency_key TEXT,
    p_actor_user_id UUID DEFAULT NULL
)
RETURNS payments
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    v_existing_payment payments;
    v_journal journal_transactions;
    v_legs JSONB;
    v_payment payments;
BEGIN
    SELECT * INTO v_existing_payment FROM payments WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        RETURN v_existing_payment;
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
    -- s-ar putea sa existe deja si un rand in payments pentru el.
    SELECT * INTO v_existing_payment FROM payments WHERE journal_id = v_journal.id;
    IF FOUND THEN
        RETURN v_existing_payment;
    END IF;

    BEGIN
        INSERT INTO payments (journal_id, from_account_id, to_account_id, to_iban, amount_minor, currency, idempotency_key)
        VALUES (v_journal.id, p_from_account_id, p_to_account_id, p_to_iban, p_amount_minor, UPPER(p_currency), p_idempotency_key)
        RETURNING * INTO v_payment;
    EXCEPTION WHEN unique_violation THEN
        SELECT * INTO v_existing_payment FROM payments WHERE idempotency_key = p_idempotency_key;
        IF NOT FOUND THEN
            RAISE;
        END IF;
        RETURN v_existing_payment;
    END;

    RETURN v_payment;
END;
$$;

GRANT EXECUTE ON FUNCTION create_payment(UUID, UUID, TEXT, BIGINT, TEXT, TEXT, TEXT, UUID) TO service_role;

-- --------------------------------------------------------------------------
-- RLS + grants pentru service_role (vezi nota din 0001 despre tabelele
-- create direct din SQL Editor - nu primesc automat drepturile implicite).
-- --------------------------------------------------------------------------
ALTER TABLE payments ENABLE ROW LEVEL SECURITY;
ALTER TABLE beneficiaries ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON payments TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON beneficiaries TO service_role;
