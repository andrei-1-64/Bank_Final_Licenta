"""List transfers the signed-in user has made between their own accounts.

Unlike `list_transactions`, this takes no `account_id`: a transfer spans two
accounts, so the natural scope is the user rather than any single account. The
set is derived from the trusted `Context`'s user id, so there is nothing here
for the model to widen.

Note this covers transfers between the user's OWN accounts. Money sent to
someone else by IBAN is the payments module, which has no tool yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool

if TYPE_CHECKING:
    from supabase import AsyncClient

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


class ListTransfersInput(BaseModel):
    """Arguments the model may supply. Validated, but still untrusted."""

    limit: int = Field(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description="Maximum number of transfers to return, newest first. Defaults to 20.",
    )


class ListTransfersTool(Tool):
    name = "list_transfers"
    description = (
        "List transfers the signed-in user has made between their own accounts, "
        "newest first. Amounts are integers in minor units (e.g. cents). The user's "
        "identity is supplied by the application, not by you."
    )
    input_schema = ListTransfersInput
    read_only = True

    def __init__(self, supabase: AsyncClient) -> None:
        """`supabase` is the shared process-wide client (see db/supabase_client)."""
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, ListTransfersInput)

        # Imported here rather than at module scope so the AI layer's tool
        # package stays importable without pulling in the banking modules.
        from app.modules.transfers import service as transfers_service

        # Scoped by the context user's id: the service resolves the user's
        # accounts first and only reads transfers sent from those.
        transfers = await transfers_service.list_transfers_for_owner(
            self._supabase, context.user_id, limit=validated_input.limit
        )

        return ToolResult(
            name=self.name,
            data={
                "transfers": [
                    {
                        "id": str(transfer["id"]),
                        "created_at": transfer["created_at"],
                        "from_account_id": str(transfer["from_account_id"]),
                        "to_account_id": str(transfer["to_account_id"]),
                        "amount_minor": transfer["amount_minor"],
                        "currency": transfer["currency"],
                        "status": transfer["status"],
                    }
                    for transfer in transfers
                ]
            },
        )
