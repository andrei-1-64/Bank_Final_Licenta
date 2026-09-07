import uuid

from app.ai.routing import RoutingDecision
from app.ai.schemas import Message, ToolCall
from app.core.exceptions import ConversationNotFoundError
from app.modules.chat.schemas import MessageRead
from app.modules.users.schemas import UserRead
from supabase import AsyncClient


async def create_conversation(supabase: AsyncClient, user: UserRead) -> dict:
    resp = (
        await supabase.table("conversations")
        .insert({"user_id": str(user.id)})
        .execute()
    )
    return resp.data[0]


async def get_conversation(
    supabase: AsyncClient, user: UserRead, conversation_id: uuid.UUID
) -> dict:
    """Ownership-checked conversation read. Same shape as accounts.get_account_for_owner:
    a missing id and one owned by someone else look identical to the caller."""
    resp = (
        await supabase.table("conversations")
        .select("*")
        .eq("id", str(conversation_id))
        .eq("user_id", str(user.id))
        .maybe_single()
        .execute()
    )
    conversation = resp.data if resp is not None else None
    if conversation is None:
        raise ConversationNotFoundError()
    return conversation


async def list_conversations(supabase: AsyncClient, user: UserRead) -> list[dict]:
    resp = (
        await supabase.table("conversations")
        .select("*")
        .eq("user_id", str(user.id))
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data


async def append_message(
    supabase: AsyncClient,
    conversation_id: uuid.UUID,
    message: Message,
    routing: RoutingDecision | None = None,
) -> dict:
    """Store one turn. `routing` belongs on the assistant turn the routed agent
    produced, and is None everywhere else (user turns, tool results)."""
    resp = (
        await supabase.table("messages")
        .insert(
            {
                "conversation_id": str(conversation_id),
                "role": message.role,
                "content": message.content,
                "tool_calls": [tc.model_dump() for tc in message.tool_calls] or None,
                "tool_call_id": message.tool_call_id,
                "name": message.name,
                "routing_metadata": routing.model_dump() if routing is not None else None,
            }
        )
        .execute()
    )
    return resp.data[0]


async def load_messages(supabase: AsyncClient, conversation_id: uuid.UUID) -> list[Message]:
    resp = (
        await supabase.table("messages")
        .select("*")
        .eq("conversation_id", str(conversation_id))
        .order("created_at")
        .execute()
    )
    return [
        Message(
            role=row["role"],
            content=row["content"],
            tool_calls=[ToolCall(**tc) for tc in (row["tool_calls"] or [])],
            tool_call_id=row["tool_call_id"],
            name=row["name"],
        )
        for row in resp.data
    ]


async def load_messages_with_routing(
    supabase: AsyncClient, conversation_id: uuid.UUID
) -> list[MessageRead]:
    """Same rows as `load_messages`, but read into `MessageRead` - the HTTP
    read model - so the history endpoint can surface `routing_metadata`
    without widening the AI-layer's `Message` type (see MessageRead's
    docstring)."""
    resp = (
        await supabase.table("messages")
        .select("*")
        .eq("conversation_id", str(conversation_id))
        .order("created_at")
        .execute()
    )
    return [
        MessageRead(
            role=row["role"],
            content=row["content"],
            tool_calls=[ToolCall(**tc) for tc in (row["tool_calls"] or [])],
            tool_call_id=row["tool_call_id"],
            name=row["name"],
            routing=RoutingDecision(**row["routing_metadata"])
            if row["routing_metadata"]
            else None,
        )
        for row in resp.data
    ]


async def delete_conversation(supabase: AsyncClient, conversation_id: uuid.UUID) -> None:
    """Delete a conversation and all its messages.

    NOT ownership-checked - the caller must call `get_conversation` first (see
    chat/router.py::delete_conversation), otherwise any id would be deletable.
    The `messages` rows go with it via the FK's ON DELETE CASCADE (see
    migrations/0006_conversations_and_messages.sql) - nothing here deletes them.
    """
    await supabase.table("conversations").delete().eq("id", str(conversation_id)).execute()


async def rename_conversation(supabase: AsyncClient, conversation_id: uuid.UUID, title: str) -> None:
    """Rename a conversation.

    NOT ownership-checked, same as `delete_conversation` above - the caller
    must call `get_conversation` first (see chat/router.py::rename_conversation).
    """
    await supabase.table("conversations").update({"title": title}).eq("id", str(conversation_id)).execute()
