-- Referral-gated opening balance: registering with the referral code
-- "BanKTHA" marks the user eligible for the welcome balance
-- (grant_opening_balance, see 0004) on every account they open; without
-- it, new accounts start at zero. See app/modules/auth/service.py for
-- where the code is checked and app/modules/accounts/service.py for
-- where eligibility gates the grant.
--
-- Se ruleaza dupa 0001-0005, in Supabase SQL Editor.

ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_bonus_eligible BOOLEAN NOT NULL DEFAULT FALSE;
