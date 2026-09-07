"""Converting a ledger amount between currencies at a BNR rate.

THE UNIT BOUNDARY, which is the only interesting thing in this module.
`bnr_client` deals in MAJOR units: its rates say "5.0489 RON per one EUR".
Every amount in this system - accounts, ledger_entries, transfers, proposals
- is an integer of MINOR units. Getting that crossing wrong is a factor-of-100
error in either direction, and a factor-of-100 error in a transfer is money.

Work it through once, so nothing downstream has to:

    major_in     = minor_in / 100
    major_out    = major_in * rate
    minor_out    = major_out * 100
                 = (minor_in / 100) * rate * 100
                 = minor_in * rate

The two scale factors cancel, so converting minor units is the SAME
multiplication as converting major ones. That is a genuine simplification,
not a shortcut - but it holds only because both currencies use the same
number of decimal places. Every currency in this system is treated as having
two (see `core/money.py`), so it holds everywhere here. It would NOT hold for
a real JPY account, which has zero: see `convert_minor`'s docstring.

The rate itself is whatever `bnr_client` produced, already divided by BNR's
`multiplier` - so a currency BNR quotes per 100 units is a per-one-unit
number by the time it reaches this module, and there is no second hidden
factor of 100 anywhere in the path.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.core.bnr_client import BnrRates

#: Minor units per major unit. One constant, matching `core/money.py`'s
#: assumption, so the two cannot drift apart silently.
MINOR_UNITS_PER_MAJOR = 100


class UnsupportedCurrencyError(ValueError):
    """BNR publishes no rate for one of the two currencies."""


def rate_between(rates: BnrRates, from_currency: str, to_currency: str) -> Decimal:
    """How many units of `to_currency` one unit of `from_currency` buys.

    Both of BNR's quotes are "RON per one unit", so the cross rate is their
    ratio - which collapses to the right thing when either side IS RON (whose
    rate against itself is 1 by definition), so RON->X, X->RON and X->Y are
    one expression rather than three branches that could disagree.
    """
    source = rates.per_unit(from_currency)
    target = rates.per_unit(to_currency)
    missing = [
        code
        for code, rate in ((from_currency, source), (to_currency, target))
        if rate is None
    ]
    if missing:
        raise UnsupportedCurrencyError(", ".join(missing))
    assert source is not None and target is not None
    return source / target


def convert_minor(amount_minor: int, rate: Decimal) -> int:
    """`amount_minor` of the source currency, in minor units of the target.

    See the module docstring for why this is a plain multiplication and not a
    /100 followed by a *100.

    Rounds HALF_UP to a whole minor unit - the convention users expect of
    money, and the one thing here that is not exact: the fractional bani that
    rounding drops are the FX desk's, the same way they are at any bank. The
    desk's two ledger journals each balance regardless (see 0024_fx_desk.sql),
    because the rounded figure is what BOTH legs of the second journal carry.

    KNOWN LIMITATION: assumes the source and target currencies have the same
    number of decimal places. True for every currency this system handles.
    A real JPY account (zero decimals) would need the two scales carried
    separately; `core/money.py` makes the same two-decimal assumption today,
    so this is a limitation of the system, not of this function.
    """
    if amount_minor <= 0:
        raise ValueError("amount_minor must be positive")
    if rate <= 0:
        raise ValueError("rate must be positive")
    converted = Decimal(amount_minor) * rate
    return int(converted.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def format_minor(amount_minor: int, currency: str) -> str:
    """Romanian money formatting: „2.480,00 RON".

    Local to the FX texts rather than reaching for a shared formatter,
    because the propose tools' existing `_format_amount` writes a comma
    decimal without the thousands separator, and an FX amount is exactly
    where a missing separator turns 248000 into something misread.
    """
    major, minor = divmod(abs(amount_minor), MINOR_UNITS_PER_MAJOR)
    sign = "-" if amount_minor < 0 else ""
    return f"{sign}{major:,}".replace(",", ".") + f",{minor:02d} {currency}"
