"""POST /api/v1/admin/users/{id}/documents (admin generates + sends a
document) and the OTP+Face confirm path for signing it
(POST /api/v1/esign/proposals/{id}/signing-code and .../confirm-admin-document).

Face auth is mocked at the module boundary the same way
test_proposals_confirm.py mocks it - these tests are about the OTP+Face
WIRING (both required, proposal ownership, the admin-issued guardrail), not
the face-match model. The OTP itself is real: `send_teams_message` is
monkeypatched to capture the text BanK would have posted to Teams, and the
6-digit code is pulled out of it, exactly like a human reading the channel.
"""

from __future__ import annotations

import re


async def _promote(supabase, user) -> None:
    """The only way to become an admin in this app - a direct database
    write, same helper as test_admin_api.py."""
    await supabase.table("users").update({"role": "admin"}).eq("id", str(user.id)).execute()


def _capture_teams(monkeypatch):
    captured: dict = {}

    async def fake_send_teams_message(text: str) -> bool:
        captured["text"] = text
        return True

    monkeypatch.setattr("app.modules.esign.service.send_teams_message", fake_send_teams_message)
    return captured


def _mock_face_auth(monkeypatch, *, valid_token: str = "a-valid-face-token"):
    async def fake_has_face_enrolled(_supabase, _user):
        return True

    async def fake_consume(_supabase, _user, token):
        if token != valid_token:
            from app.core.exceptions import InvalidFaceConfirmationError

            raise InvalidFaceConfirmationError()

    monkeypatch.setattr(
        "app.modules.esign.service.face_auth_service.has_face_enrolled", fake_has_face_enrolled
    )
    monkeypatch.setattr(
        "app.modules.esign.service.face_auth_service.consume_face_confirmation_token", fake_consume
    )


