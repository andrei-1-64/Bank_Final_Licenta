"""Convert an amount between currencies at the official BNR reference rate.

The only tool in this package that touches neither the ledger nor the
`Context`. Conversion is a pure calculation over a public daily rate table:
there is no account to resolve, no ownership to check, and nothing about the
signed-in user that would change the answer. `context` is therefore accepted
and ignored - deliberately, not by oversight. Adding an account read here
would give a currency calculator a reason to touch the user's data, which is
exactly the shape of thing worth not having.

Rates come from `app.core.bnr_client`, which owns the fetching, the
`multiplier` handling, the cache and the stale-fallback rule. See its module
docstring - in particular the multiplier note, which is the difference between
a correct HUF conversion and one that is wrong by 100x.

RON is BNR's base currency. RON -> X divides, X -> RON multiplies, and
X -> Y goes through RON. Everything is `Decimal`; there are no floats
anywhere in the path, and the single rounding happens once, at the end.

Every user-facing string here is Romanian, including the failure messages -
the model is told what went wrong in the language it must answer in.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool
from app.core import bnr_client

logger = logging.getLogger(__name__)

#: Marks the data as coming from the real BNR feed rather than a guess.
_SOURCE_BNR = "bnr"

#: How the converted amount and the rate are reported. Two decimals for the
#: amount is the ordinary money convention; the rate keeps four, because a
#: per-unit rate for a currency quoted per 100 units (HUF at ~0.0146 RON) is
#: entirely zeros at two.
_AMOUNT_QUANTUM = Decimal("0.01")
_RATE_QUANTUM = Decimal("0.0001")

#: An ISO-4217 code, e.g. RON, EUR, USD. Case is normalised in `run`.
CurrencyCode = Annotated[str, StringConstraints(min_length=3, max_length=3)]

#: Upper bound on the amount. Not a business rule - just a guard so a model
#: that emits a nonsense magnitude gets a validation error instead of a
#: Decimal operation that takes a noticeable amount of time.
_MAX_AMOUNT = Decimal("1000000000")


class ConvertCurrencyInput(BaseModel):
    """Arguments the model may supply. Validated, but still untrusted.

    Nothing here can widen anything: the worst a bad argument can do is
    produce a Romanian error message.
    """

    # A Decimal, not a float: the model sends JSON, and a float round-trip is
    # precisely the sort of quiet error money code exists to avoid. `gt=0`
    # because converting zero or a negative amount is a mistake upstream
    # rather than something to answer.
    amount: Decimal = Field(
        gt=0,
        le=_MAX_AMOUNT,
        description=(
            "Suma de convertit, în unități întregi ale monedei sursă "
            "(de exemplu 100 pentru 100 EUR, nu 10000)."
        ),
    )
    from_currency: CurrencyCode = Field(
        description="Codul ISO al monedei sursă, de exemplu EUR, USD sau RON.",
    )
    to_currency: CurrencyCode = Field(
        description="Codul ISO al monedei în care se face conversia, de exemplu RON.",
    )


class ConvertCurrencyTool(Tool):
    name = "convert_currency"
    description = (
        "Convert an amount from one currency to another using the official "
        "daily reference rates published by Banca Națională a României (BNR). "
        "Returns the converted amount, the per-unit rate used, the BNR "
        "publication date of that rate, and whether the rate is stale. "
        "Always report the publication date to the user. This tool needs no "
        "account and no user identity."
    )
    input_schema = ConvertCurrencyInput
    read_only = True

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, ConvertCurrencyInput)
        # `context` intentionally unused - see the module docstring.

        amount = validated_input.amount
        source = validated_input.from_currency.strip().upper()
        target = validated_input.to_currency.strip().upper()

        # Same currency: a no-op, and worth answering without a network call.
        # It is also the one case that is correct even if BNR is unreachable.
        if source == target:
            return ToolResult(
                name=self.name,
                data={
                    "amount": _format(amount, _AMOUNT_QUANTUM),
                    "from_currency": source,
                    "to_currency": target,
                    "converted_amount": _format(amount, _AMOUNT_QUANTUM),
                    "rate": "1.0000",
                    "rate_date": None,
                    "stale": False,
                    "source": _SOURCE_BNR,
                    "note": "Aceeași monedă - suma rămâne neschimbată.",
                },
            )

        try:
            rates, stale = await bnr_client.get_rates()
        except bnr_client.BnrUnavailableError:
            # Cold cache and BNR unreachable. There is no honest answer, so
            # say so rather than letting the model improvise a rate - an
            # invented exchange rate looks exactly like a real one.
            logger.warning("convert_currency: BNR unavailable with a cold cache")
            return ToolResult.failure(
                name=self.name,
                error=(
                    "Cursul valutar BNR nu este disponibil momentan. "
                    "Te rog încearcă din nou peste câteva minute. "
                    "Nu pot estima un curs."
                ),
            )

        source_rate = rates.per_unit(source)
        target_rate = rates.per_unit(target)

        missing = [
            code
            for code, rate in ((source, source_rate), (target, target_rate))
            if rate is None
        ]
        if missing:
            return ToolResult.failure(
                name=self.name,
                error=(
                    f"BNR nu publică un curs pentru {', '.join(missing)}. "
                    f"Cursul din {rates.published_on.isoformat()} acoperă doar "
                    "monedele din lista oficială BNR."
                ),
            )
        assert source_rate is not None and target_rate is not None

        # Both rates are RON per one unit, so the cross rate is their ratio -
        # which collapses to the right thing when either side is RON (whose
        # rate is 1 by definition), so RON -> X, X -> RON and X -> Y are one
        # expression rather than three branches that could disagree.
        rate = source_rate / target_rate
        converted = amount * rate

        return ToolResult(
            name=self.name,
            data={
                "amount": _format(amount, _AMOUNT_QUANTUM),
                "from_currency": source,
                "to_currency": target,
                "converted_amount": _format(converted, _AMOUNT_QUANTUM),
                "rate": _format(rate, _RATE_QUANTUM),
                "rate_date": rates.published_on.isoformat(),
                "stale": stale,
                "source": _SOURCE_BNR,
                "note": (
                    "Curs din cache - BNR nu a putut fi contactat acum, "
                    f"cursul este cel publicat la {rates.published_on.isoformat()}."
                    if stale
                    else None
                ),
            },
        )


def _format(value: Decimal, quantum: Decimal) -> str:
    """A decimal as a plain string.

    A string, not a float: `ToolResult.data` is serialised with `json.dumps`,
    which cannot take a `Decimal` at all and would take a `float` while
    quietly changing it. ROUND_HALF_UP rather than Python's default
    banker's rounding, which is the convention users expect of money.
    """
    return str(value.quantize(quantum, rounding=ROUND_HALF_UP))
