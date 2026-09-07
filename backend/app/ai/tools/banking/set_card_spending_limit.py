"""Change the spending limit on one of the signed-in user's cards.

Write tool - see freeze_card.py's header for why this executes directly
instead of needing a propose/confirm UI step. Reversible (the limit can
always be changed again, including back to what it was).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool

if TYPE_CHECKING:
    from supabase import AsyncClient

Last4 = Annotated[str, StringConstraints(pattern=r"^\d{4}$")]


class SetCardSpendingLimitInput(BaseModel):
    last4: Last4 = Field(description="The last 4 digits of the card to change.")
    spending_limit_minor: int | None = Field(
        default=None,
        gt=0,
        description=(
            "New limit in minor units (e.g. cents), e.g. 100000 for a 1000.00 "
            "limit. Omit or pass null to remove the limit entirely."
        ),
    )


class SetCardSpendingLimitTool(Tool):
    name = "set_card_spending_limit"
    description = (
        "Change the spending limit on one of the signed-in user's cards, identified "
        "by its last 4 digits. Pass null to remove the limit entirely. "
        "Confirm the new amount with the user before calling this."
    )
    input_schema = SetCardSpendingLimitInput
    read_only = False

    def __init__(self, supabase: AsyncClient) -> None:
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, SetCardSpendingLimitInput)
        from app.modules.cards import service as cards_service

        card_id = await cards_service.find_card_id_by_last4_for_owner(
            self._supabase, context.user_id, validated_input.last4
        )
        card = await cards_service.set_spending_limit_for_owner(
            self._supabase, context.user_id, card_id, validated_input.spending_limit_minor
        )
        return ToolResult(
            name=self.name,
            data={"last4": card["last4"], "spending_limit_minor": card["spending_limit_minor"]},
        )
