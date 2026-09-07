"""The onboarding agent's tools.

propose_registration is pure structured-data extraction: no DB access, no
side effects. It hands back whatever the model currently believes the
user's registration fields are, so the router can surface them to the
frontend as a confirmation screen. Account creation only ever happens
through POST /auth/register, fired by an explicit human confirmation
click - this tool never creates anything, and never bypasses that
endpoint's validation.

DELIBERATELY NO `password` FIELD HERE. A password typed into the chat
would reach the model as conversation text, come back as this tool's own
call arguments, get echoed into ToolResult.data (client-held history AND
the response's collected_fields - see onboarding/router.py), and round-
trip to the LLM provider again on every later turn - a credential
transiting through components that should never see it. The confirmation
screen (register.html's renderConfirmationCard) asks for the password
directly, in a real password field, right before POSTing to
/auth/register - it never passes through the chat/LLM pipeline at all.

check_existing_account is the one read-only exception to "no DB access":
it lets the agent short-circuit as soon as it has an email + CNP, instead
of only discovering a duplicate at the very end of a full interview.
"""

from typing import TYPE_CHECKING

from pydantic import BaseModel

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool
from app.modules.auth import service as auth_service

if TYPE_CHECKING:
    from supabase import AsyncClient


class ProposeRegistrationInput(BaseModel):
    email: str
    first_name: str
    last_name: str
    national_id: str
    phone: str | None = None
    address: str | None = None
    referral_code: str | None = None


class ProposeRegistrationTool(Tool):
    name = "propose_registration"
    description = (
        "Call this once you have collected all required fields (email, "
        "first_name, last_name, national_id) and have explicitly asked the "
        "user about each optional one (phone, address, referral_code) - "
        "even if they chose to skip it, pass null for it. NEVER ask for or "
        "include a password here - the confirmation screen the app shows "
        "next asks for it directly, in a real password field, never "
        "through this chat. This does NOT create the account; it only "
        "reports what you've gathered so the app can show the user a "
        "confirmation screen before anything is submitted."
    )
    input_schema = ProposeRegistrationInput
    read_only = True

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        return ToolResult(name=self.name, data=validated_input.model_dump())


class CheckExistingAccountInput(BaseModel):
    email: str
    national_id: str


class CheckExistingAccountTool(Tool):
    name = "check_existing_account"
    description = (
        "Call this as soon as you have BOTH the user's email and CNP, "
        "before asking anything else. Checks whether an account already "
        "exists for either one. If the result's exists is true, STOP the "
        "registration interview immediately - do not ask for name, "
        "password, or anything else. Tell the user calmly that an account "
        "already exists and that the app can help them reset their "
        "password instead."
    )
    input_schema = CheckExistingAccountInput
    read_only = True

    def __init__(self, supabase: "AsyncClient") -> None:
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, CheckExistingAccountInput)
        result = await auth_service.check_existing_account(
            self._supabase, validated_input.email, validated_input.national_id
        )
        # Echo the email back in the result (not a new disclosure - the
        # user just typed it into this same conversation) so the router
        # can build a password-reset link without re-parsing the trace.
        return ToolResult(name=self.name, data={**result, "email": validated_input.email})
