"""/chat - the AI layer's HTTP surface.

Identity is established here, at the edge: the session cookie resolves to a
`UserRead` via `get_current_user`, and `build_context_for_user` turns that into
the trusted `Context` every tool resolves accounts against. Nothing the client
sends contributes to identity.

The server owns conversation history (see `conversations_service`): the client
only ever holds a `conversation_id`, so the transcript survives a page reload.
"""

import json
import uuid

from fastapi import APIRouter, Depends

from app.ai.context import build_context_for_user
from app.ai.providers.base import ModelProvider, ProviderError
from app.ai.providers.embedding_base import EmbeddingProvider
from app.ai.providers.mock_embedding_provider import MockEmbeddingProvider
from app.ai.providers.mock_provider import MockProvider
from app.ai.schemas import Message, ModelResponse
from app.ai.service import AIService
from app.ai.tools.propose_tools import PROPOSE_TOOL_NAMES
from app.ai.turn import TurnDispatchResult
from app.config import ConfigurationError, Settings, get_settings
from app.core.dependencies import get_current_user
from app.core.exceptions import (
    AIProviderError,
    AIProviderMisconfiguredError,
    AIServiceUnavailableError,
)
from app.db.supabase_client import get_supabase
from app.modules.chat import conversations_service, proposals_service
from app.modules.chat.schemas import (
    ChatRequest,
    ChatResponse,
    ConversationRead,
    ConversationRenameRequest,
    MessageRead,
    ProposalConfirmRequest,
    ProposalRead,
)
from app.modules.documents import service as documents_service
from app.modules.statements import service as statements_service
from app.modules.users.schemas import UserRead
from supabase import AsyncClient

router = APIRouter()

PROVIDER_MOCK = "mock"
PROVIDER_AZURE = "azure"
_VALID_PROVIDERS = (PROVIDER_MOCK, PROVIDER_AZURE)

MOCK_REPLY = (
    "AI provider is set to mock — set AI_PROVIDER=azure in .env to use the real model."
)


def get_model_provider(settings: Settings = Depends(get_settings)) -> ModelProvider:
    """Pick the provider from configuration.

    A FastAPI dependency rather than a plain call so tests can override it with
    a scripted `MockProvider`, the same way they override `get_supabase`.
    """
    choice = settings.AI_PROVIDER.strip().lower()

    if choice == PROVIDER_MOCK:
        # `repeat_last` so a multi-turn conversation doesn't exhaust the script.
        return MockProvider([ModelResponse(text=MOCK_REPLY)], repeat_last=True)

    if choice == PROVIDER_AZURE:
        from app.ai.providers.azure_provider import AzureOpenAIProvider

        try:
            return AzureOpenAIProvider(settings)
        except ConfigurationError as exc:
            # The operator sees the specifics in the logs; the caller does not.
            raise AIServiceUnavailableError() from exc

    raise AIProviderMisconfiguredError(
        f"AI_PROVIDER={settings.AI_PROVIDER!r} is not a known provider. "
        f"Valid values: {', '.join(_VALID_PROVIDERS)}."
    )


def get_embedding_provider(settings: Settings = Depends(get_settings)) -> EmbeddingProvider:
    """Same provider-selection contract as `get_model_provider`, for embeddings.

    Kept as a separate dependency (not folded into `get_model_provider`)
    because a deployment can have chat configured without embeddings - only
    the docs agent's tool actually calls this one, so only asking for
    documentation should be able to fail on it.
    """
    choice = settings.AI_PROVIDER.strip().lower()

    if choice == PROVIDER_MOCK:
        return MockEmbeddingProvider()

    if choice == PROVIDER_AZURE:
        from app.ai.providers.azure_embedding_provider import AzureEmbeddingProvider

        try:
            return AzureEmbeddingProvider(settings)
        except ConfigurationError as exc:
            raise AIServiceUnavailableError() from exc

    raise AIProviderMisconfiguredError(
        f"AI_PROVIDER={settings.AI_PROVIDER!r} is not a known provider. "
        f"Valid values: {', '.join(_VALID_PROVIDERS)}."
    )


