"""POST /statements/upload - bank statement PDF upload + extraction (Step 13).

Mirrors documents/router.py's upload endpoint (size/type/magic-byte checks,
conversation resolution) with one difference: text extraction is
STRUCTURED (statement_extractor.extract_statement, Azure Document
Intelligence) rather than plain-text (pymupdf) - the AI needs rows to run
insights over, not prose.

Rows are EXTRACTED, UNVERIFIED, and never written to the ledger - see
statements/service.py's module docstring. The upload response says so
explicitly (StatementUploadResponse.note) so the frontend can surface that
framing to the user.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.config import ConfigurationError, Settings, get_settings
from app.core.dependencies import get_current_user
from app.core.exceptions import AIServiceUnavailableError, ValidationError
from app.db.supabase_client import get_supabase
from app.modules.chat import conversations_service
from app.modules.documents import service as documents_service
from app.modules.documents.extractor import (
    ALLOWED_MIME_TYPES,
    MAX_FILE_SIZE_BYTES,
    validate_pdf_magic_bytes,
)
from app.modules.documents.statement_extractor import extract_statement
from app.modules.statements import service as statements_service
from app.modules.statements.schemas import (
    StatementDetail,
    StatementSummary,
    StatementUploadResponse,
)
from app.modules.users.schemas import UserRead
from supabase import AsyncClient

router = APIRouter()

_UNVERIFIED_NOTE = (
    "Rândurile din acest extras sunt extrase automat și NEVERIFICATE - pot "
    "conține erori de citire. Nu sunt scrise în jurnalul contabil."
)


@router.post("/upload", response_model=StatementUploadResponse)
async def upload_statement(
    file: UploadFile = File(...),
    conversation_id: str | None = Form(default=None),
    document_id: str | None = Form(default=None),
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> StatementUploadResponse:
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

    resolved_document_id = None
    if document_id is not None:
        # Ownership-checked the same way, for an optional link back to an
        # already-uploaded document (e.g. the same PDF read via
        # read_document too).
        document = await documents_service.get_document(supabase, str(user.id), document_id)
        resolved_document_id = document["id"]

    try:
        di_config = settings.require_document_intelligence()
    except ConfigurationError as exc:
        raise AIServiceUnavailableError() from exc

    extracted = await extract_statement(content, di_config)

    statement = await statements_service.create_statement(
        supabase,
        user_id=str(user.id),
        conversation_id=resolved_conversation_id,
        document_id=resolved_document_id,
        bank_name=extracted.bank_name,
        period_start=extracted.period_start.isoformat() if extracted.period_start else None,
        period_end=extracted.period_end.isoformat() if extracted.period_end else None,
        currency=extracted.currency,
        opening_balance=extracted.opening_balance,
        closing_balance=extracted.closing_balance,
        rows=[
            {
                "posted_date": row.posted_date.isoformat() if row.posted_date else None,
                "description": row.description,
                "amount": row.amount,
                "currency": extracted.currency,
                "balance_after": row.balance_after,
                "row_index": row.row_index,
            }
            for row in extracted.rows
        ],
    )

    return StatementUploadResponse(
        statement=StatementSummary.model_validate(statement),
        conversation_id=resolved_conversation_id,
        row_count=len(extracted.rows),
        note=_UNVERIFIED_NOTE,
    )


@router.get("", response_model=list[StatementSummary])
async def list_statements(
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> list[StatementSummary]:
    statements = await statements_service.list_statements(supabase, str(user.id))
    return [StatementSummary.model_validate(s) for s in statements]


@router.get("/{statement_id}", response_model=StatementDetail)
async def get_statement_detail(
    statement_id: str,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> StatementDetail:
    statement = await statements_service.get_statement_with_rows(
        supabase, str(user.id), statement_id
    )
    return StatementDetail.model_validate(statement)


@router.delete("/{statement_id}")
async def delete_statement(
    statement_id: str,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> dict:
    await statements_service.soft_delete_statement(supabase, str(user.id), statement_id)
    return {"status": "ok"}
