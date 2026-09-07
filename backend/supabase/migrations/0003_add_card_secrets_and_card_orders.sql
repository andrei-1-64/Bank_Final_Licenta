-- Adauga persistarea numarului complet/expirare/CVV pe cards (revine
-- deliberat asupra deciziei de a nu stoca niciodata un PAN complet - sunt
-- numere fictive, fara retea de carduri reala in spate, si trebuie sa
-- ramana vizibile din lista de carduri, nu doar o singura data la emitere)
-- si adauga tabela card_orders (comanda unui card fizic, care emite si
-- leaga un Card real prin card_id).
--
-- Coloanele noi de pe cards sunt NULLABLE: tabela poate avea deja randuri
-- de dinainte de aceasta migratie, care nu au aceste valori - invariantul
-- "mereu completate la emitere" e impus in app (cards/service.py::issue_card),
-- nu in schema.
--
-- Se ruleaza dupa 0001_initial_schema.sql si 0002_ledger_functions.sql, in
-- Supabase SQL Editor.

-- --------------------------------------------------------------------------
-- cards: numar complet, expirare, CVV
-- --------------------------------------------------------------------------
ALTER TABLE cards ADD COLUMN IF NOT EXISTS card_number VARCHAR(19);
ALTER TABLE cards ADD COLUMN IF NOT EXISTS expiry_month SMALLINT CHECK (expiry_month BETWEEN 1 AND 12);
ALTER TABLE cards ADD COLUMN IF NOT EXISTS expiry_year SMALLINT;
ALTER TABLE cards ADD COLUMN IF NOT EXISTS cvv VARCHAR(4);

-- --------------------------------------------------------------------------
-- card_orders (comanda de card fizic - livrare la o adresa)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS card_orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    card_id UUID UNIQUE REFERENCES cards(id) ON DELETE RESTRICT,
    full_name VARCHAR(200) NOT NULL,
    phone VARCHAR(20) NOT NULL,
    address VARCHAR(255) NOT NULL,
    city VARCHAR(100) NOT NULL,
    postal_code VARCHAR(20) NOT NULL,
    country VARCHAR(100) NOT NULL,
    status VARCHAR(10) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'shipped', 'delivered', 'cancelled'))
);
CREATE INDEX IF NOT EXISTS idx_card_orders_account_id ON card_orders (account_id);

-- --------------------------------------------------------------------------
-- RLS + grants pentru service_role (vezi nota din 0001 despre tabelele
-- create direct din SQL Editor - nu primesc automat drepturile implicite).
-- --------------------------------------------------------------------------
ALTER TABLE card_orders ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON card_orders TO service_role;
