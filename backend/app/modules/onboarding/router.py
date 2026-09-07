"""POST /onboarding/chat - Comodul, the pre-auth registration assistant.

No session required: this runs before an account exists. History is
client-held (see OnboardingChatRequest) rather than server-stored, since
there's no authenticated user to own a conversations row yet - see
app/modules/chat/router.py for the authenticated equivalent this mirrors.
The actual account only ever gets created by the frontend POSTing to
/auth/register once the user explicitly confirms - this endpoint only ever
proposes, never submits.
"""

import json
import uuid

from fastapi import APIRouter, Depends
from supabase import AsyncClient

from app.ai.context import build_context
from app.ai.providers.base import ModelProvider, ProviderError
from app.ai.schemas import Message
from app.ai.tools.registry import ToolRegistry
from app.core.exceptions import AIProviderError
from app.db.supabase_client import get_supabase
from app.modules.chat.router import get_model_provider
from app.modules.onboarding.agent import OnboardingAgent
from app.modules.onboarding.schemas import OnboardingChatRequest, OnboardingChatResponse
from app.modules.onboarding.tool import CheckExistingAccountTool, ProposeRegistrationTool

router = APIRouter()


def _extract_tool_result(trace: list[Message], tool_name: str) -> dict | None:
    for message in trace:
        if message.role != "tool" or message.name != tool_name:
            continue
        payload = json.loads(message.content or "{}")
        if payload.get("ok"):
            return payload.get("result")
    return None


def _redact_passwords(history: list[Message]) -> list[Message]:
    """Defense-in-depth, on top of propose_registration's schema having no
    password field at all (see onboarding/tool.py): that schema only
    validates the tool's RESULT. The raw arguments a model requested are
    preserved verbatim on the assistant's own tool_calls (needed so the
    provider sees its own prior turn correctly on the next request) - a
    model that doesn't perfectly follow the "never ask for a password"
    instruction could still put one there, and this is client-held history
    that round-trips back to the client and to the model on every later
    turn. Strips a `password` key from any tool call's arguments,
    regardless of which tool, before the history ever leaves this
    process."""
    redacted = []
    for message in history:
        if not message.tool_calls or not any("password" in c.arguments for c in message.tool_calls):
            redacted.append(message)
            continue
        cleaned_calls = [
            call.model_copy(update={"arguments": {k: v for k, v in call.arguments.items() if k != "password"}})
            for call in message.tool_calls
        ]
        redacted.append(message.model_copy(update={"tool_calls": cleaned_calls}))
    return redacted


@router.post("/chat", response_model=OnboardingChatResponse)
async def chat(
    payload: OnboardingChatRequest,
    provider: ModelProvider = Depends(get_model_provider),
    supabase: AsyncClient = Depends(get_supabase),
) -> OnboardingChatResponse:
    # No real identity exists yet - a fresh synthetic id per request is
    # fine since neither tool resolves anything through context (one has
    # no DB access at all, the other only reads users by email/national_id,
    # not by identity).
    context = build_context(
        user_id=f"onboarding-{uuid.uuid4()}", account_ids=(), language=payload.language
    )

    tools = ToolRegistry([ProposeRegistrationTool(), CheckExistingAccountTool(supabase)])
    agent = OnboardingAgent(provider, tools)
    conversation = [*payload.history, Message(role="user", content=payload.message)]

    try:
        turn = await agent.run(conversation, context)
    except ProviderError as exc:
        raise AIProviderError() from exc

    reply, trace = turn.reply, turn.trace
    updated_history = _redact_passwords(
        [*conversation, *trace, Message(role="assistant", content=reply)]
    )

    account_conflict = _extract_tool_result(trace, CheckExistingAccountTool.name)
    if account_conflict is not None and not account_conflict.get("exists"):
        account_conflict = None

    return OnboardingChatResponse(
        reply=reply,
        history=updated_history,
        collected_fields=_extract_tool_result(trace, ProposeRegistrationTool.name),
        account_conflict=account_conflict,
    )
