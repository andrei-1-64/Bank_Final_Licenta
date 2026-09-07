"""Look up who an IBAN actually belongs to, before a payment is proposed.

Same lookup the manual "Plăți" form already does live while typing an IBAN
(see accounts/router.py::get_account_holder, accounts/service.py::
get_account_holder_by_iban) - this tool gives the chat agent the same
payee-name check, so it can show the user the REAL account holder and get
their confirmation before calling propose_payment, instead of trusting
whatever name the user typed in conversation.

Read-only: this only looks a name up, it never creates or changes anything.
`ProposePaymentTool` (see app/ai/tools/propose_tools.py) independently
re-resolves the holder server-side too - this tool is for the conversational
"is this really who you meant?" step, not the source of truth for what ends
up on the proposal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool
from app.core.exceptions import IbanNotFoundError

if TYPE_CHECKING:
    from supabase import AsyncClient


class ResolveIbanHolderInput(BaseModel):
    iban: str = Field(min_length=15, max_length=34, description="The IBAN to look up.")


class ResolveIbanHolderTool(Tool):
    name = "resolve_iban_holder"
    description = (
        "Verifică cui aparține un IBAN, ÎNAINTE de a pregăti o plată prin "
        "propose_payment. Întoarce numele real al titularului contului (dacă "
        "IBAN-ul aparține unui client BanK) sau found=false dacă nu aparține "
        "niciunui client BanK — caz în care nicio plată nu poate fi făcută "
        "către el. Folosește mereu acest tool înainte de propose_payment și "
        "arată numele găsit utilizatorului pentru confirmare."
    )
    input_schema = ResolveIbanHolderInput
    read_only = True

    def __init__(self, supabase: AsyncClient) -> None:
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, ResolveIbanHolderInput)
        del context  # public-within-the-app lookup, no ownership to check

        from app.modules.accounts import service as accounts_service

        try:
            holder = await accounts_service.get_account_holder_by_iban(
                self._supabase, validated_input.iban
            )
        except IbanNotFoundError:
            return ToolResult(name=self.name, data={"found": False})

        return ToolResult(
            name=self.name,
            data={
                "found": True,
                "first_name": holder["first_name"],
                "last_name": holder["last_name"],
            },
        )
