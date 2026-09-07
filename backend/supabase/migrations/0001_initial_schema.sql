-- Schema initiala pentru backend-ul BanK (Dev2.1), pe Supabase REST.
-- Se ruleaza manual in Supabase SQL Editor (proiectul gzeteavjfrgmptsnsetz).
--
-- ATENTIE: sterge tabelele vechi ramase de la prototipul de pe branch-ul
-- `login` (users cu nume/prenume/cnp, login_attempts, otp_codes) - schema
-- aceea era complet diferita si nu mai e folosita. Contul de test
-- davidgradea@yahoo.com de acolo se pierde odata cu stergerea.
DROP TABLE IF EXISTS otp_codes CASCADE;
DROP TABLE IF EXISTS login_attempts CASCADE;
DROP TABLE IF EXISTS users CASCADE;

-- --------------------------------------------------------------------------
-- users
-- --------------------------------------------------------------------------
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    email VARCHAR(320) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    email_verified BOOLEAN NOT NULL DEFAULT FALSE,
    national_id VARCHAR(13) UNIQUE,
    gender VARCHAR(1),
    date_of_birth DATE,
    phone VARCHAR(20),
    address VARCHAR(255)
);

-- --------------------------------------------------------------------------
-- sessions
-- --------------------------------------------------------------------------
CREATE TABLE sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash VARCHAR(64) UNIQUE NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_sessions_user_id ON sessions (user_id);

-- --------------------------------------------------------------------------
-- login_attempts (append-only, rate limiting)
-- --------------------------------------------------------------------------
CREATE TABLE login_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    email VARCHAR(320) NOT NULL,
    success BOOLEAN NOT NULL
);
CREATE INDEX idx_login_attempts_email_created_at ON login_attempts (email, created_at DESC);

-- --------------------------------------------------------------------------
-- accounts (fara coloana de sold - soldul se calculeaza mereu din ledger_entries)
-- --------------------------------------------------------------------------
CREATE TABLE accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    name VARCHAR(255) NOT NULL,
    currency VARCHAR(3) NOT NULL,
    status VARCHAR(10) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed'))
);
CREATE INDEX idx_accounts_user_id ON accounts (user_id);

-- --------------------------------------------------------------------------
-- journal_transactions (grupeaza "picioarele"/legs unei singure miscari de bani)
-- --------------------------------------------------------------------------
CREATE TABLE journal_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reference VARCHAR(64) NOT NULL,
    idempotency_key VARCHAR(255) UNIQUE NOT NULL,
    description VARCHAR(500) NOT NULL
);

-- --------------------------------------------------------------------------
-- ledger_entries (IMUTABIL, append-only - soldul = SUM(ledger_entries))
-- --------------------------------------------------------------------------
CREATE TABLE ledger_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    journal_id UUID NOT NULL REFERENCES journal_transactions(id) ON DELETE RESTRICT,
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    direction VARCHAR(10) NOT NULL CHECK (direction IN ('debit', 'credit')),
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    currency VARCHAR(3) NOT NULL
);
CREATE INDEX idx_ledger_entries_journal_id ON ledger_entries (journal_id);
CREATE INDEX idx_ledger_entries_account_id ON ledger_entries (account_id);

-- --------------------------------------------------------------------------
-- transfers
-- --------------------------------------------------------------------------
CREATE TABLE transfers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    journal_id UUID NOT NULL REFERENCES journal_transactions(id) ON DELETE RESTRICT,
    from_account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    to_account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    amount_minor BIGINT NOT NULL,
    currency VARCHAR(3) NOT NULL,
    status VARCHAR(10) NOT NULL DEFAULT 'completed' CHECK (status IN ('completed', 'failed')),
    idempotency_key VARCHAR(255) UNIQUE NOT NULL
);
CREATE INDEX idx_transfers_from_account_id ON transfers (from_account_id);
CREATE INDEX idx_transfers_to_account_id ON transfers (to_account_id);

-- --------------------------------------------------------------------------
-- cards
-- --------------------------------------------------------------------------
CREATE TABLE cards (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    last4 VARCHAR(4) NOT NULL,
    status VARCHAR(10) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'frozen', 'cancelled')),
    spending_limit_minor BIGINT
);
CREATE INDEX idx_cards_account_id ON cards (account_id);

-- --------------------------------------------------------------------------
-- audit_log (append-only)
-- --------------------------------------------------------------------------
CREATE TABLE audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    action VARCHAR(100) NOT NULL,
    entity VARCHAR(255) NOT NULL,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX idx_audit_log_user_id ON audit_log (user_id);

-- --------------------------------------------------------------------------
-- RLS: activat peste tot, fara politici (deny-all pentru anon/authenticated).
-- Backend-ul foloseste doar cheia service_role, care ocoleste RLS automat -
-- asta e doar o plasa de siguranta ieftina impotriva unei chei gresite
-- expuse cumva catre frontend.
-- --------------------------------------------------------------------------
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE login_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE journal_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE transfers ENABLE ROW LEVEL SECURITY;
ALTER TABLE cards ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;

-- --------------------------------------------------------------------------
-- Permisiuni pentru service_role. Tabelele create direct din SQL Editor nu
-- primesc automat drepturile implicite pe care Supabase le seteaza de obicei
-- pentru tabelele create prin migratii CLI (lectie invatata deja o data pe
-- proiectul asta, cu prototipul de pe `login`).
-- --------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO service_role;
