"""/admin - the admin panel's HTTP surface.

THE GATE: `require_admin` is declared once, on the router itself, so it
applies to every route in this file - including any added later. Putting it
per-endpoint would make "forgot the dependency on the new endpoint" a
one-line path to exposing every user's data, which is precisely the failure
this module cannot afford. Do not remove it from the router and re-add it
per route.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from supabase import AsyncClient

from app.core.dependencies import require_admin
from app.db.supabase_client import get_supabase
from app.modules.admin import service
from app.modules.admin.schemas import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    AdminCardOrderRead,
    AdminDocumentSendRequest,
    AdminDocumentSent,
    AdminIdentity,
    AdminStats,
    AdminTransaction,
    AdminUserDetail,
    AdminUserSummary,
    AuditLogEntry,
    CardOrderStatusUpdate,
    UserBlockUpdate,
    UserRoleUpdate,
)
from app.modules.card_orders.models import CardOrderStatus
from app.modules.documents.schemas import DocumentToSign
from app.modules.users.schemas import UserRead

router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("/me", response_model=AdminIdentity)
async def whoami(admin: UserRead = Depends(require_admin)) -> AdminIdentity:
    """Reaching this at all proves the caller is an admin - the frontend uses
    a 200-vs-403 here to decide whether to show the admin link."""
    return AdminIdentity(id=admin.id, email=admin.email, role="admin")


@router.get("/stats", response_model=AdminStats)
async def get_stats(supabase: AsyncClient = Depends(get_supabase)) -> AdminStats:
    return AdminStats.model_validate(await service.get_stats(supabase))


@router.get("/users", response_model=list[AdminUserSummary])
async def list_users(
    search: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    supabase: AsyncClient = Depends(get_supabase),
) -> list[dict]:
    return await service.list_users(supabase, search=search, limit=limit, offset=offset)


@router.get("/users/{user_id}", response_model=AdminUserDetail)
async def get_user(
    user_id: uuid.UUID,
    supabase: AsyncClient = Depends(get_supabase),
) -> dict:
    return await service.get_user_detail(supabase, user_id)


@router.patch("/users/{user_id}/role", response_model=AdminUserSummary)
async def set_user_role(
    user_id: uuid.UUID,
    payload: UserRoleUpdate,
    admin: UserRead = Depends(require_admin),
    supabase: AsyncClient = Depends(get_supabase),
) -> dict:
    return await service.set_user_role(supabase, admin, user_id, payload.role)


@router.patch("/users/{user_id}/blocked", response_model=AdminUserSummary)
async def set_user_blocked(
    user_id: uuid.UUID,
    payload: UserBlockUpdate,
    admin: UserRead = Depends(require_admin),
    supabase: AsyncClient = Depends(get_supabase),
) -> dict:
    return await service.set_user_blocked(supabase, admin, user_id, payload.blocked)


@router.post("/users/{user_id}/documents", response_model=AdminDocumentSent)
async def send_document(
    user_id: uuid.UUID,
    payload: AdminDocumentSendRequest,
    admin: UserRead = Depends(require_admin),
    supabase: AsyncClient = Depends(get_supabase),
) -> dict:
    """Generates a PDF from `payload` + the target user's own profile data
    and attaches it to a conversation of theirs - see
    service.generate_and_send_document. The user signs it themselves, later,
    from "Documente de semnat" (GET /documents/to-sign), through the OTP+Face
    confirm path (see app/modules/esign)."""
    return await service.generate_and_send_document(
        supabase, admin, user_id, title=payload.title, body=payload.body
    )


@router.get("/users/{user_id}/documents", response_model=list[DocumentToSign])
async def list_documents_for_user(
    user_id: uuid.UUID,
    supabase: AsyncClient = Depends(get_supabase),
) -> list[dict]:
    """Documents this admin (or another admin) has sent to the user, with
    signed status - "Documente trimise" in the user-detail panel."""
    return await service.list_documents_for_user(supabase, user_id)


@router.get("/documents/{document_id}/pdf")
async def get_document_pdf(
    document_id: uuid.UUID,
    supabase: AsyncClient = Depends(get_supabase),
) -> Response:
    """Raw PDF bytes for previewing a SENT document - admin-issued only,
    never a user's own upload (see service.get_admin_issued_document_pdf).
    Fetched as a blob by the frontend, same as the user-facing
    GET /documents/{id}/pdf - see previewDocumentPdf() in admin.js."""
    document = await service.get_admin_issued_document_pdf(supabase, document_id)
    return Response(content=document["content"], media_type="application/pdf")


@router.get("/users/{user_id}/transactions", response_model=list[AdminTransaction])
async def list_user_transactions(
    user_id: uuid.UUID,
    card_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    supabase: AsyncClient = Depends(get_supabase),
) -> list[dict]:
    return await service.list_user_transactions(
        supabase, user_id, card_id=card_id, limit=limit, offset=offset
    )


@router.get("/card-orders", response_model=list[AdminCardOrderRead])
async def list_card_orders(
    status: CardOrderStatus | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    supabase: AsyncClient = Depends(get_supabase),
) -> list[dict]:
    return await service.list_card_orders(
        supabase, status=status.value if status else None, limit=limit, offset=offset
    )


@router.patch("/card-orders/{order_id}", response_model=AdminCardOrderRead)
async def update_card_order_status(
    order_id: uuid.UUID,
    payload: CardOrderStatusUpdate,
    admin: UserRead = Depends(require_admin),
    supabase: AsyncClient = Depends(get_supabase),
) -> dict:
    return await service.update_card_order_status(
        supabase, admin, order_id, payload.status
    )


@router.get("/audit-log", response_model=list[AuditLogEntry])
async def list_audit_log(
    user_id: uuid.UUID | None = Query(default=None),
    action: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    supabase: AsyncClient = Depends(get_supabase),
) -> list[dict]:
    return await service.list_audit_log(
        supabase, user_id=user_id, action=action, limit=limit, offset=offset
    )
