"""e-Sign endpoints.

The actual cryptographic signing never happens here - creating a sign
request only inserts a `pending` proposal (see esign_service.
create_sign_request); confirming it, and therefore signing, happens through
the existing proposals flow (POST /chat/proposals/{id}/confirm - see
proposals_service._execute's "sign_document" branch), the same way a
transfer's execution isn't a /transfers endpoint either. What's here besides
that is what a signature needs after the fact: looking it up, listing a
document's signatures, and independently verifying one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from supabase import AsyncClient

from app.core.dependencies import get_current_user
from app.db.supabase_client import get_supabase
from app.modules.chat.schemas import ProposalRead
from app.modules.esign import service as esign_service
from app.modules.esign.schemas import (
    AdminDocumentConfirmRequest,
    SignatureRead,
    SignatureVerifyResponse,
    SignRequestCreate,
)
from app.modules.users.schemas import UserRead

router = APIRouter()


@router.post("/documents/{document_id}/sign-requests", response_model=ProposalRead)
async def create_sign_request(
    document_id: str,
    body: SignRequestCreate,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> ProposalRead:
    """Creates a pending `sign_document` proposal - same shape as a
    propose_* tool's result, so the frontend's existing generic proposal
    card (confirm with Face ID/password, or reject) handles this with no
    changes; see esign_service.create_sign_request's docstring for why this
    is a direct endpoint rather than an AI tool."""
    proposal = await esign_service.create_sign_request(supabase, user, document_id, body.intent)
    return ProposalRead.model_validate(proposal)


@router.post("/proposals/{proposal_id}/signing-code", status_code=204)
async def request_signing_code(
    proposal_id: str,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> None:
    """Only meaningful for a sign_document proposal whose document was
    issued by an admin (see esign_service._require_admin_issued_sign_
    proposal) - anything else 422s. Delivers the code out-of-band (Teams);
    204 either way, so the response never confirms whether a code exists."""
    await esign_service.request_signing_code(supabase, user, proposal_id)


@router.post("/proposals/{proposal_id}/confirm-admin-document", response_model=ProposalRead)
async def confirm_admin_document(
    proposal_id: str,
    body: AdminDocumentConfirmRequest,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> ProposalRead:
    """The stronger confirm path for admin-issued documents: OTP AND Face,
    both required - see esign_service.confirm_admin_document. Self-uploaded
    documents are NOT accepted here; they keep using
    POST /chat/proposals/{id}/confirm with Face-or-password."""
    proposal = await esign_service.confirm_admin_document(
        supabase, user, proposal_id, otp_code=body.otp_code, face_token=body.face_token
    )
    return ProposalRead.model_validate(proposal)


@router.get("/signatures/{signature_id}", response_model=SignatureRead)
async def get_signature(
    signature_id: str,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> SignatureRead:
    signature = await esign_service.get_signature(supabase, user, signature_id)
    return SignatureRead.model_validate(signature)


@router.get("/signatures/{signature_id}/verify", response_model=SignatureVerifyResponse)
async def verify_signature(
    signature_id: str,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> SignatureVerifyResponse:
    result = await esign_service.verify_signature(supabase, user, signature_id)
    return SignatureVerifyResponse(
        signature=SignatureRead.model_validate(result["signature"]),
        signature_valid=result["signature_valid"],
        document_unchanged=result["document_unchanged"],
    )


@router.get("/documents/{document_id}/signatures", response_model=list[SignatureRead])
async def list_signatures_for_document(
    document_id: str,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> list[SignatureRead]:
    signatures = await esign_service.list_signatures_for_document(supabase, user, document_id)
    return [SignatureRead.model_validate(row) for row in signatures]
