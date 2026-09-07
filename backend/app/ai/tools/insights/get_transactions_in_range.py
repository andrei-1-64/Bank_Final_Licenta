"""Read every transaction the user made in a date window, across all accounts.

The analytical counterpart to `list_transactions`, and deliberately different
in three ways:

- **All accounts, not one.** "Unde s-au dus banii?" is a question about a
  person's money, not about one account, so there is no `account_id` parameter
  at all — the account set is derived from the trusted `Context`.
- **Oldest first.** A trend reads forwards in time; a statement reads backwards.
- **A much larger cap.** Spotting a quarterly pattern means seeing the quarter.

The model does the date arithmetic: it turns "săptămâna trecută" into concrete
ISO dates and passes those. That keeps natural-language calendar reasoning
(which the model is good at, and which is full of locale and timezone
judgement calls) out of the tool, and leaves the tool with one unambiguous
job.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, model_validator

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool
from app.ai.tools.insights._shared import load_rows

if TYPE_CHECKING:
    from supabase import AsyncClient

DEFAULT_LIMIT = 500
MAX_LIMIT = 2000


class GetTransactionsInRangeInput(BaseModel):
    """Arguments the model may supply. Validated, but still untrusted.

    `start_date` / `end_date` are typed as real dates rather than strings, so
    the advertised JSON Schema carries `"format": "date"` — that is what steers
    the model into emitting `2026-08-01` instead of `August 1st`. Anything
    unparseable is a clean validation failure it can correct, not a crash.
    """

    start_date: date = Field(
        description="First day to include, inclusive. ISO format, e.g. 2026-08-01."
    )
    end_date: date = Field(
        description="Last day to include, inclusive. ISO format, e.g. 2026-08-31."
    )
    limit: int = Field(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description="Maximum number of transactions to return, oldest first. Defaults to 500.",
    )

    @model_validator(mode="after")
    def _range_is_ordered(self) -> GetTransactionsInRangeInput:
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class GetTransactionsInRangeTool(Tool):
    name = "get_transactions_in_range"
    description = (
        "Read the signed-in user's transactions between two dates, across ALL of "
        "their accounts, oldest first. Use this for analysis: spending patterns, "
        "categories, trends, comparisons between periods. Work out the ISO dates "
        "yourself from what the user asked (e.g. 'last week', 'in October') and "
        "pass them in. Both dates are inclusive. Amounts are integers in minor "
        "units; `direction` is 'debit' (money out) or 'credit' (money in)."
    )
    input_schema = GetTransactionsInRangeInput
    read_only = True

    def __init__(self, supabase: AsyncClient) -> None:
        """`supabase` is the shared process-wide client (see db/supabase_client)."""
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, GetTransactionsInRangeInput)

        # Both bounds are inclusive whole days: "in October" means the 1st from
        # midnight through the 31st until midnight, not until the 31st at 00:00.
        date_from = datetime.combine(validated_input.start_date, time.min, tzinfo=UTC)
        date_to = datetime.combine(validated_input.end_date, time.max, tzinfo=UTC)

        entries = await load_rows(
            self._supabase,
            context,
            date_from=date_from,
            date_to=date_to,
            limit=validated_input.limit,
        )

        return ToolResult(
            name=self.name,
            data={
                "start_date": validated_input.start_date.isoformat(),
                "end_date": validated_input.end_date.isoformat(),
                "count": len(entries),
                "transactions": [
                    {
                        "id": str(entry.id),
                        "account_id": str(entry.account_id),
                        "created_at": entry.created_at.isoformat(),
                        "amount_minor": entry.amount_minor,
                        "currency": entry.currency,
                        "direction": entry.direction.value,
                        # `description` is not in the original field list, but an
                        # agent asked to categorise spending needs the human
                        # label ("Kaufland") - `reference` alone is an opaque code.
                        "description": entry.description,
                        "reference": entry.reference,
                    }
                    for entry in entries
                ],
            },
        )
