"""Project the user's balance N months ahead from their real transaction
history - pure arithmetic on a linear monthly rate, not a forecasting model.

The rate is either measured (average net of the last 3 months' real income
and spending) or supplied by the user via `monthly_savings_override` ("if I
save 500 RON/month"). Either way the tool always reports the measured
averages alongside the projection, so the model can show the user both "here
is your real trend" and "here is what you asked to project instead."
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool
from app.ai.tools.planning._shared import current_balance, recent_monthly_averages

if TYPE_CHECKING:
    from supabase import AsyncClient

MIN_MONTHS_AHEAD = 1
MAX_MONTHS_AHEAD = 24

NO_HISTORY_NOTE = "Nu există suficiente date istorice pentru o proiecție precisă."


class ProjectBalanceInput(BaseModel):
    """Arguments the model may supply. Validated, but still untrusted."""

    months_ahead: int = Field(
        ge=MIN_MONTHS_AHEAD,
        le=MAX_MONTHS_AHEAD,
        description="How many months into the future to project. 1-24.",
    )
    monthly_savings_override: int | None = Field(
        default=None,
        description=(
            "Optional. Use this exact net monthly change (minor units, may be "
            "negative) instead of the one computed from the user's last 3 "
            "months of history - e.g. 'if I save 500 RON/month'."
        ),
    )
    account_id: str | None = Field(
        default=None,
        description=(
            "Optional. Restrict to one of the user's own accounts. Omit it to "
            "project the balance across all of their accounts."
        ),
    )


class ProjectBalanceTool(Tool):
    name = "project_balance"
    description = (
        "Project the user's balance forward 1-24 months, using their real "
        "average monthly income and spending from the last 3 months (or an "
        "explicit monthly savings rate the user specifies). Also reports "
        "months_until_zero if spending is projected to outpace income. "
        "Use this for 'what will my balance be' or 'if I save X/month' "
        "questions."
    )
    input_schema = ProjectBalanceInput
    read_only = True

    def __init__(self, supabase: AsyncClient) -> None:
        """`supabase` is the shared process-wide client (see db/supabase_client)."""
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, ProjectBalanceInput)

        balance_minor, currency = await current_balance(
            self._supabase, context, account_id=validated_input.account_id
        )
        income_avg, spending_avg, has_history = await recent_monthly_averages(
            self._supabase, context, account_id=validated_input.account_id
        )

        no_history = not has_history and validated_input.monthly_savings_override is None
        net_monthly_minor = (
            validated_input.monthly_savings_override
            if validated_input.monthly_savings_override is not None
            else income_avg - spending_avg
        )

        projection: list[dict[str, Any]]
        if no_history:
            projection = [
                {"month": month, "projected_balance_minor": balance_minor}
                for month in range(1, validated_input.months_ahead + 1)
            ]
        else:
            projection = [
                {
                    "month": month,
                    "projected_balance_minor": balance_minor + net_monthly_minor * month,
                }
                for month in range(1, validated_input.months_ahead + 1)
            ]

        months_until_zero: int | None = None
        if not no_history and net_monthly_minor < 0:
            months_until_zero = (
                0 if balance_minor <= 0 else math.ceil(balance_minor / -net_monthly_minor)
            )

        data: dict[str, Any] = {
            "current_balance_minor": balance_minor,
            "currency": currency,
            "monthly_income_avg_minor": income_avg,
            "monthly_spending_avg_minor": spending_avg,
            "net_monthly_minor": net_monthly_minor,
            "projection": projection,
            "months_until_zero": months_until_zero,
        }
        if no_history:
            data["note"] = NO_HISTORY_NOTE

        return ToolResult(name=self.name, data=data)
