"""confirm_proposal must refuse `auth_method: "password"` for exactly the
transfer/payment proposals that would have required Face ID (not password)
on the direct, non-AI path - see proposals_service._proposal_requires_face.

Before this guardrail, `proposal_pre_authorized=True` (set only by
_execute) skipped face_auth_service.enforce_face_confirmation entirely, so
a large transfer or a first payment to a new recipient proposed by the AI
chat agent could be confirmed with a plain password - silently defeating
the mandatory-Face-ID policy on the one path most users would actually take.
"""

from __future__ import annotations

from app.core.exceptions import InvalidFaceConfirmationError
from app.modules.chat.proposals_service import (
    FACE_FAILURES_BEFORE_PASSWORD_FALLBACK,
    create_proposal,
)
from app.modules.face_auth.service import FACE_CONFIRMATION_THRESHOLD_MINOR

_LARGE_AMOUNT = FACE_CONFIRMATION_THRESHOLD_MINOR + 100
_SMALL_AMOUNT = FACE_CONFIRMATION_THRESHOLD_MINOR - 100


async def _open_account(client, name="Checking", currency="RON"):
    resp = await client.post("/api/v1/accounts", json={"name": name, "currency": currency})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _seed_transfer(supabase, user, conversation, from_account, to_account, amount):
    return await create_proposal(
        supabase,
        user_id=str(user.id),
        conversation_id=conversation["id"],
        proposal_type="transfer",
        payload={
            "from_account_id": from_account["id"],
            "to_account_id": to_account["id"],
            "amount_minor": amount,
            "currency": "RON",
            "description": None,
        },
        summary=f"Transfer de {amount / 100:.2f} RON",
    )


async def _seed_payment(supabase, user, conversation, from_account, to_iban, amount):
    return await create_proposal(
        supabase,
        user_id=str(user.id),
        conversation_id=conversation["id"],
        proposal_type="payment",
        payload={
            "from_account_id": from_account["id"],
            "to_iban": to_iban,
            "beneficiary_name": "Test Recipient",
            "amount_minor": amount,
            "description": None,
            "save_beneficiary": False,
        },
        summary=f"Plată de {amount / 100:.2f} RON",
    )


async def test_large_transfer_confirmed_with_password_is_rejected(
    supabase, authed_client, conversation_factory, seed_balance_factory
):
    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")
    await seed_balance_factory(from_account["id"], _LARGE_AMOUNT * 2, currency="RON")
    conversation = await conversation_factory(user)
    proposal = await _seed_transfer(
        supabase, user, conversation, from_account, to_account, _LARGE_AMOUNT
    )
    # Not an exact literal: a freshly opened account also gets an automatic
    # referral opening-balance grant on top of what seed_balance_factory
    # adds - the guardrail is that the balance doesn't move AT ALL, whatever
    # it started at.
    from_before = (await client.get(f"/api/v1/accounts/{from_account['id']}")).json()

    resp = await client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    assert resp.status_code == 428, resp.text
    assert resp.json()["error"]["code"] == "face_auth_method_required"

    from_after = (await client.get(f"/api/v1/accounts/{from_account['id']}")).json()
    assert from_after["balance_minor"] == from_before["balance_minor"]  # untouched


async def test_large_transfer_confirmed_with_face_succeeds(
    supabase, authed_client, conversation_factory, seed_balance_factory, monkeypatch
):
    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")
    await seed_balance_factory(from_account["id"], _LARGE_AMOUNT * 2, currency="RON")
    conversation = await conversation_factory(user)
    proposal = await _seed_transfer(
        supabase, user, conversation, from_account, to_account, _LARGE_AMOUNT
    )
    from_before = (await client.get(f"/api/v1/accounts/{from_account['id']}")).json()

    async def _fake_enrolled(_supabase, _user):
        return True

    async def _fake_consume(_supabase, _user, token):
        assert token == "a-valid-face-token"

    monkeypatch.setattr(
        "app.modules.chat.proposals_service.face_auth_service.has_face_enrolled", _fake_enrolled
    )
    monkeypatch.setattr(
        "app.modules.chat.proposals_service.face_auth_service.consume_face_confirmation_token",
        _fake_consume,
    )

    resp = await client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "face", "credential": "a-valid-face-token"},
    )
    assert resp.status_code == 200, resp.text

    from_after = (await client.get(f"/api/v1/accounts/{from_account['id']}")).json()
    assert from_after["balance_minor"] == from_before["balance_minor"] - _LARGE_AMOUNT


async def test_small_transfer_confirmed_with_password_still_works(
    supabase, authed_client, conversation_factory, seed_balance_factory
):
    """No regression: an ordinary, below-threshold transfer must keep
    working with a plain password, exactly as before this guardrail."""
    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")
    await seed_balance_factory(from_account["id"], _SMALL_AMOUNT * 2, currency="RON")
    conversation = await conversation_factory(user)
    proposal = await _seed_transfer(
        supabase, user, conversation, from_account, to_account, _SMALL_AMOUNT
    )

    resp = await client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    assert resp.status_code == 200, resp.text


async def test_first_payment_to_new_recipient_confirmed_with_password_is_rejected(
    supabase, authed_client, authed_client_factory, conversation_factory, seed_balance_factory
):
    sender_client, sender = authed_client
    from_account = await _open_account(sender_client, "Cont Curent")
    await seed_balance_factory(from_account["id"], _SMALL_AMOUNT * 2, currency="RON")

    recipient_client, _recipient = await authed_client_factory()
    to_account = await _open_account(recipient_client, "Cont Destinatar")

    conversation = await conversation_factory(sender)
    proposal = await _seed_payment(
        supabase, sender, conversation, from_account, to_account["iban"], _SMALL_AMOUNT
    )

    resp = await sender_client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    assert resp.status_code == 428, resp.text
    assert resp.json()["error"]["code"] == "face_auth_method_required"


