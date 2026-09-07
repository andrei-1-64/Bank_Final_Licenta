from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, StringConstraints


class SignRequestCreate(BaseModel):
    #: What the user is agreeing to by signing - shown back to them in the
    #: proposal card and folded into the canonical payload, so it becomes
    #: part of what the signature itself attests to (not just "this hash",
    #: but "this hash, for this stated purpose").
    intent: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]


class AdminDocumentConfirmRequest(BaseModel):
    """Body of POST /esign/proposals/{id}/confirm-admin-document - see
    esign_service.confirm_admin_document. Both fields are required and both
    are verified: this is the "OTP AND Face" path, not "either one"."""

    otp_code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=6, max_length=6)]
    face_token: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SignatureRead(BaseModel):
    id: str
    created_at: datetime
    document_id: str
    proposal_id: str
    key_id: str
    algorithm: str
    document_sha256: str
    signed_at: datetime
    auth_method: str
    intent: str
    signature_b64: str


class SignatureVerifyResponse(BaseModel):
    signature: SignatureRead
    #: The Ed25519 check over the stored canonical payload + stored public
    #: key for signature.key_id - independent of whether the document
    #: itself still matches (see document_unchanged below).
    signature_valid: bool
    #: Whether documents.content still hashes to signature.document_sha256.
    #: False means the stored document bytes changed after signing - the
    #: signature itself can still be mathematically valid (it signs the
    #: HASH recorded at signing time, not "whatever the row currently
    #: contains"), but it no longer vouches for the current document.
    document_unchanged: bool
