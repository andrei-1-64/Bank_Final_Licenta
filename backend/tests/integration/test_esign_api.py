"""POST /api/v1/esign/documents/{id}/sign-requests + the read/verify
endpoints, and the "sign_document" branch of proposals_service.confirm_proposal.

Documents are seeded directly against the `documents` table (same reasoning
as test_proposals_confirm.py seeding `proposals` directly) - real upload/OCR
is already covered by test_documents_api.py, so these tests can focus on the
e-Sign-specific behaviour: the pending-proposal handoff, the hash re-check at
sign time, and independent verification.
"""

from __future__ import annotations

import base64
import hashlib

import pymupdf


def _build_pdf_bytes(text: str = "Contract de test.") -> bytes:
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), text)
    pdf_bytes = document.tobytes()
    document.close()
    return pdf_bytes


async def _seed_document(
    supabase, user, conversation, *, text: str = "Contract de test.", admin_issued: bool = True
) -> dict:
    """`admin_issued=True` (the default) is what every signable document
    looks like: only documents the bank issued TO a user can be signed (see
    esign_service.create_sign_request). The admin's id is seeded as the
    user's own here - a shortcut, since these tests only need the column to
    be non-null, and who issued it is covered by
    test_admin_document_signing_api.py instead.

    Pass `admin_issued=False` for a self-uploaded document, i.e. the case
    signing must refuse."""
    content = _build_pdf_bytes(text)
    resp = (
        await supabase.table("documents")
        .insert(
            {
                "user_id": str(user.id),
                "conversation_id": conversation["id"],
                "filename": "contract.pdf",
                "mime_type": "application/pdf",
                "size_bytes": len(content),
                "content": "\\x" + content.hex(),
                "extracted_text": text,
                "page_count": 1,
                "issued_by_admin_id": str(user.id) if admin_issued else None,
            }
        )
        .execute()
    )
    return resp.data[0]


async def _row(supabase, table, row_id, select="*"):
    resp = await supabase.table(table).select(select).eq("id", row_id).maybe_single().execute()
    return resp.data


