"""POST /documents/upload - PDF upload for the DocumentAgent (Step 12).

Size and type are checked BEFORE the bytes ever reach pymupdf: length first
(cheapest), then the Content-Type header, then the actual file signature
(magic bytes) - a spoofed Content-Type is not enough to get an arbitrary
file into the extractor. See extractor.py for both checks.

Ownership of `conversation_id` (when supplied) is verified the same way
chat/router.py verifies it for /chat: a foreign or nonexistent id raises
ConversationNotFoundError, never silently attaching to someone else's
conversation. When no conversation_id is given, a new conversation is
created - mirroring exactly what POST /chat already does when its own
conversation_id is None, so a document can be the first thing a user does
in a conversation, not only a follow-up to an existing one.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response
from supabase import AsyncClient

from app.core.dependencies import get_current_user
from app.core.exceptions import ValidationError
from app.db.supabase_client import get_supabase
from app.modules.chat import conversations_service
from app.modules.documents import service as documents_service
from app.modules.documents.extractor import (
    ALLOWED_MIME_TYPES,
    MAX_FILE_SIZE_BYTES,
    extract_pdf_text,
    validate_pdf_magic_bytes,
)
from app.modules.documents.schemas import DocumentRead, DocumentToSign, DocumentUploadResponse
from app.modules.users.schemas import UserRead

router = APIRouter()


@router.get("/to-sign", response_model=list[DocumentToSign])
async def list_documents_to_sign(
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> list[DocumentToSign]:
    """Backs the "Documente de semnat" profile section - admin-issued
    documents belonging to the caller, signed or not."""
    documents = await documents_service.list_admin_issued_documents(supabase, str(user.id))
    return [DocumentToSign.model_validate(doc) for doc in documents]


@router.get("/{document_id}/pdf")
async def get_document_pdf(
    document_id: str,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> Response:
    """Raw PDF bytes for previewing a document - own upload or admin-issued,
    same ownership check either way (documents_service.get_document_with_
    content). The frontend fetches this as a blob (credentials included,
    not a plain navigable link - the API is a different origin from the
    frontend) and opens it as an object URL; see previewDocumentPdf() in
    app.js/admin.js."""
    document = await documents_service.get_document_with_content(
        supabase, str(user.id), document_id
    )
    return Response(content=document["content"], media_type="application/pdf")


@router.post("/upload", response_model=DocumentUploadResponse)
async def upload_document(
    file: UploadFile = File(...),
    conversation_id: str | None = Form(default=None),
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> DocumentUploadResponse:
    if file.content_type not in ALLOWED_MIME_TYPES:
        raise ValidationError("Doar fișiere PDF sunt acceptate.")

    content = await file.read()
    if not content:
        raise ValidationError("Fișierul este gol.")
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise ValidationError("Fișierul depășește 5 MB.")
    validate_pdf_magic_bytes(content)

    if conversation_id is None:
        conversation = await conversations_service.create_conversation(supabase, user)
    else:
        # Ownership-checked: raises ConversationNotFoundError for a foreign
        # or nonexistent id, never leaking which one it was.
        conversation = await conversations_service.get_conversation(
            supabase, user, uuid.UUID(conversation_id)
        )
    resolved_conversation_id = conversation["id"]

    extracted_text, page_count = await extract_pdf_text(content)

    document = await documents_service.create_document(
        supabase,
        user_id=str(user.id),
        conversation_id=resolved_conversation_id,
        filename=file.filename or "document.pdf",
        mime_type=file.content_type,
        content=content,
        extracted_text=extracted_text,
        page_count=page_count,
    )

    # Metadata only - never logs filename+size together with extracted_text,
    # and never logs extracted_text or content at all.
    return DocumentUploadResponse(
        document=DocumentRead.model_validate(document),
        conversation_id=resolved_conversation_id,
    )
