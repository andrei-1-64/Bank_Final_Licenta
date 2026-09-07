"""Propose ordering a physical card, for the user to review and confirm.

Unlike every other write tool in this package (freeze_card.py etc.), this
one stays `read_only = True` and never touches the database: shipping a
physical card needs a full postal address (name, phone, address, city,
postal code, country) gathered piece by piece over several turns, and
getting one of those fields wrong is a lot more annoying to undo than
"unfreeze the card" - a real address goes out with the shipment. So this
tool only VALIDATES that the named account is the user's own and echoes the
gathered fields back as a proposal; chat/router.py lifts that proposal onto
`ChatResponse.proposal`, and the frontend renders a confirmation card whose
button calls the real POST /card-orders - the same "propose, app executes"
split as onboarding's propose_registration tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.ai.context import Context, IdentityError
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool

if TYPE_CHECKING:
    from supabase import AsyncClient


class ProposeCardOrderInput(BaseModel):
    account_id: str | None = Field(
        default=None,
        description=(
            "Optional. One of the user's own account identifiers, to attach the "
            "new card to. Omit it to use their default account."
        ),
    )
    full_name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=1, max_length=20)
    address: str = Field(min_length=1, max_length=255)
    city: str = Field(min_length=1, max_length=100)
    postal_code: str = Field(min_length=1, max_length=20)
    country: str = Field(min_length=1, max_length=100)


class ProposeCardOrderTool(Tool):
    name = "propose_card_order"
    description = (
        "Once you have collected the account, full name, phone, shipping address "
        "(street, city, postal code, country) for a physical card order, call this "
        "to prepare it for the user's review. It does NOT place the order - the "
        "application shows the user a confirmation card and only orders it if they "
        "approve. Never invent any field the user didn't give you."
    )
    input_schema = ProposeCardOrderInput
    read_only = True

    def __init__(self, supabase: AsyncClient) -> None:
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, ProposeCardOrderInput)

        try:
            account_id = context.resolve_account(validated_input.account_id)
        except IdentityError:
            raise

        return ToolResult(
            name=self.name,
            data={
                "account_id": account_id,
                "full_name": validated_input.full_name,
                "phone": validated_input.phone,
                "address": validated_input.address,
                "city": validated_input.city,
                "postal_code": validated_input.postal_code,
                "country": validated_input.country,
            },
        )
