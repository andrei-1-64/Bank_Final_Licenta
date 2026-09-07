"""Read one of the signed-in user's account balances from the ledger.

The balance is derived server-side as SUM(ledger_entries) for the account (see
`ledger.service.get_balance`) - never a stored balance column, which does not
exist. Money stays in integer minor units; no floats, ever.

The identity path is the security-critical part: the account is resolved
through the trusted `Context`, never from the model's arguments. The model may
name an account only to NARROW the selection to one the user already owns;
anything else is refused. The database read is then scoped by the context
user's id as well, so ownership is enforced twice - once in memory, once in
the query.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.ai.context import Context, IdentityError
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool

if TYPE_CHECKING:
    from supabase import AsyncClient

logger = logging.getLogger(__name__)

#: Marks the data as coming from the real ledger rather than a placeholder.
_SOURCE_LEDGER = "ledger"

AccountId = Annotated[str, StringConstraints(min_length=1)]


class GetBalanceInput(BaseModel):
    """Arguments the model may supply. Validated, but still untrusted.

    `account_id` is optional and can only narrow: it is honoured when the
    context user owns it and refused otherwise. Omitting it selects the user's
    default account.
    """

    account_id: AccountId | None = Field(
        default=None,
        description=(
            "Optional. One of the user's own account identifiers. "
            "Omit it to use their default account — you do not need to know or "
            "guess account identifiers."
        ),
    )


class GetBalanceTool(Tool):
    name = "get_balance"
    description = (
        "Read the current balance of one of the signed-in user's accounts. "
        "Returns an integer amount in minor units (e.g. cents) plus the currency. "
        "The user's identity is supplied by the application, not by you."
    )
    input_schema = GetBalanceInput
    read_only = True

    def __init__(self, supabase: AsyncClient) -> None:
        """`supabase` is the shared process-wide client (see db/supabase_client)."""
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, GetBalanceInput)

        # Imported here rather than at module scope so the AI layer's tool
        # package stays importable without pulling in the banking modules.
        from app.modules.accounts import service as accounts_service
        from app.modules.ledger import service as ledger_service

        try:
            # The ONLY place the account is decided. Untrusted input in, trusted
            # account out — or a refusal.
            account_id = context.resolve_account(validated_input.account_id)
        except IdentityError:
            # Log the refusal itself; never the identifier that was refused.
            logger.warning(
                "access denied user_id=%s tool=%s", context.user_id, self.name
            )
            raise

        # Scoped by the context user's id too: a Context that has drifted from
        # the database cannot read an account its user no longer owns.
        account = await accounts_service.get_account_for_owner(
            self._supabase, context.user_id, account_id
        )
        balance_minor = await ledger_service.get_balance(
            self._supabase, uuid.UUID(str(account["id"]))
        )

        return ToolResult(
            name=self.name,
            data={
                "account_id": account_id,
                "balance_minor": balance_minor,
                "currency": account["currency"],
                "source": _SOURCE_LEDGER,
            },
        )
