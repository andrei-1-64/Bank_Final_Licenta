import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.ai.routing import RoutingDecision
from app.ai.schemas import Role, ToolCall

#: Roughly a long paragraph. Bounds the prompt the model is asked to process.
MAX_MESSAGE_CHARS = 4000


class ChatRequest(BaseModel):
    # `strip_whitespace` before `min_length` so a whitespace-only message is a
    # 422 like an empty one, rather than an empty prompt reaching the model.
    message: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_CHARS),
    ]
    #: Null starts a new conversation. Otherwise the caller is continuing one
    #: it was handed by an earlier response - ownership is checked server-side.
    conversation_id: uuid.UUID | None = None
    #: Set by the frontend after a successful /documents/upload, to route this
    #: turn to DocumentAgent (see app/ai/orchestrator.py's context-first
    #: check). Ownership is verified server-side in router.py before this
    #: ever reaches Context - never trusted as-is, same as conversation_id.
    document_id: uuid.UUID | None = None
    #: Set by the frontend after a successful /statements/upload, to route
    #: this turn to DocumentAgent (see app/ai/orchestrator.py's context-first
    #: check). Ownership is verified server-side in router.py before this
    #: ever reaches Context, same as document_id above. Unlike document_id,
    #: an omitted statement_id does NOT mean "no statement active" - see
    #: app/ai/context.py's Context.statement_id docstring for the implicit
    #: fallback (last statement uploaded in this conversation stays active).
    statement_id: uuid.UUID | None = None
    #: The caller's active UI language (see frontend/language.js's LANGUAGES
    #: map), read straight off `document.documentElement.lang` client-side.
    #: Threaded onto `Context.language` (see app/ai/context.py) so the
    #: agent's reply follows it - never validated against a fixed list here,
    #: since an unrecognized code degrades harmlessly to Romanian (see
    #: app/ai/language_directive.py) rather than being worth a 422.
    language: str = "ro"


class ProposalRead(BaseModel):
    """An AI-proposed write action, pending (or past) human confirmation.

    Mirrors the `proposals` table row - see
    backend/supabase/migrations/0013_proposals.sql and
    app/modules/chat/proposals_service.py.
    """

    id: str
    status: str
    proposal_type: str
    payload: dict[str, Any]
    summary: str
    created_at: datetime


class ProposalConfirmRequest(BaseModel):
    """Step-up auth proof, verified server-side before a proposal executes.

    `credential` is either a face-confirmation token (see
    face_auth_service.create_face_confirmation) or a plaintext password -
    never logged, never stored (see proposals_service.confirm_proposal).
    """

    auth_method: Literal["face", "password"]
    credential: str


class ChatResponse(BaseModel):
    reply: str
    #: Always returned, so the client knows what to send on the next turn -
    #: history itself now lives server-side (see conversations_service).
    conversation_id: uuid.UUID
    #: EVERY agent that ran this turn, in execution order (Step 15). One entry
    #: for an ordinary turn; two when an agent handed off mid-turn, and the
    #: second entry's `handoff_from` names the first. The frontend renders this
    #: as a chain ("-> Analiza -> Bancar"); see renderAgentChain in
    #: frontend/app.js. Never empty for a successful turn.
    routing_chain: list[RoutingDecision] = Field(default_factory=list)
    #: The LAST decision in `routing_chain`, duplicated here so a client
    #: written before Step 15 keeps working unchanged. The last one rather than
    #: the first deliberately: it is the agent that produced `reply`, i.e. what
    #: a single-agent client would have wanted to label the reply with anyway.
    #: None only when no agent ran at all.
    routing: RoutingDecision | None = None
    #: Set only when this turn's agent called a propose_* tool (see
    #: app/ai/tools/propose_tools.py) - same "optional, additive" pattern as
    #: `routing`. None means no action was proposed this turn. NOTE:
    #: propose_card_order (a separate, older propose-only tool - see
    #: app/ai/tools/banking/propose_card_order.py) does NOT write into the
    #: `proposals` table and so never populates this field; its result
    #: currently only reaches the user as prose in the reply, not as a
    #: confirm/reject card. Follow-up: migrate it onto proposals_service.
    proposal: ProposalRead | None = None


class MessageRead(BaseModel):
    """One stored turn, read back for the conversation-history endpoint.

    Deliberately separate from app.ai.schemas.Message: that type is the
    provider-agnostic transcript shape threaded through the agent loop, and
    must NOT carry routing (see test_tool_result_and_message_types_are_
    untouched_by_routing) - routing rides alongside the transcript, not
    inside it. This is the HTTP read model, for the history GET only.
    """

    role: Role
    content: str | None = None
    tool_calls: list[ToolCall]
    tool_call_id: str | None = None
    name: str | None = None
    #: Set only on the assistant turn a routed agent produced; None on every
    #: other row (user turns, tool results) and on messages predating Step 7.
    routing: RoutingDecision | None = None


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    created_at: datetime


class ConversationRenameRequest(BaseModel):
    # Matches the `title VARCHAR(200)` column (migrations/0006).
    title: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]