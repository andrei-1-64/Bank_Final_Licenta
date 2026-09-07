-- Explicitly marks a saved contact as a subscription/merchant rather than
-- a person - gates the subscription-price-increase block (see
-- payments/service.py::_detect_subscription_price_increase) so a friend
-- who happens to get paid the same recurring amount twice never gets
-- mistaken for "Netflix raised its price." Defaults false: a plain
-- payment's auto-save (which never asks) never marks anyone a
-- subscription by accident - only the standalone add-contact form does.
--
-- Se ruleaza dupa 0001-0014, in Supabase SQL Editor.

ALTER TABLE beneficiaries ADD COLUMN IF NOT EXISTS is_subscription BOOLEAN NOT NULL DEFAULT false;
