"""Per-user document storage + ownership-checked reads.

`content` (the raw PDF bytes) is never selected except by
`get_document_with_content` - every other read excludes it explicitly, so a
multi-MB payload never rides along on a metadata fetch or a tool call. See
migrations/0014_documents.sql for the table shape.

Functions here take `user_id: str` rather than a `UserRead`, matching
`app.modules.chat.proposals_service.create_proposal`'s convention: the AI
tool layer only ever has a `Context` (which carries `user_id`, not a full
`UserRead`), so this keeps one ownership-check shape usable from both the
router and the `read_document` tool instead of needing two.
"""

from __future__ import annotations

from supabase import AsyncClient

from app.core.exceptions import NotFoundError

#: Every column except `content` - the raw PDF bytes are never returned by a
#: normal read. See get_document_with_content for the one exception.
_METADATA_AND_TEXT_COLUMNS = (
    "id, created_at, user_id, conversation_id, filename, mime_type, "
    "size_bytes, extracted_text, page_count, issued_by_admin_id"
)


async def create_document(
    supabase: AsyncClient,
    *,
    user_id: str,
    conversation_id: str | None,
    filename: str,
    mime_type: str,
    content: bytes,
    extracted_text: str,
    page_count: int,
    issued_by_admin_id: str | None = None,
) -> dict:
    inserted = (
        await supabase.table("documents")
        .insert(
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": len(content),
                # PostgREST hands a JSON string straight to Postgres's bytea
                # input parser, which expects the `\x`-prefixed hex format
                # (the "escape" format isn't JSON-safe) - see
                # get_document_with_content for the matching decode.
                "content": "\\x" + content.hex(),
                "extracted_text": extracted_text,
                "page_count": page_count,
                # None (the default) means "the user uploaded this
                # themselves" - see 0019_admin_documents.sql.
                "issued_by_admin_id": issued_by_admin_id,
            }
        )
        .execute()
    )
    document_id = inserted.data[0]["id"]

    # Re-select without `content` rather than trusting insert()'s echoed row -
    # PostgREST returns every column by default, and the whole point here is
    # that the multi-MB bytea never rides along past this function.
    resp = (
        await supabase.table("documents")
        .select(_METADATA_AND_TEXT_COLUMNS)
        .eq("id", document_id)
        .maybe_single()
        .execute()
    )
    return resp.data


async def get_document(supabase: AsyncClient, user_id: str, document_id: str) -> dict:
    """Ownership-checked read, without content bytes. Includes extracted_text,
    since this is what `ReadDocumentTool` calls to get the model-facing text.

    A missing id and one owned by someone else look identical to the caller -
    NotFoundError either way, same reasoning as conversations_service and
    proposals_service."""
    resp = (
        await supabase.table("documents")
        .select(_METADATA_AND_TEXT_COLUMNS)
        .eq("id", document_id)
        .maybe_single()
        .execute()
    )
    document = resp.data if resp is not None else None
    if document is None or document["user_id"] != user_id:
        raise NotFoundError("Document not found.")
    return document


async def get_document_with_content(
    supabase: AsyncClient, user_id: str, document_id: str
) -> dict:
    """Same ownership check as get_document, but includes the raw PDF bytes.

    Not called anywhere in Step 12 - kept for a future "serve the original
    file back" endpoint, so that need doesn't require re-deriving the
    ownership check."""
    resp = (
        await supabase.table("documents")
        .select("*")
        .eq("id", document_id)
        .maybe_single()
        .execute()
    )
    document = resp.data if resp is not None else None
    if document is None or document["user_id"] != user_id:
        raise NotFoundError("Document not found.")
    raw = document.get("content")
    if isinstance(raw, str):
        document["content"] = bytes.fromhex(raw.removeprefix("\\x"))
    return document


async def list_documents_for_conversation(
    supabase: AsyncClient, user_id: str, conversation_id: str
) -> list[dict]:
    """Ownership-checked list - only documents this user owns, scoped to one
    conversation. No content bytes, no extracted_text (metadata only, same
    shape DocumentRead expects)."""
    resp = (
        await supabase.table("documents")
        .select(_METADATA_AND_TEXT_COLUMNS)
        .eq("conversation_id", conversation_id)
        .eq("user_id", user_id)
        .order("created_at")
        .execute()
    )
    return resp.data


async def list_admin_issued_documents(supabase: AsyncClient, user_id: str) -> list[dict]:
    """Documents an admin generated and sent to this user (see
    admin/service.py::generate_and_send_document), each flagged with whether
    it already has a signature - GET /documents/to-sign's "Documente de
    semnat" list is exactly this.

    Two queries rather than a PostgREST embed: `signatures` has no FK back
    to `documents` that PostgREST would resolve as a to-many embed cleanly
    for an "exists" check, and the document count here is always small (one
    user's admin-issued documents), so N+1 isn't a concern - same reasoning
    as admin/service.py::get_user_detail's per-account balance loop.
    """
    resp = (
        await supabase.table("documents")
        .select("id, filename, page_count, created_at")
        .eq("user_id", user_id)
        .not_.is_("issued_by_admin_id", "null")
        .order("created_at", desc=True)
        .execute()
    )
    documents = resp.data or []
    if not documents:
        return []

    signed_resp = (
        await supabase.table("signatures")
        .select("document_id")
        .in_("document_id", [doc["id"] for doc in documents])
        .execute()
    )
    signed_document_ids = {row["document_id"] for row in (signed_resp.data or [])}

    for document in documents:
        document["signed"] = document["id"] in signed_document_ids
    return documents
