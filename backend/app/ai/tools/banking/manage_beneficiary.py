"""Add or remove a saved beneficiary ("Contactele mele") by IBAN.

Write tools - see freeze_card.py's header for why these execute directly
instead of needing a propose/confirm UI step. Both are cheap to undo
(re-add, or remove again), so the risk here is "wrong data saved", not
"money moved" - saving a beneficiary never moves money by itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool

if TYPE_CHECKING:
    from supabase import AsyncClient


class AddBeneficiaryInput(BaseModel):
    iban: str = Field(min_length=15, max_length=34, description="The beneficiary's IBAN.")
    display_name: str = Field(
        min_length=1, max_length=200, description="Name to save this contact under."
    )


class RemoveBeneficiaryInput(BaseModel):
    iban: str = Field(
        min_length=15, max_length=34, description="The IBAN of the saved contact to remove."
    )


class AddBeneficiaryTool(Tool):
    name = "add_beneficiary"
    description = (
        "Save a new beneficiary/contact (IBAN + display name) to the signed-in "
        "user's saved payees, so they can be picked when paying someone later. "
        "Confirm the IBAN and name with the user before calling this."
    )
    input_schema = AddBeneficiaryInput
    read_only = False

    def __init__(self, supabase: AsyncClient) -> None:
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, AddBeneficiaryInput)
        from app.modules.beneficiaries import service as beneficiaries_service

        beneficiary = await beneficiaries_service.add_beneficiary_for_owner(
            self._supabase, context.user_id, validated_input.iban, validated_input.display_name
        )
        return ToolResult(
            name=self.name,
            data={"iban": beneficiary["iban"], "display_name": beneficiary["display_name"]},
        )


class RemoveBeneficiaryTool(Tool):
    name = "remove_beneficiary"
    description = (
        "Remove a saved beneficiary/contact from the signed-in user's saved payees, "
        "identified by IBAN. Confirm with the user before calling this."
    )
    input_schema = RemoveBeneficiaryInput
    read_only = False

    def __init__(self, supabase: AsyncClient) -> None:
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, RemoveBeneficiaryInput)
        from app.modules.beneficiaries import service as beneficiaries_service

        beneficiary_id = await beneficiaries_service.find_beneficiary_id_by_iban_for_owner(
            self._supabase, context.user_id, validated_input.iban
        )
        await beneficiaries_service.remove_beneficiary_for_owner(
            self._supabase, context.user_id, beneficiary_id
        )
        return ToolResult(name=self.name, data={"iban": validated_input.iban, "removed": True})
