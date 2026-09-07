-- Optional cancel/manage URL for a saved beneficiary, e.g. a subscription
-- merchant's website. Used by the subscription-price-increase block (see
-- payments/service.py::_detect_subscription_price_increase) to tell the
-- user exactly where to go if they'd rather cancel than pay the new price.
--
-- Se ruleaza dupa 0001-0013, in Supabase SQL Editor.

ALTER TABLE beneficiaries ADD COLUMN IF NOT EXISTS website VARCHAR(255);
