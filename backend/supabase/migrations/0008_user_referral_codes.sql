-- Per-user referral codes: each user gets their own unique, shareable code
-- (checked in auth/service.py alongside the old hardcoded fallback code -
-- see FALLBACK_REFERRAL_CODE there). Generated lazily, on first call to
-- GET /users/me/referral-code, not at registration - most users never open
-- that panel, so there is no reason to burn a DB write on every signup for
-- something most rows will never need.
--
-- Se ruleaza dupa 0001-0007, in Supabase SQL Editor.

ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code VARCHAR(20) UNIQUE;
