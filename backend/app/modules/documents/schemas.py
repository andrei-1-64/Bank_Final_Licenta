import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DocumentRead(BaseModel):
    """A document's metadata - never its content bytes or extracted text.

    See documents/service.py::get_document, which selects everything except
    `content` for exactly this reason: this shape is safe to hand back over
    HTTP without shipping a multi-MB payload on every read.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    mime_type: str
    size_bytes: int
    page_count: int
    created_at: datetime
    conversation_id: uuid.UUID | None


class DocumentUploadResponse(BaseModel):
    document: DocumentRead
    #: The conversation this document is attached to - echoed back explicitly
    #: because the caller may not have supplied one (see router.py::upload,
    #: which creates a conversation when none is given, mirroring
    #: chat/router.py's own POST /chat behaviour).
    conversation_id: uuid.UUID


class DocumentToSign(BaseModel):
    """One row of GET /documents/to-sign - admin-issued documents belonging
    to the caller (see documents/service.py::list_admin_issued_documents),
    each flagged with whether it has already been signed."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    page_count: int
    created_at: datetime
    signed: bool
