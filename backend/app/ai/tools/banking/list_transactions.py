"""List recent ledger activity for one of the signed-in user's accounts.

Same identity rule as `get_balance`: the account is resolved through the
trusted `Context`, never taken from the model's arguments. A model-supplied
`account_id` may only NARROW the selection to an account the user already
owns; anything else is refused without echoing the identifier back.

`days_back` and `limit` are bounded in the schema, so an out-of-range value is
a clean validation failure the model can correct - not an unbounded query.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.ai.context import Context, IdentityError
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool

if TYPE_CHECKING:
    from supabase import AsyncClient

logger = logging.getLogger(__name__)

#: Matches what a typical banking app shows by default.
DEFAULT_DAYS_BACK = 30
MAX_DAYS_BACK = 365

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

AccountId = Annotated[str, StringConstraints(min_length=1)]


class ListTransactionsInput(BaseModel):
    """Arguments the model may supply. Validated, but still untrusted."""

    account_id: AccountId | None = Field(
        default=None,
        description=(
            "Optional. One of the user's own account identifiers. "
            "Omit it to use their default account — you do not need to know or "
            "guess account identifiers."
        ),
    )
    days_back: int = Field(
        default=DEFAULT_DAYS_BACK,
        ge=1,
        le=MAX_DAYS_BACK,
        description=(
            "How many days of history to return, counting back from now. "
            "Defaults to 30. Use 7 for 'this week', 90 for 'this quarter'."
        ),
    )
    limit: int = Field(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description="Maximum number of transactions to return, newest first. Defaults to 50.",
    )


class ListTransactionsTool(Tool):
    name = "list_transactions"
    description = (
        "List recent transactions for one of the signed-in user's accounts, newest "
        "first. Amounts are integers in minor units (e.g. cents); `direction` is "
        "'debit' (money out) or 'credit' (money in). Returns an empty list when "
        "there was no activity in the requested window."
    )
    input_schema = ListTransactionsInput
    read_only = True

    def __init__(self, supabase: AsyncClient) -> None:
        """`supabase` is the shared process-wide client (see db/supabase_client)."""
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, ListTransactionsInput)

        # Imported here rather than at module scope so the AI layer's tool
        # package stays importable without pulling in the banking modules.
        from app.modules.transactions import service as transactions_service

        try:
            # The ONLY place the account is decided. Untrusted input in, trusted
            # account out — or a refusal.
            account_id = context.resolve_account(validated_input.account_id)
        except IdentityError:
            # Log the refusal itself; never the identifier that was refused.
            logger.warning("access denied user_id=%s tool=%s", context.user_id, self.name)
            raise

        date_from = datetime.now(UTC) - timedelta(days=validated_input.days_back)

        # Scoped by the context user's id too: the service re-checks ownership
        # against the database before reading any entries.
        entries = await transactions_service.list_account_transactions_for_owner(
            self._supabase,
            context.user_id,
            uuid.UUID(account_id),
            date_from=date_from,
            limit=validated_input.limit,
        )

        return ToolResult(
            name=self.name,
            data={
                "account_id": account_id,
                "days_back": validated_input.days_back,
                "transactions": [
                    {
                        "id": str(entry.id),
                        "created_at": entry.created_at.isoformat(),
                        "amount_minor": entry.amount_minor,
                        "currency": entry.currency,
                        "direction": entry.direction.value,
                        "description": entry.description,
                        "reference": entry.reference,
                    }
                    for entry in entries
                ],
            },
        )