@router.post("", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
    provider: ModelProvider = Depends(get_model_provider),
    embedding_provider: EmbeddingProvider = Depends(get_embedding_provider),
) -> ChatResponse:
    if payload.conversation_id is None:
        conversation = await conversations_service.create_conversation(supabase, user)
    else:
        # Ownership-checked: raises ConversationNotFoundError for a foreign or
        # nonexistent id, never leaking which one it was.
        conversation = await conversations_service.get_conversation(
            supabase, user, payload.conversation_id
        )
    conversation_id = uuid.UUID(conversation["id"])

    # Ownership-checked BEFORE it ever reaches Context: get_document raises
    # NotFoundError for a foreign or nonexistent id, exactly like
    # conversations_service.get_conversation above - by the time
    # active_document_id is set below, "the caller owns this document" is
    # already an established fact, not something the orchestrator, the
    # agent, or the tool re-checks.
    active_document_id = None
    if payload.document_id is not None:
        await documents_service.get_document(supabase, str(user.id), str(payload.document_id))
        active_document_id = str(payload.document_id)

    # Explicit path first (ownership-checked exactly like document_id
    # above); if the turn named none, fall back to whatever statement was
    # most recently uploaded in this conversation - see
    # app/ai/context.py's Context.statement_id docstring for why this one
    # field is implicit where active_document_id is not.
    active_statement_id = None
    if payload.statement_id is not None:
        await statements_service.get_statement(supabase, str(user.id), str(payload.statement_id))
        active_statement_id = str(payload.statement_id)
    else:
        latest_statement = await statements_service.get_latest_statement_for_conversation(
            supabase, str(user.id), str(conversation_id)
        )
        if latest_statement is not None:
            active_statement_id = latest_statement["id"]

    # THE EDGE. Built from the authenticated session, never from the payload.
    # conversation_id is resolved above so propose_* tools can attach a
    # proposal to this turn's conversation (proposals.conversation_id is
    # NOT NULL - see backend/supabase/migrations/0013_proposals.sql).
    context = await build_context_for_user(
        user,
        supabase,
        conversation_id=str(conversation_id),
        active_document_id=active_document_id,
        statement_id=active_statement_id,
        language=payload.language,
    )

    history = await conversations_service.load_messages(supabase, conversation_id)

    service = AIService(supabase, provider=provider, embedding_provider=embedding_provider)
    try:
        turn = await service.handle_message(history, payload.message, context)
    except ProviderError as exc:
        raise AIProviderError() from exc

    new_messages = await _persist_turn(supabase, conversation_id, payload.message, turn)
    proposal = await _extract_proposal(supabase, user, new_messages)

    routing_chain = turn.routing_chain
    return ChatResponse(
        # EVERY hop that spoke, not just the last one (see combined_reply).
        # A compound question answered across a handoff - Insights takes the
        # spending half, Banking the balance half - must not lose the first
        # half here, when both halves are already being persisted below.
        reply=turn.combined_reply,
        conversation_id=conversation_id,
        routing_chain=routing_chain,
        # Backward-compatible duplicate of the last hop - see ChatResponse.
        routing=routing_chain[-1] if routing_chain else None,
        proposal=proposal,
    )


