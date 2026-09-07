"""Look up a SAVED beneficiary (Payments > Beneficiari) by name, so the chat
agent can resolve "trimite lui Andrei Popescu 50 EUR" to an IBAN without
asking the user to type one they already gave the app once before.

Read-only: this only searches the user's own saved contacts. It does not
replace resolve_iban_holder - once a match's IBAN is found here, the agent
still calls resolve_iban_holder with that IBAN before propose_payment, the
same guardrail every other payment goes through (see propose_tools.py's
ProposePaymentTool docstring: the real account holder is always
re-resolved from the IBAN server-side, never trusted from a name alone).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool

if TYPE_CHECKING:
    from supabase import AsyncClient


class FindBeneficiaryByNameInput(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=200,
        description="The name (or part of it) the user said, e.g. 'Andrei' or 'Andrei Popescu'.",
    )


class FindBeneficiaryByNameTool(Tool):
    name = "find_beneficiary_by_name"
    description = (
        "Caută printre beneficiarii SALVAȚI ai utilizatorului (din Plăți > "
        "Beneficiari) după nume. Folosește-l când utilizatorul cere o plată "
        "către o persoană NUMITĂ, dar nu a dat un IBAN — dacă găsești o "
        "potrivire, folosește IBAN-ul găsit cu resolve_iban_holder ca de "
        "obicei (nu presupune identitatea doar din numele salvat). Dacă nu "
        "găsești nimic, cere-i utilizatorului IBAN-ul sau oferă-i opțiunea "
        "de a încărca un extras/poză."
    )
    input_schema = FindBeneficiaryByNameInput
    read_only = True

    def __init__(self, supabase: AsyncClient) -> None:
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, FindBeneficiaryByNameInput)

        from app.modules.beneficiaries.service import list_beneficiaries_for_owner

        beneficiaries = await list_beneficiaries_for_owner(self._supabase, context.user_id)

        needle = validated_input.name.strip().lower()
        matches = [b for b in beneficiaries if needle in b["display_name"].lower()]

        return ToolResult(
            name=self.name,
            data={
                "matches": [
                    {"display_name": b["display_name"], "iban": b["iban"]} for b in matches
                ]
            },
        )
