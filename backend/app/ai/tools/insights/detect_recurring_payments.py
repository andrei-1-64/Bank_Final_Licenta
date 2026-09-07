"""Find subscription-like recurring payments from transaction history.

Pattern-based, not a scrape of any merchant/subscription database: a payment
counts as recurring if the same normalized description/reference shows up in
2+ distinct calendar months at a roughly stable amount. "Roughly stable"
tolerates a subscription's price changing slightly (e.g. Spotify's yearly
price bump) without losing the match.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool
from app.ai.tools.insights._shared import load_rows, months_ago

if TYPE_CHECKING:
    from app.modules.transactions.schemas import TransactionEntryRead
    from supabase import AsyncClient

MIN_MONTHS_BACK = 1
MAX_MONTHS_BACK = 12
DEFAULT_MONTHS_BACK = 3

#: A group's amounts must all sit within this fraction of their mean to count
#: as "the same payment" - subscriptions occasionally change price slightly.
AMOUNT_TOLERANCE = 0.2

#: Fewer than this many distinct calendar months is a one-off, not a pattern.
MIN_DISTINCT_MONTHS = 2


def _normalized_key(entry: TransactionEntryRead) -> str:
    return (entry.description or entry.reference).strip().lower()


def _is_stable(amounts: list[int]) -> bool:
    mean = sum(amounts) / len(amounts)
    if mean == 0:
        return False
    return all(abs(amount - mean) <= AMOUNT_TOLERANCE * mean for amount in amounts)


class DetectRecurringPaymentsInput(BaseModel):
    """Arguments the model may supply. Validated, but still untrusted."""

    months_back: int = Field(
        default=DEFAULT_MONTHS_BACK,
        ge=MIN_MONTHS_BACK,
        le=MAX_MONTHS_BACK,
        description="How many months of history to scan for recurring payments. Defaults to 3.",
    )


class DetectRecurringPaymentsTool(Tool):
    name = "detect_recurring_payments"
    description = (
        "Find subscription-like recurring payments (streaming, gym, phone "
        "plan, etc.) in the user's transaction history, across all of their "
        "accounts. Use this for 'what subscriptions do I have' or 'how much "
        "do I pay in recurring costs' questions."
    )
    input_schema = DetectRecurringPaymentsInput
    read_only = True

    def __init__(self, supabase: AsyncClient) -> None:
        """`supabase` is the shared process-wide client (see db/supabase_client)."""
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, DetectRecurringPaymentsInput)

        now = datetime.now(UTC)
        date_from = months_ago(now, validated_input.months_back)
        entries = await load_rows(self._supabase, context, date_from=date_from, date_to=now)
        spending = [entry for entry in entries if entry.direction.value == "debit"]

        groups: dict[str, list[TransactionEntryRead]] = {}
        for entry in spending:
            groups.setdefault(_normalized_key(entry), []).append(entry)

        recurring_payments: list[dict[str, Any]] = []
        for group in groups.values():
            distinct_months = {(e.created_at.year, e.created_at.month) for e in group}
            amounts = [e.amount_minor for e in group]
            if len(distinct_months) < MIN_DISTINCT_MONTHS or not _is_stable(amounts):
                continue

            average_amount_minor = round(sum(amounts) / len(amounts))
            latest = max(group, key=lambda e: e.created_at)
            recurring_payments.append(
                {
                    # The raw (not normalized) description of the most recent
                    # occurrence reads better than a lowercased match key.
                    "name": latest.description or latest.reference,
                    "frequency": "monthly",
                    "average_amount_minor": average_amount_minor,
                    "currency": latest.currency,
                    "last_seen": latest.created_at.date().isoformat(),
                    "occurrences": len(group),
                    "total_spent_minor": sum(amounts),
                }
            )

        recurring_payments.sort(key=lambda p: int(p["average_amount_minor"]), reverse=True)
        estimated_monthly_cost_minor = sum(
            int(p["average_amount_minor"]) for p in recurring_payments
        )

        return ToolResult(
            name=self.name,
            data={
                "months_analyzed": validated_input.months_back,
                "recurring_payments": recurring_payments,
                "estimated_monthly_cost_minor": estimated_monthly_cost_minor,
            },
        )
