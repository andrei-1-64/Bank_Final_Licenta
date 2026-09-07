"""Freeze/unfreeze one of the signed-in user's cards.

Write tools, unlike every other tool in this package - see the `read_only`
class attribute on each. Freezing/unfreezing is low-stakes and fully
reversible (the counterpart tool undoes it instantly), so it executes
directly rather than needing a separate propose/confirm UI step; the system
prompt still asks the model to say what it's about to do and get a "yes"
in the conversation first.

Cards are identified by their last 4 digits, never a raw id - the model
never sees (and the system prompt forbids it from asking for) a full card
number, and a UUID is nothing a human would type either.
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


class FreezeCardInput(BaseModel):
    last4: Last4 = Field(description="The last 4 digits of the card to freeze.")


class UnfreezeCardInput(BaseModel):
    last4: Last4 = Field(description="The last 4 digits of the card to unfreeze.")


class FreezeCardTool(Tool):
    name = "freeze_card"
    description = (
        "Freeze one of the signed-in user's cards, identified by its last 4 digits. "
        "A frozen card cannot be charged until unfrozen. Reversible via unfreeze_card. "
        "Confirm with the user before calling this."
    )
    input_schema = FreezeCardInput
    read_only = False

    def __init__(self, supabase: AsyncClient) -> None:
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, FreezeCardInput)
        from app.modules.cards import service as cards_service

        card_id = await cards_service.find_card_id_by_last4_for_owner(
            self._supabase, context.user_id, validated_input.last4
        )
        card = await cards_service.freeze_card_for_owner(self._supabase, context.user_id, card_id)
        return ToolResult(name=self.name, data={"last4": card["last4"], "status": card["status"]})


class UnfreezeCardTool(Tool):
    name = "unfreeze_card"
    description = (
        "Unfreeze one of the signed-in user's cards, identified by its last 4 digits, "
        "so it can be charged again. Confirm with the user before calling this."
    )
    input_schema = UnfreezeCardInput
    read_only = False

    def __init__(self, supabase: AsyncClient) -> None:
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, UnfreezeCardInput)
        from app.modules.cards import service as cards_service

        card_id = await cards_service.find_card_id_by_last4_for_owner(
            self._supabase, context.user_id, validated_input.last4
        )
        card = await cards_service.unfreeze_card_for_owner(
            self._supabase, context.user_id, card_id
        )
        return ToolResult(name=self.name, data={"last4": card["last4"], "status": card["status"]})