async def test_send_document_creates_admin_issued_document(
    supabase, authed_client, authed_client_factory
):
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    _target_client, target = await authed_client_factory()

    resp = await admin_client.post(
        f"/api/v1/admin/users/{target.id}/documents",
        json={"title": "Adeverință de venit", "body": "Conținutul documentului de test."},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["filename"] == "Adeverință de venit.pdf"

    row = (
        await supabase.table("documents")
        .select("*")
        .eq("id", body["id"])
        .maybe_single()
        .execute()
    ).data
    assert row["user_id"] == str(target.id)
    assert row["issued_by_admin_id"] == str(admin.id)
    assert row["conversation_id"] is not None
    # The generated PDF's own extracted text carries the TARGET's name, not
    # the admin's - proves the template was filled from the right profile.
    assert target.first_name in row["extracted_text"]


async def test_send_document_to_unknown_user_is_404(supabase, authed_client):
    import uuid

    admin_client, admin = authed_client
    await _promote(supabase, admin)

    resp = await admin_client.post(
        f"/api/v1/admin/users/{uuid.uuid4()}/documents",
        json={"title": "T", "body": "B"},
    )
    assert resp.status_code == 404, resp.text


async def test_send_document_body_too_long_is_rejected(
    supabase, authed_client, authed_client_factory
):
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    _target_client, target = await authed_client_factory()

    resp = await admin_client.post(
        f"/api/v1/admin/users/{target.id}/documents",
        json={"title": "T", "body": "x" * 3001},
    )
    assert resp.status_code == 422, resp.text


async def test_documents_to_sign_lists_admin_issued_with_signed_flag(
    supabase, authed_client, authed_client_factory
):
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    target_client, target = await authed_client_factory()

    # A self-uploaded document must NOT appear in this list.
    upload_resp = await target_client.post(
        "/api/v1/documents/upload",
        files={"file": ("mine.pdf", _pdf_bytes("Document propriu."), "application/pdf")},
    )
    assert upload_resp.status_code == 200, upload_resp.text

    send_resp = await admin_client.post(
        f"/api/v1/admin/users/{target.id}/documents",
        json={"title": "Notificare", "body": "Conținut."},
    )
    assert send_resp.status_code == 200, send_resp.text

    list_resp = await target_client.get("/api/v1/documents/to-sign")
    assert list_resp.status_code == 200, list_resp.text
    items = list_resp.json()
    assert len(items) == 1
    assert items[0]["filename"] == "Notificare.pdf"
    assert items[0]["signed"] is False


async def test_full_otp_and_face_confirm_signs_the_document(
    supabase, authed_client, authed_client_factory, monkeypatch
):
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    target_client, target = await authed_client_factory()
    captured = _capture_teams(monkeypatch)
    _mock_face_auth(monkeypatch)

    send_resp = await admin_client.post(
        f"/api/v1/admin/users/{target.id}/documents",
        json={"title": "Adeverință", "body": "Conținut oficial."},
    )
    document_id = send_resp.json()["id"]

    sign_request_resp = await target_client.post(
        f"/api/v1/esign/documents/{document_id}/sign-requests",
        json={"intent": "Sunt de acord."},
    )
    assert sign_request_resp.status_code == 200, sign_request_resp.text
    proposal_id = sign_request_resp.json()["id"]

    code_resp = await target_client.post(f"/api/v1/esign/proposals/{proposal_id}/signing-code")
    assert code_resp.status_code == 204, code_resp.text
    otp_code = re.search(r"\*\*(\d{6})\*\*", captured["text"]).group(1)

    confirm_resp = await target_client.post(
        f"/api/v1/esign/proposals/{proposal_id}/confirm-admin-document",
        json={"otp_code": otp_code, "face_token": "a-valid-face-token"},
    )
    assert confirm_resp.status_code == 200, confirm_resp.text
    assert confirm_resp.json()["status"] == "confirmed"

    signature_row = (
        await supabase.table("signatures")
        .select("*")
        .eq("proposal_id", proposal_id)
        .maybe_single()
        .execute()
    ).data
    assert signature_row is not None
    assert signature_row["auth_method"] == "otp_face"

    list_resp = await target_client.get("/api/v1/documents/to-sign")
    assert list_resp.json()[0]["signed"] is True


async def test_wrong_otp_code_is_rejected(
    supabase, authed_client, authed_client_factory, monkeypatch
):
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    target_client, target = await authed_client_factory()
    _capture_teams(monkeypatch)
    _mock_face_auth(monkeypatch)

    send_resp = await admin_client.post(
        f"/api/v1/admin/users/{target.id}/documents",
        json={"title": "Adeverință", "body": "Conținut oficial."},
    )
    document_id = send_resp.json()["id"]
    sign_request_resp = await target_client.post(
        f"/api/v1/esign/documents/{document_id}/sign-requests",
        json={"intent": "Sunt de acord."},
    )
    proposal_id = sign_request_resp.json()["id"]
    await target_client.post(f"/api/v1/esign/proposals/{proposal_id}/signing-code")

    resp = await target_client.post(
        f"/api/v1/esign/proposals/{proposal_id}/confirm-admin-document",
        json={"otp_code": "000000", "face_token": "a-valid-face-token"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_signing_code"

    resp2 = (
        await supabase.table("signatures")
        .select("id")
        .eq("proposal_id", proposal_id)
        .maybe_single()
        .execute()
    )
    assert resp2 is None or resp2.data is None


async def test_five_wrong_otp_codes_locks_out_further_attempts(
    supabase, authed_client, authed_client_factory, monkeypatch
):
    """confirm_admin_document is a deliberately separate function from
    proposals_service.confirm_proposal (see its docstring) with its own
    counter - document_signing_codes.attempts, same threshold of 5 as
    CONFIRM_MAX_FAILED_ATTEMPTS - rather than the shared login_attempts-based
    one the password/face path uses. This proves that counter actually
    trips, the same guarantee the other two auth methods get from theirs."""
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    target_client, target = await authed_client_factory()
    captured = _capture_teams(monkeypatch)
    _mock_face_auth(monkeypatch)

    send_resp = await admin_client.post(
        f"/api/v1/admin/users/{target.id}/documents",
        json={"title": "Adeverință", "body": "Conținut oficial."},
    )
    document_id = send_resp.json()["id"]
    sign_request_resp = await target_client.post(
        f"/api/v1/esign/documents/{document_id}/sign-requests",
        json={"intent": "Sunt de acord."},
    )
    proposal_id = sign_request_resp.json()["id"]
    await target_client.post(f"/api/v1/esign/proposals/{proposal_id}/signing-code")
    otp_code = re.search(r"\*\*(\d{6})\*\*", captured["text"]).group(1)

    for _ in range(5):
        resp = await target_client.post(
            f"/api/v1/esign/proposals/{proposal_id}/confirm-admin-document",
            json={"otp_code": "000000", "face_token": "a-valid-face-token"},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "invalid_signing_code"

    # 6th attempt is locked out even with the CORRECT code now.
    resp = await target_client.post(
        f"/api/v1/esign/proposals/{proposal_id}/confirm-admin-document",
        json={"otp_code": otp_code, "face_token": "a-valid-face-token"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_signing_code"

    resp2 = (
        await supabase.table("signatures")
        .select("id")
        .eq("proposal_id", proposal_id)
        .maybe_single()
        .execute()
    )
    assert resp2 is None or resp2.data is None


async def test_self_uploaded_document_cannot_use_otp_face_path(
    authed_client, monkeypatch
):
    """The stronger OTP+Face path is reserved for admin-issued documents -
    see esign_service._require_admin_issued_sign_proposal. A self-uploaded
    document must be signed through the ordinary Face-or-password confirm
    (POST /chat/proposals/{id}/confirm) instead."""
    client, _user = authed_client
    _capture_teams(monkeypatch)

    upload_resp = await client.post(
        "/api/v1/documents/upload",
        files={"file": ("mine.pdf", _pdf_bytes("Document propriu."), "application/pdf")},
    )
    document_id = upload_resp.json()["document"]["id"]

    sign_request_resp = await client.post(
        f"/api/v1/esign/documents/{document_id}/sign-requests",
        json={"intent": "Sunt de acord."},
    )
    proposal_id = sign_request_resp.json()["id"]

    resp = await client.post(f"/api/v1/esign/proposals/{proposal_id}/signing-code")
    assert resp.status_code == 422, resp.text


def _pdf_bytes(text: str) -> bytes:
    import pymupdf

    document = pymupdf.open()
    document.new_page().insert_text((72, 72), text)
    pdf_bytes = document.tobytes()
    document.close()
    return pdf_bytes
