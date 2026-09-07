"""What-if analysis: apply user-described adjustments (a spending cut, a
raise) to the measured baseline net-monthly rate and compare the resulting
projection against the unadjusted one.

The model supplies the adjustments as signed monthly amounts - it already
did the work of turning "cut coffee spending in half" into a concrete number
before calling this tool; the tool's job is only the arithmetic of applying
that delta over time, not interpreting the request.
"""

from __future__ import annotations

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


class Adjustment(BaseModel):
    """One delta to apply to the baseline net monthly change."""

    description: str = Field(min_length=1, description="What this adjustment represents.")
    monthly_amount_minor: int = Field(
        description=(
            "Signed change to net monthly cash flow, in minor units. Positive "
            "for more money (a raise, a spending cut framed as savings), "
            "negative for less (a new expense)."
        )
    )


class SimulateScenarioInput(BaseModel):
    """Arguments the model may supply. Validated, but still untrusted."""

    months_ahead: int = Field(
        ge=MIN_MONTHS_AHEAD,
        le=MAX_MONTHS_AHEAD,
        description="How many months into the future to project. 1-24.",
    )
    adjustments: list[Adjustment] = Field(
        default_factory=list,
        description="Deltas to apply to the baseline net monthly change. May be empty.",
    )
    account_id: str | None = Field(
        default=None,
        description=(
            "Optional. Restrict to one of the user's own accounts. Omit it to "
            "simulate across all of their accounts."
        ),
    )


def _project(balance_minor: int, net_monthly_minor: int, months_ahead: int) -> list[dict[str, Any]]:
    return [
        {"month": month, "projected_balance_minor": balance_minor + net_monthly_minor * month}
        for month in range(1, months_ahead + 1)
    ]


class SimulateScenarioTool(Tool):
    name = "simulate_scenario"
    description = (
        "Compare the user's baseline balance projection against a 'what if' "
        "scenario with one or more adjustments applied (a spending cut, a "
        "raise, a new recurring cost). Each adjustment is a signed monthly "
        "amount in minor units - work out the number from what the user "
        "described before calling this. Use this for 'what if I...' or "
        "'how much would it help if...' questions."
    )
    input_schema = SimulateScenarioInput
    read_only = True

    def __init__(self, supabase: AsyncClient) -> None:
        """`supabase` is the shared process-wide client (see db/supabase_client)."""
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, SimulateScenarioInput)

        balance_minor, currency = await current_balance(
            self._supabase, context, account_id=validated_input.account_id
        )
        income_avg, spending_avg, _has_history = await recent_monthly_averages(
            self._supabase, context, account_id=validated_input.account_id
        )
        baseline_net_minor = income_avg - spending_avg
        adjusted_net_minor = baseline_net_minor + sum(
            adj.monthly_amount_minor for adj in validated_input.adjustments
        )

        baseline_projection = _project(
            balance_minor, baseline_net_minor, validated_input.months_ahead
        )
        adjusted_projection = _project(
            balance_minor, adjusted_net_minor, validated_input.months_ahead
        )
        difference_at_end_minor = (
            adjusted_projection[-1]["projected_balance_minor"]
            - baseline_projection[-1]["projected_balance_minor"]
        )

        return ToolResult(
            name=self.name,
            data={
                "currency": currency,
                "baseline_net_monthly_minor": baseline_net_minor,
                "adjusted_net_monthly_minor": adjusted_net_minor,
                "adjustments_applied": [
                    {
                        "description": adj.description,
                        "monthly_amount_minor": adj.monthly_amount_minor,
                    }
                    for adj in validated_input.adjustments
                ],
                "baseline_projection": baseline_projection,
                "adjusted_projection": adjusted_projection,
                "difference_at_end_minor": difference_at_end_minor,
            },
        )