async def test_payment_to_known_beneficiary_confirmed_with_password_still_works(
    supabase, authed_client, authed_client_factory, conversation_factory, seed_balance_factory
):
    """No regression: a payment to an already-saved beneficiary is NOT a
    "first payment to a new recipient", so password alone stays enough."""
    sender_client, sender = authed_client
    from_account = await _open_account(sender_client, "Cont Curent")
    await seed_balance_factory(from_account["id"], _SMALL_AMOUNT * 2, currency="RON")

    recipient_client, _recipient = await authed_client_factory()
    to_account = await _open_account(recipient_client, "Cont Destinatar")

    await supabase.table("beneficiaries").insert(
        {
            "user_id": str(sender.id),
            "display_name": "Test Recipient",
            "iban": to_account["iban"],
        }
    ).execute()

    conversation = await conversation_factory(sender)
    proposal = await _seed_payment(
        supabase, sender, conversation, from_account, to_account["iban"], _SMALL_AMOUNT
    )

    resp = await sender_client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    assert resp.status_code == 200, resp.text


async def test_large_transfer_confirmed_with_password_succeeds_after_enough_failed_attempts(
    supabase, authed_client, conversation_factory, seed_balance_factory, monkeypatch
):
    """The guardrail isn't a hard wall: once FACE_FAILURES_BEFORE_PASSWORD_
    FALLBACK failed attempts are already on record for this proposal (in
    practice, always failed face attempts - password is rejected outright
    before that many exist), a password retry is accepted like any other
    proposal - mirrors face_auth/service.py::enforce_face_confirmation's own
    password fallback for the direct (non-AI) path."""
    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")
    await seed_balance_factory(from_account["id"], _LARGE_AMOUNT * 2, currency="RON")
    conversation = await conversation_factory(user)
    proposal = await _seed_transfer(
        supabase, user, conversation, from_account, to_account, _LARGE_AMOUNT
    )

    async def _fake_enrolled(_supabase, _user):
        return True

    async def _fake_consume_always_fails(_supabase, _user, _token):
        raise InvalidFaceConfirmationError()

    monkeypatch.setattr(
        "app.modules.chat.proposals_service.face_auth_service.has_face_enrolled", _fake_enrolled
    )
    monkeypatch.setattr(
        "app.modules.chat.proposals_service.face_auth_service.consume_face_confirmation_token",
        _fake_consume_always_fails,
    )

    for _ in range(FACE_FAILURES_BEFORE_PASSWORD_FALLBACK):
        resp = await client.post(
            f"/api/v1/chat/proposals/{proposal['id']}/confirm",
            json={"auth_method": "face", "credential": "wrong-token"},
        )
        assert resp.status_code == 400, resp.text  # invalid_face_confirmation

    from_before = (await client.get(f"/api/v1/accounts/{from_account['id']}")).json()

    resp = await client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    assert resp.status_code == 200, resp.text

    from_after = (await client.get(f"/api/v1/accounts/{from_account['id']}")).json()
    assert from_after["balance_minor"] == from_before["balance_minor"] - _LARGE_AMOUNT


async def test_large_transfer_confirmed_with_password_still_rejected_below_the_threshold(
    supabase, authed_client, conversation_factory, seed_balance_factory, monkeypatch
):
    """One failed face attempt short of the threshold: password must still
    be refused, not just eventually accepted after "some" failures."""
    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")
    await seed_balance_factory(from_account["id"], _LARGE_AMOUNT * 2, currency="RON")
    conversation = await conversation_factory(user)
    proposal = await _seed_transfer(
        supabase, user, conversation, from_account, to_account, _LARGE_AMOUNT
    )

    async def _fake_enrolled(_supabase, _user):
        return True

    async def _fake_consume_always_fails(_supabase, _user, _token):
        raise InvalidFaceConfirmationError()

    monkeypatch.setattr(
        "app.modules.chat.proposals_service.face_auth_service.has_face_enrolled", _fake_enrolled
    )
    monkeypatch.setattr(
        "app.modules.chat.proposals_service.face_auth_service.consume_face_confirmation_token",
        _fake_consume_always_fails,
    )

    for _ in range(FACE_FAILURES_BEFORE_PASSWORD_FALLBACK - 1):
        await client.post(
            f"/api/v1/chat/proposals/{proposal['id']}/confirm",
            json={"auth_method": "face", "credential": "wrong-token"},
        )

    resp = await client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    assert resp.status_code == 428, resp.text
    assert resp.json()["error"]["code"] == "face_auth_method_required"


async def test_non_transfer_proposal_is_unaffected_by_the_guardrail(
    supabase, authed_client, conversation_factory
):
    """open_account has no amount/recipient at all - _proposal_requires_face
    must short-circuit on proposal_type alone, never touch payload["amount_minor"]."""
    client, user = authed_client
    conversation = await conversation_factory(user)
    proposal = await create_proposal(
        supabase,
        user_id=str(user.id),
        conversation_id=conversation["id"],
        proposal_type="open_account",
        payload={"name": "Cont Nou", "currency": "RON", "product_type": "checking"},
        summary="Deschidere cont nou",
    )

    resp = await client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    assert resp.status_code == 200, resp.text
