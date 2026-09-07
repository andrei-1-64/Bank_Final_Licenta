"""Is a financial goal reachable by a given date, and if so how much needs to
be saved monthly? Feasibility is judged against the user's real measured
income and spending over the last 3 months, not an assumed rate.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool
from app.ai.tools.planning._shared import current_balance, recent_monthly_averages

if TYPE_CHECKING:
    from supabase import AsyncClient

ALREADY_ACHIEVED = "already_achieved"
FEASIBLE = "feasible"
FEASIBLE_WITH_CUTS = "feasible_with_cuts"
NOT_FEASIBLE = "not_feasible"


def _format_amount(amount_minor: int, currency: str) -> str:
    """Romanian money format: comma decimal, two places - e.g. '285,72 RON'."""
    return f"{amount_minor / 100:,.2f} {currency}".replace(",", "X").replace(".", ",").replace(
        "X", "."
    )


class SavingsGoalInput(BaseModel):
    """Arguments the model may supply. Validated, but still untrusted."""

    goal_amount_minor: int = Field(gt=0, description="Target amount, in minor units.")
    target_date: date = Field(description="ISO date the goal should be reached by.")
    account_id: str | None = Field(
        default=None,
        description=(
            "Optional. Restrict to one of the user's own accounts. Omit it to "
            "use the balance across all of their accounts."
        ),
    )

    @field_validator("target_date")
    @classmethod
    def _must_be_future(cls, value: date) -> date:
        if value <= datetime.now(UTC).date():
            raise ValueError("target_date must be in the future")
        return value


class SavingsGoalTool(Tool):
    name = "savings_goal"
    description = (
        "Check whether a financial goal (e.g. 'save 2500 RON for a PS5 by "
        "March') is achievable by a target date, given the user's real "
        "current balance and recent income/spending. Returns the required "
        "monthly savings rate and a feasibility verdict. Use this for 'can I "
        "afford', 'is it possible to save for', or 'how much do I need to "
        "save' questions with a concrete goal and deadline."
    )
    input_schema = SavingsGoalInput
    read_only = True

    def __init__(self, supabase: AsyncClient) -> None:
        """`supabase` is the shared process-wide client (see db/supabase_client)."""
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, SavingsGoalInput)

        balance_minor, currency = await current_balance(
            self._supabase, context, account_id=validated_input.account_id
        )
        income_avg, spending_avg, _has_history = await recent_monthly_averages(
            self._supabase, context, account_id=validated_input.account_id
        )
        current_monthly_net_minor = income_avg - spending_avg

        gap_minor = validated_input.goal_amount_minor - balance_minor
        today = datetime.now(UTC).date()
        months_remaining = (validated_input.target_date.year - today.year) * 12 + (
            validated_input.target_date.month - today.month
        )
        if validated_input.target_date.day < today.day:
            months_remaining -= 1
        months_remaining = max(months_remaining, 0)

        # A deadline inside the current month still needs a rate, not a
        # division by zero - treat it as needing the whole gap within one
        # month, which is the conservative (highest) required rate anyway.
        required_monthly_savings_minor = (
            round(gap_minor / max(months_remaining, 1)) if gap_minor > 0 else 0
        )

        if gap_minor <= 0:
            feasibility = ALREADY_ACHIEVED
            suggestion = (
                "Ai deja suma necesară pentru acest obiectiv — este deja atins."
            )
        elif required_monthly_savings_minor <= current_monthly_net_minor:
            feasibility = FEASIBLE
            suggestion = (
                f"Poți atinge acest obiectiv economisind "
                f"{_format_amount(required_monthly_savings_minor, currency)} pe lună — "
                "sub ritmul tău actual de economisire."
            )
        elif required_monthly_savings_minor <= income_avg:
            feasibility = FEASIBLE_WITH_CUTS
            required_str = _format_amount(required_monthly_savings_minor, currency)
            suggestion = (
                f"Ai nevoie să economisești {required_str} pe lună — peste ritmul "
                "tău actual, dar posibil dacă reduci cheltuielile."
            )
        else:
            feasibility = NOT_FEASIBLE
            suggestion = (
                "Obiectivul nu este realizabil în acest termen la venitul tău actual. "
                "Ai nevoie fie de mai mult timp, fie de o sumă mai mică."
            )

        return ToolResult(
            name=self.name,
            data={
                "goal_amount_minor": validated_input.goal_amount_minor,
                "current_balance_minor": balance_minor,
                "currency": currency,
                "gap_minor": gap_minor,
                "target_date": validated_input.target_date.isoformat(),
                "months_remaining": months_remaining,
                "required_monthly_savings_minor": required_monthly_savings_minor,
                "current_monthly_net_minor": current_monthly_net_minor,
                "feasibility": feasibility,
                "suggestion": suggestion,
            },
        )
