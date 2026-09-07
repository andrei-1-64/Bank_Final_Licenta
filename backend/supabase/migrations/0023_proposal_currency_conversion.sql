-- Cross-currency transfer proposals: the rate is locked when the proposal is
-- CREATED, and the confirm path executes against the locked numbers rather
-- than re-fetching. BNR publishes once per business day, so a re-fetch would
-- almost always agree - but "almost always" is not what a user tapping
-- confirm on a specific figure has agreed to.
--
-- All six columns are NULLABLE and stay NULL for a same-currency transfer,
-- which is every transfer the product does today. Nothing reads them unless
-- `converted_amount_minor` is populated, so applying this migration changes
-- no existing behaviour on its own.
--
-- Amounts are BIGINT MINOR UNITS, matching accounts/ledger_entries/transfers
-- everywhere else in this schema. The BNR feed quotes rates per MAJOR unit
-- (5.0489 RON per one EUR), so the application converts at that boundary -
-- see the note on exchange_rate below.
--
-- Se ruleaza dupa 0001-0022, in Supabase SQL Editor.

ALTER TABLE proposals
    -- The SOURCE account's currency and the amount as the user stated it.
    ADD COLUMN IF NOT EXISTS original_currency TEXT,
    ADD COLUMN IF NOT EXISTS original_amount_minor BIGINT,

    -- The TARGET account's currency and what actually gets credited there.
    ADD COLUMN IF NOT EXISTS converted_currency TEXT,
    ADD COLUMN IF NOT EXISTS converted_amount_minor BIGINT,

    -- The rate applied, per ONE MAJOR unit of original_currency, expressed in
    -- converted_currency. Already multiplier-adjusted by the BNR client: a
    -- currency BNR quotes per 100 units (HUF, JPY) is divided down to a
    -- per-one-unit figure before it ever reaches here, so this column never
    -- needs a companion "multiplier" to be interpreted.
    --
    -- NUMERIC without a fixed scale on purpose: a per-unit HUF rate is around
    -- 0.0146, and NUMERIC(10,4) would round it to 0.0146 at best and 0.0000
    -- at a coarser scale. Unbounded NUMERIC stores exactly what was used.
    ADD COLUMN IF NOT EXISTS exchange_rate NUMERIC,

    -- BNR's own publication date for that rate - NOT the proposal's
    -- created_at. On a Monday morning, before the day's publication, these
    -- differ, and the date the user is shown must be the one BNR stands
    -- behind.
    ADD COLUMN IF NOT EXISTS exchange_rate_date DATE;

-- Either the whole conversion is recorded or none of it is. A row carrying a
-- converted amount but no rate (or vice versa) is not a proposal anyone can
-- audit afterwards, and the confirm path keys off these being consistent.
ALTER TABLE proposals
    DROP CONSTRAINT IF EXISTS proposals_currency_conversion_complete;
ALTER TABLE proposals
    ADD CONSTRAINT proposals_currency_conversion_complete CHECK (
        (
            original_currency IS NULL
            AND original_amount_minor IS NULL
            AND converted_currency IS NULL
            AND converted_amount_minor IS NULL
            AND exchange_rate IS NULL
            AND exchange_rate_date IS NULL
        )
        OR (
            original_currency IS NOT NULL
            AND original_amount_minor IS NOT NULL
            AND converted_currency IS NOT NULL
            AND converted_amount_minor IS NOT NULL
            AND exchange_rate IS NOT NULL
            AND exchange_rate_date IS NOT NULL
        )
    );

-- Amounts are positive when present, same rule the ledger enforces on a leg.
ALTER TABLE proposals
    DROP CONSTRAINT IF EXISTS proposals_conversion_amounts_positive;
ALTER TABLE proposals
    ADD CONSTRAINT proposals_conversion_amounts_positive CHECK (
        (original_amount_minor IS NULL OR original_amount_minor > 0)
        AND (converted_amount_minor IS NULL OR converted_amount_minor > 0)
        AND (exchange_rate IS NULL OR exchange_rate > 0)
    );