async def _persist_turn(
    supabase: AsyncClient,
    conversation_id: uuid.UUID,
    user_message: str,
    turn: TurnDispatchResult,
) -> list[Message]:
    """Store one turn: the user's message, then EACH agent hop separately.

    Per-hop, not per-turn (Step 15). Every hop writes its own trace followed by
    its own assistant row, and the routing decision goes on that hop's final
    assistant row and nowhere else - the same `is_final_reply` convention as
    before, now applied once per hop instead of once per turn. Ordering is what
    reconstructs the chain on replay: consecutive assistant rows where each
    one's `handoff_from` names the previous one's `agent_name` are one chain
    (see renderAgentChain in frontend/app.js).

    A hop's assistant row is written even when its `content` is empty, which is
    the normal case for a hop that handed off before saying anything: that row
    is where its routing_metadata lives, and dropping it would erase the first
    half of every chain from the stored history. The frontend already skips
    empty-content rows when drawing bubbles, so no blank bubble appears.

    The synthetic user message a handoff hands the target agent is deliberately
    NOT persisted. It is model-authored text, and storing it as a `user` row
    would put words in the user's mouth in their own transcript - and feed them
    back as real user input on the next turn. The handoff is already recorded,
    in the target hop's routing row (`reason` + `handoff_from`).

    Returns every message written, flat, for `_extract_proposal` to scan.
    """
    written: list[Message] = [Message(role="user", content=user_message)]
    await conversations_service.append_message(supabase, conversation_id, written[0])

    for hop in turn.hops:
        hop_messages = [*hop.trace, Message(role="assistant", content=hop.reply)]
        for message in hop_messages:
            is_final_reply = message is hop_messages[-1]
            await conversations_service.append_message(
                supabase,
                conversation_id,
                message,
                routing=hop.routing if is_final_reply else None,
            )
        written.extend(hop_messages)

    return written


async def _extract_proposal(
    supabase: AsyncClient, user: UserRead, new_messages: list[Message]
) -> ProposalRead | None:
    """If this turn called a propose_* tool and it succeeded, surface the
    proposal it created on the response (see ChatResponse.proposal) so the
    frontend can render a confirm/reject card without a second round trip.

    Scans the tool-result trace rather than threading a return value through
    handle_message/dispatch - same reasoning as `routing`: the trace is
    already the single source of truth for what happened this turn.

    NOTE: propose_card_order (see app/ai/tools/banking/propose_card_order.py)
    is NOT in PROPOSE_TOOL_NAMES and does not write a `proposals` row, so it
    never surfaces here - its result currently only reaches the user as
    prose in the reply. Follow-up: migrate it onto proposals_service so it
    gets the same confirm/reject card as the other propose_* tools."""
    for message in new_messages:
        if message.role != "tool" or message.name not in PROPOSE_TOOL_NAMES:
            continue
        content = json.loads(message.content or "{}")
        if not content.get("ok"):
            continue
        proposal_id = content["result"]["proposal_id"]
        proposal = await proposals_service.get_proposal(supabase, user, proposal_id)
        return ProposalRead.model_validate(proposal)
    return None


@router.get("/conversations", response_model=list[ConversationRead])
async def list_conversations(
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> list[ConversationRead]:
    return await conversations_service.list_conversations(supabase, user)


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: uuid.UUID,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> dict:
    await conversations_service.get_conversation(supabase, user, conversation_id)
    await conversations_service.delete_conversation(supabase, conversation_id)
    return {"status": "ok"}


@router.patch("/conversations/{conversation_id}")
async def rename_conversation(
    conversation_id: uuid.UUID,
    payload: ConversationRenameRequest,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> dict:
    await conversations_service.get_conversation(supabase, user, conversation_id)
    await conversations_service.rename_conversation(supabase, conversation_id, payload.title)
    return {"status": "ok"}


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageRead])
async def get_conversation_messages(
    conversation_id: uuid.UUID,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> list[MessageRead]:
    await conversations_service.get_conversation(supabase, user, conversation_id)
    return await conversations_service.load_messages_with_routing(supabase, conversation_id)


@router.post("/proposals/{proposal_id}/confirm", response_model=ProposalRead)
async def confirm_proposal(
    proposal_id: uuid.UUID,
    payload: ProposalConfirmRequest,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> ProposalRead:
    proposal = await proposals_service.confirm_proposal(
        supabase, user, str(proposal_id), payload.auth_method, payload.credential
    )
    return ProposalRead.model_validate(proposal)


@router.post("/proposals/{proposal_id}/reject", response_model=ProposalRead)
async def reject_proposal(
    proposal_id: uuid.UUID,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> ProposalRead:
    proposal = await proposals_service.reject_proposal(supabase, user, str(proposal_id))
    return ProposalRead.model_validate(proposal)
