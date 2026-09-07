-- Referrer-side reward: whoever's per-user referral_code (see 0008) was used
-- at registration gets 300 RON when the person they referred opens an
-- account. Unlike the referred user's own welcome balance (paid instantly,
-- see accounts/service.py::OPENING_BALANCE_MINOR), this side needs a queue:
-- the referrer might not have a RON account yet (or any account at all) at
-- that moment, so the reward can sit `pending` until they open their first
-- RON account, whenever that happens - see accounts/service.py::open_account.
--
-- Se ruleaza dupa 0001-0008, in Supabase SQL Editor.

-- --------------------------------------------------------------------------
-- users: who referred this user (per-user code only - the fallback
-- "BanKTHA" code has no specific referrer to reward, so this stays NULL for
-- users who registered with it or with no code at all).
-- --------------------------------------------------------------------------
ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;

-- --------------------------------------------------------------------------
-- referral_rewards
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS referral_rewards (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    referrer_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    referred_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- The specific account-opening that earned this reward. UNIQUE so a
    -- retried request can never create a second reward row for the same
    -- event (idempotency by natural key, same idea as everywhere else's
    -- idempotency_key columns, just expressed as a constraint here).
    referred_account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    amount_minor BIGINT NOT NULL,
    currency VARCHAR(3) NOT NULL,
    status VARCHAR(10) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid')),
    paid_account_id UUID REFERENCES accounts(id) ON DELETE RESTRICT,
    paid_at TIMESTAMPTZ,
    UNIQUE (referred_account_id)
);
CREATE INDEX IF NOT EXISTS idx_referral_rewards_referrer_pending
    ON referral_rewards (referrer_user_id) WHERE status = 'pending';

-- --------------------------------------------------------------------------
-- RLS + grants for service_role (see 0001's note: tables created directly
-- from the SQL Editor don't get the default privileges Supabase normally
-- sets up for CLI-migrated tables).
-- --------------------------------------------------------------------------
ALTER TABLE referral_rewards ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON referral_rewards TO service_role;