async def test_create_sign_request_creates_pending_proposal(
    authed_client, supabase, conversation_factory
):
    client, user = authed_client
    conversation = await conversation_factory(user)
    document = await _seed_document(supabase, user, conversation)

    resp = await client.post(
        f"/api/v1/esign/documents/{document['id']}/sign-requests",
        json={"intent": "Sunt de acord cu termenii."},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["proposal_type"] == "sign_document"
    assert body["payload"]["document_id"] == document["id"]
    assert body["payload"]["intent"] == "Sunt de acord cu termenii."
    # The hash captured at request time - re-checked at confirm time (see
    # test_confirm_rejects_a_document_that_changed_since_the_request below).
    assert body["payload"]["document_sha256"] == hashlib.sha256(
        bytes.fromhex(document["content"].removeprefix("\\x"))
    ).hexdigest()


async def test_create_sign_request_for_a_self_uploaded_document_is_refused(
    authed_client, supabase, conversation_factory
):
    """A document the user uploaded to the chat themselves is reference
    material for the AI to read, not something to sign - there is no
    counterparty and nothing was agreed. Only what the bank issues TO them
    is signable."""
    client, user = authed_client
    conversation = await conversation_factory(user)
    document = await _seed_document(supabase, user, conversation, admin_issued=False)

    resp = await client.post(
        f"/api/v1/esign/documents/{document['id']}/sign-requests",
        json={"intent": "Sunt de acord cu termenii."},
    )
    assert resp.status_code == 422, resp.text

    # No proposal was created for it either - the refusal happens before any
    # state is written, not after.
    proposals = (
        await supabase.table("proposals").select("id").eq("user_id", str(user.id)).execute()
    ).data
    assert proposals == []


async def test_create_sign_request_for_someone_elses_document_returns_404(
    authed_client, authed_client_factory, supabase, conversation_factory
):
    owner_client, owner = authed_client
    conversation = await conversation_factory(owner)
    document = await _seed_document(supabase, owner, conversation)

    other_client, _other_user = await authed_client_factory()

    resp = await other_client.post(
        f"/api/v1/esign/documents/{document['id']}/sign-requests",
        json={"intent": "Sunt de acord."},
    )
    assert resp.status_code == 404, resp.text


async def test_confirm_creates_a_verifiable_signature(
    authed_client, supabase, conversation_factory
):
    client, user = authed_client
    conversation = await conversation_factory(user)
    document = await _seed_document(supabase, user, conversation)

    create_resp = await client.post(
        f"/api/v1/esign/documents/{document['id']}/sign-requests",
        json={"intent": "Sunt de acord cu termenii."},
    )
    proposal_id = create_resp.json()["id"]

    confirm_resp = await client.post(
        f"/api/v1/chat/proposals/{proposal_id}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    assert confirm_resp.status_code == 200, confirm_resp.text
    assert confirm_resp.json()["status"] == "confirmed"

    # proposal_id is UNIQUE on signatures, and confirm_resp doesn't echo the
    # signature's own id - look it up by the one thing we already know.
    resp = (
        await supabase.table("signatures")
        .select("*")
        .eq("proposal_id", proposal_id)
        .maybe_single()
        .execute()
    )
    signature_row = resp.data
    assert signature_row is not None
    assert signature_row["document_id"] == document["id"]
    assert signature_row["auth_method"] == "password"

    list_resp = await client.get(f"/api/v1/esign/documents/{document['id']}/signatures")
    assert list_resp.status_code == 200, list_resp.text
    signatures = list_resp.json()
    assert len(signatures) == 1
    assert signatures[0]["id"] == signature_row["id"]

    verify_resp = await client.get(f"/api/v1/esign/signatures/{signature_row['id']}/verify")
    assert verify_resp.status_code == 200, verify_resp.text
    verify_body = verify_resp.json()
    assert verify_body["signature_valid"] is True
    assert verify_body["document_unchanged"] is True


async def test_confirm_rejects_a_document_that_changed_since_the_request(
    authed_client, supabase, conversation_factory
):
    """THE GUARDRAIL: the document is re-hashed at confirm time, not trusted
    from the proposal's payload - if the bytes changed in between, signing
    must refuse rather than sign different content than what was requested."""
    client, user = authed_client
    conversation = await conversation_factory(user)
    document = await _seed_document(supabase, user, conversation)

    create_resp = await client.post(
        f"/api/v1/esign/documents/{document['id']}/sign-requests",
        json={"intent": "Sunt de acord cu termenii."},
    )
    proposal_id = create_resp.json()["id"]

    tampered_content = _build_pdf_bytes("Continut modificat.")
    await (
        supabase.table("documents")
        .update({"content": "\\x" + tampered_content.hex()})
        .eq("id", document["id"])
        .execute()
    )

    confirm_resp = await client.post(
        f"/api/v1/chat/proposals/{proposal_id}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    assert confirm_resp.status_code == 422, confirm_resp.text
    assert confirm_resp.json()["error"]["code"] == "validation_error"

    resp = (
        await supabase.table("signatures")
        .select("id")
        .eq("proposal_id", proposal_id)
        .maybe_single()
        .execute()
    )
    # .maybe_single() returns None itself (not a response with .data=None)
    # when nothing matches - same guard as service.py's ownership checks.
    assert resp is None or resp.data is None

    # The proposal stays pending - a validation failure inside _execute is
    # not a step-up auth failure, so nothing in confirm_proposal marks it
    # confirmed or expired; the same shape as any other _execute error.
    row = await _row(supabase, "proposals", proposal_id, select="status")
    assert row["status"] == "pending"


async def test_list_signatures_for_someone_elses_document_returns_404(
    authed_client, authed_client_factory, supabase, conversation_factory
):
    owner_client, owner = authed_client
    conversation = await conversation_factory(owner)
    document = await _seed_document(supabase, owner, conversation)

    other_client, _other_user = await authed_client_factory()

    resp = await other_client.get(f"/api/v1/esign/documents/{document['id']}/signatures")
    assert resp.status_code == 404, resp.text


async def test_verify_someone_elses_signature_returns_404(
    authed_client, authed_client_factory, supabase, conversation_factory
):
    owner_client, owner = authed_client
    conversation = await conversation_factory(owner)
    document = await _seed_document(supabase, owner, conversation)

    create_resp = await owner_client.post(
        f"/api/v1/esign/documents/{document['id']}/sign-requests",
        json={"intent": "Sunt de acord."},
    )
    proposal_id = create_resp.json()["id"]
    await owner_client.post(
        f"/api/v1/chat/proposals/{proposal_id}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    resp = (
        await supabase.table("signatures")
        .select("id")
        .eq("proposal_id", proposal_id)
        .maybe_single()
        .execute()
    )
    signature_id = resp.data["id"]

    other_client, _other_user = await authed_client_factory()
    verify_resp = await other_client.get(f"/api/v1/esign/signatures/{signature_id}/verify")
    assert verify_resp.status_code == 404, verify_resp.text
