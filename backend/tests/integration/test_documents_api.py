"""POST /api/v1/documents/upload.

Real PDFs are built with pymupdf itself (the same library extractor.py uses),
so these tests exercise the actual extraction path rather than mocking it -
matching test_id_ocr_api.py's approach of rendering real inputs instead of
stubbing the library that reads them.

There is no GET /documents/{id} (or list) endpoint - Step 12 only needed
upload, since reads happen through ReadDocumentTool (see
tests/ai/test_document_agent.py) and through /chat's document_id (see
test_chat_with_document.py). The "never leaks content bytes" and
"ownership-checked" properties are therefore verified against the surface
that actually exists: the upload response itself, and the conversation_id
ownership check upload already has to do.
"""

from __future__ import annotations

import pymupdf

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024


def _build_pdf_bytes(text: str = "Chiria lunara este 500 EUR.") -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    pdf_bytes = document.tobytes()
    document.close()
    return pdf_bytes


async def test_upload_requires_authentication(client):
    resp = await client.post(
        "/api/v1/documents/upload",
        files={"file": ("contract.pdf", _build_pdf_bytes(), "application/pdf")},
    )
    assert resp.status_code == 401


async def test_upload_pdf_succeeds_and_creates_a_conversation(authed_client):
    client, _user = authed_client
    pdf_bytes = _build_pdf_bytes()

    resp = await client.post(
        "/api/v1/documents/upload",
        files={"file": ("contract.pdf", pdf_bytes, "application/pdf")},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    document = body["document"]
    assert document["filename"] == "contract.pdf"
    assert document["mime_type"] == "application/pdf"
    assert document["size_bytes"] == len(pdf_bytes)
    assert document["page_count"] == 1
    assert body["conversation_id"] == document["conversation_id"]

    # No conversation_id was supplied, so upload created a new one.
    conversations = await client.get("/api/v1/chat/conversations")
    assert body["conversation_id"] in [c["id"] for c in conversations.json()]


async def test_upload_attaches_to_a_conversation_the_caller_owns(
    authed_client, conversation_factory
):
    client, user = authed_client
    conversation = await conversation_factory(user)

    resp = await client.post(
        "/api/v1/documents/upload",
        files={"file": ("contract.pdf", _build_pdf_bytes(), "application/pdf")},
        data={"conversation_id": conversation["id"]},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["conversation_id"] == conversation["id"]


async def test_upload_rejects_a_conversation_the_caller_does_not_own(
    authed_client_factory, conversation_factory
):
    alice_client, _alice = await authed_client_factory()
    _bob_client, bob = await authed_client_factory()
    bob_conversation = await conversation_factory(bob)

    resp = await alice_client.post(
        "/api/v1/documents/upload",
        files={"file": ("contract.pdf", _build_pdf_bytes(), "application/pdf")},
        data={"conversation_id": bob_conversation["id"]},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "conversation_not_found"


async def test_upload_rejects_file_over_5mb(authed_client):
    client, _user = authed_client
    oversized = b"%PDF-1.4\n" + b"0" * (MAX_FILE_SIZE_BYTES + 1)

    resp = await client.post(
        "/api/v1/documents/upload",
        files={"file": ("huge.pdf", oversized, "application/pdf")},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_upload_rejects_non_pdf_content_type(authed_client):
    client, _user = authed_client

    resp = await client.post(
        "/api/v1/documents/upload",
        files={"file": ("contract.txt", b"just some text", "text/plain")},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_upload_rejects_pdf_content_type_with_wrong_magic_bytes(authed_client):
    """A spoofed Content-Type is not enough - the actual file signature is
    checked too (see extractor.validate_pdf_magic_bytes)."""
    client, _user = authed_client

    resp = await client.post(
        "/api/v1/documents/upload",
        files={"file": ("fake.pdf", b"this is not a pdf file at all", "application/pdf")},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_upload_rejects_a_corrupt_pdf(authed_client):
    """Correct magic bytes, but pymupdf still can't parse the rest of it."""
    client, _user = authed_client
    corrupt = b"%PDF-1.4\n" + b"this is garbage, not a real xref table"

    resp = await client.post(
        "/api/v1/documents/upload",
        files={"file": ("corrupt.pdf", corrupt, "application/pdf")},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_upload_response_never_includes_content_bytes_or_extracted_text(
    authed_client,
):
    """DocumentRead only ever carries metadata - the multi-MB bytea column
    and the (potentially large, and untrusted-for-logging) extracted text
    must never ride along on the upload response."""
    client, _user = authed_client

    resp = await client.post(
        "/api/v1/documents/upload",
        files={"file": ("contract.pdf", _build_pdf_bytes(), "application/pdf")},
    )

    document = resp.json()["document"]
    assert "content" not in document
    assert "extracted_text" not in document
