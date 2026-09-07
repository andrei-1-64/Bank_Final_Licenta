"""List every account the signed-in user owns, with live ledger balances.

Takes no arguments on purpose: there is nothing for the model to narrow, and
nothing for it to guess. The account set comes from the trusted `Context`'s
user id, so the model cannot ask about anyone else's accounts - not by naming
them, and not by omitting them.

Balances are included inline rather than left for a follow-up `get_balance`
call: users asking "ce conturi am?" almost always want the amounts too, and
one tool call beats N+1 round-trips through the model.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from pydantic import BaseModel

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool

if TYPE_CHECKING:
    from supabase import AsyncClient


class ListAccountsInput(BaseModel):
    """No arguments: the user's accounts are fully determined by their identity."""


class ListAccountsTool(Tool):
    name = "list_accounts"
    description = (
        "List all of the signed-in user's accounts, each with its current balance "
        "in minor units (e.g. cents) and currency. Takes no arguments - the user's "
        "identity is supplied by the application, not by you. Use this for questions "
        "like which or how many accounts the user has."
    )
    input_schema = ListAccountsInput
    read_only = True

    def __init__(self, supabase: AsyncClient) -> None:
        """`supabase` is the shared process-wide client (see db/supabase_client)."""
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, ListAccountsInput)

        # Imported here rather than at module scope so the AI layer's tool
        # package stays importable without pulling in the banking modules.
        from app.modules.accounts import service as accounts_service
        from app.modules.ledger import service as ledger_service

        # Scoped by the context user's id in the query itself - the only place
        # the account set is decided. A user with no accounts gets an empty
        # list, which is a legitimate state, not an error.
        accounts = await accounts_service.list_accounts_for_owner(
            self._supabase, context.user_id
        )

        listed = []
        for account in accounts:
            balance_minor = await ledger_service.get_balance(
                self._supabase, uuid.UUID(str(account["id"]))
            )
            listed.append(
                {
                    "id": str(account["id"]),
                    "name": account["name"],
                    "currency": account["currency"],
                    "iban": account.get("iban"),
                    "status": account["status"],
                    "balance_minor": balance_minor,
                }
            )

        return ToolResult(name=self.name, data={"accounts": listed})
