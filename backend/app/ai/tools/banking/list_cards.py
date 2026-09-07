"""List the signed-in user's cards - safe fields only.

SECURITY INVARIANT: this tool returns `last4` and never the full card number,
CVV, or expiry date. Those columns exist on the row this reads, so the
projection below is deliberate and explicit rather than a `**card` splat: the
model (and therefore the chat transcript, and therefore anything downstream
that renders it) must never see a usable card credential.

Takes no arguments - the card set is fully determined by the trusted
`Context`'s user id, so the model cannot reach anyone else's cards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool

if TYPE_CHECKING:
    from supabase import AsyncClient


class ListCardsInput(BaseModel):
    """No arguments: the user's cards are fully determined by their identity."""


class ListCardsTool(Tool):
    name = "list_cards"
    description = (
        "List all of the signed-in user's cards. Returns only the last 4 digits of "
        "each card number, plus its status and spending limit — never the full card "
        "number, CVV, or expiry date. Takes no arguments; the user's identity is "
        "supplied by the application, not by you."
    )
    input_schema = ListCardsInput
    read_only = True

    def __init__(self, supabase: AsyncClient) -> None:
        """`supabase` is the shared process-wide client (see db/supabase_client)."""
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, ListCardsInput)

        # Imported here rather than at module scope so the AI layer's tool
        # package stays importable without pulling in the banking modules.
        from app.modules.cards import service as cards_service

        # Scoped by the context user's id: the service resolves the user's
        # accounts first and only reads cards attached to those.
        cards = await cards_service.list_cards_for_owner(self._supabase, context.user_id)

        return ToolResult(
            name=self.name,
            data={
                "cards": [
                    # Explicit projection - see the SECURITY INVARIANT above.
                    {
                        "id": str(card["id"]),
                        "account_id": str(card["account_id"]),
                        "last4": card["last4"],
                        "status": card["status"],
                        "spending_limit_minor": card.get("spending_limit_minor"),
                    }
                    for card in cards
                ]
            },
        )
