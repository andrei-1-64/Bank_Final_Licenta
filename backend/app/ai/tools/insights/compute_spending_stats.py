"""Aggregate income/spending/net statistics over a date range - the numbers a
dashboard summary tile would show, computed on demand instead of cached.

"Largest"/"smallest transaction" and "busiest day" are about SPENDING (debit
transactions) specifically, matching the rest of this tool's numbers - a
salary deposit being the "largest transaction" in a range would be a
confusing answer to a spending question.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, model_validator

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool
from app.ai.tools.insights._shared import day_bounds, load_rows

if TYPE_CHECKING:
    from app.modules.transactions.schemas import TransactionEntryRead
    from supabase import AsyncClient


def _transaction_summary(entry: TransactionEntryRead) -> dict[str, object]:
    return {
        "amount_minor": entry.amount_minor,
        "reference": entry.reference,
        "date": entry.created_at.date().isoformat(),
    }


class ComputeSpendingStatsInput(BaseModel):
    """Arguments the model may supply. Validated, but still untrusted."""

    start_date: date = Field(
        description="First day to include, inclusive. ISO format, e.g. 2026-08-01."
    )
    end_date: date = Field(
        description="Last day to include, inclusive. ISO format, e.g. 2026-08-31."
    )

    @model_validator(mode="after")
    def _range_is_ordered(self) -> ComputeSpendingStatsInput:
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class ComputeSpendingStatsTool(Tool):
    name = "compute_spending_stats"
    description = (
        "Compute aggregate income/spending statistics for the user across all "
        "of their accounts over a date range: totals, net, averages, the "
        "largest and smallest expense, and the busiest spending day. Use this "
        "for 'how much did I spend', 'financial summary', or 'statistics' "
        "questions. Returns zeros (not an error) when there is no activity."
    )
    input_schema = ComputeSpendingStatsInput
    read_only = True

    def __init__(self, supabase: AsyncClient) -> None:
        """`supabase` is the shared process-wide client (see db/supabase_client)."""
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, ComputeSpendingStatsInput)

        date_from, date_to = day_bounds(validated_input.start_date, validated_input.end_date)
        entries = await load_rows(
            self._supabase, context, date_from=date_from, date_to=date_to
        )

        total_income_minor = sum(
            e.amount_minor for e in entries if e.direction.value == "credit"
        )
        spending = [e for e in entries if e.direction.value == "debit"]
        total_spending_minor = sum(e.amount_minor for e in spending)

        largest_transaction = None
        smallest_transaction = None
        busiest_day = None
        avg_transaction_minor = 0

        if spending:
            largest_transaction = _transaction_summary(
                max(spending, key=lambda e: e.amount_minor)
            )
            smallest_transaction = _transaction_summary(
                min(spending, key=lambda e: e.amount_minor)
            )
            avg_transaction_minor = round(total_spending_minor / len(spending))

            by_day: dict[str, list[TransactionEntryRead]] = defaultdict(list)
            for entry in spending:
                by_day[entry.created_at.date().isoformat()].append(entry)
            busiest_date, busiest_entries = max(
                by_day.items(), key=lambda item: (len(item[1]), item[0])
            )
            busiest_day = {
                "date": busiest_date,
                "count": len(busiest_entries),
                "total_minor": sum(e.amount_minor for e in busiest_entries),
            }

        days_in_range = (validated_input.end_date - validated_input.start_date).days + 1
        daily_average_spending_minor = round(total_spending_minor / days_in_range)

        return ToolResult(
            name=self.name,
            data={
                "start_date": validated_input.start_date.isoformat(),
                "end_date": validated_input.end_date.isoformat(),
                "total_income_minor": total_income_minor,
                "total_spending_minor": total_spending_minor,
                "net_minor": total_income_minor - total_spending_minor,
                "transaction_count": len(entries),
                "avg_transaction_minor": avg_transaction_minor,
                "largest_transaction": largest_transaction,
                "smallest_transaction": smallest_transaction,
                "daily_average_spending_minor": daily_average_spending_minor,
                "busiest_day": busiest_day,
            },
        )
