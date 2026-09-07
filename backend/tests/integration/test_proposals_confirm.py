"""POST /api/v1/chat/proposals/{id}/confirm and .../reject (Step 11).

Proposals are seeded directly via proposals_service.create_proposal (the
propose_* tools themselves, driven through /api/v1/chat, are covered in
test_propose_tools.py) so these tests can focus on the step-up auth gate and
the execute/idempotency/expiry machinery in proposals_service.confirm_proposal.

Face auth is mocked at the module boundary (app.modules.face_auth.service),
same pattern test_trusted_devices_api.py uses for its own external calls -
these tests are about the propose/confirm flow, not the face-match model.
"""

from datetime import UTC, datetime, timedelta

from app.modules.chat.proposals_service import create_proposal


async def _open_account(client, name="Checking", currency="RON"):
    resp = await client.post("/api/v1/accounts", json={"name": name, "currency": currency})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _seed(supabase, user, conversation, from_account, to_account, amount=10_000):
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


async def _row(supabase, proposal_id, select="*"):
    resp = (
        await supabase.table("proposals")
        .select(select)
        .eq("id", proposal_id)
        .maybe_single()
        .execute()
    )
    return resp.data


async def test_confirm_with_correct_password_executes_transfer(
    authed_client, supabase, conversation_factory
):
    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")
    conversation = await conversation_factory(user)
    proposal = await _seed(supabase, user, conversation, from_account, to_account)

    resp = await client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "confirmed"

    from_after = (await client.get(f"/api/v1/accounts/{from_account['id']}")).json()
    to_after = (await client.get(f"/api/v1/accounts/{to_account['id']}")).json()
    assert from_after["balance_minor"] == from_account["balance_minor"] - 10_000
    assert to_after["balance_minor"] == to_account["balance_minor"] + 10_000

    row = await _row(supabase, proposal["id"])
    assert row["status"] == "confirmed"
    assert row["confirmed_at"] is not None
    assert row["result"] is not None


async def test_confirm_with_wrong_password_returns_error_and_stays_pending(
    authed_client, supabase, conversation_factory
):
    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")
    conversation = await conversation_factory(user)
    proposal = await _seed(supabase, user, conversation, from_account, to_account)

    resp = await client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "password", "credential": "wrong-password"},
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "unauthorized"

    from_after = (await client.get(f"/api/v1/accounts/{from_account['id']}")).json()
    assert from_after["balance_minor"] == from_account["balance_minor"]

    row = await _row(supabase, proposal["id"], select="status")
    assert row["status"] == "pending"


async def test_confirm_with_valid_face_token_executes(
    authed_client, supabase, conversation_factory, monkeypatch
):
    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")
    conversation = await conversation_factory(user)
    proposal = await _seed(supabase, user, conversation, from_account, to_account)

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
    assert resp.json()["status"] == "confirmed"

    from_after = (await client.get(f"/api/v1/accounts/{from_account['id']}")).json()
    assert from_after["balance_minor"] == from_account["balance_minor"] - 10_000


async def test_confirm_with_invalid_face_token_does_not_execute(
    authed_client, supabase, conversation_factory, monkeypatch
):
    from app.core.exceptions import InvalidFaceConfirmationError

    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")
    conversation = await conversation_factory(user)
    proposal = await _seed(supabase, user, conversation, from_account, to_account)

    async def _fake_enrolled(_supabase, _user):
        return True

    async def _fake_consume(_supabase, _user, _token):
        raise InvalidFaceConfirmationError()

    monkeypatch.setattr(
        "app.modules.chat.proposals_service.face_auth_service.has_face_enrolled", _fake_enrolled
    )
    monkeypatch.setattr(
        "app.modules.chat.proposals_service.face_auth_service.consume_face_confirmation_token",
        _fake_consume,
    )

    resp = await client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "face", "credential": "a-bad-token"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_face_confirmation"

    from_after = (await client.get(f"/api/v1/accounts/{from_account['id']}")).json()
    assert from_after["balance_minor"] == from_account["balance_minor"]

    row = await _row(supabase, proposal["id"], select="status")
    assert row["status"] == "pending"


async def test_confirm_face_method_without_enrollment_is_rejected(
    authed_client, supabase, conversation_factory, monkeypatch
):
    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")
    conversation = await conversation_factory(user)
    proposal = await _seed(supabase, user, conversation, from_account, to_account)

    async def _fake_not_enrolled(_supabase, _user):
        return False

    monkeypatch.setattr(
        "app.modules.chat.proposals_service.face_auth_service.has_face_enrolled",
        _fake_not_enrolled,
    )

    resp = await client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "face", "credential": "anything"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "validation_error"

    row = await _row(supabase, proposal["id"], select="status")
    assert row["status"] == "pending"


async def test_confirm_someone_elses_proposal_returns_404(
    authed_client, authed_client_factory, supabase, conversation_factory
):
    owner_client, owner = authed_client
    from_account = await _open_account(owner_client, "Cont Curent")
    to_account = await _open_account(owner_client, "Economii")
    conversation = await conversation_factory(owner)
    proposal = await _seed(supabase, owner, conversation, from_account, to_account)

    other_client, _other_user = await authed_client_factory()

    resp = await other_client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    assert resp.status_code == 404, resp.text


async def test_confirm_already_confirmed_returns_error_no_double_execution(
    authed_client, supabase, conversation_factory
):
    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")
    conversation = await conversation_factory(user)
    proposal = await _seed(supabase, user, conversation, from_account, to_account)

    first = await client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    assert first.status_code == 200, first.text

    second = await client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "proposal_not_pending"

    from_after = (await client.get(f"/api/v1/accounts/{from_account['id']}")).json()
    # Only the first confirm's transfer applied - not two.
    assert from_after["balance_minor"] == from_account["balance_minor"] - 10_000


async def test_confirm_expired_proposal_returns_error(
    authed_client, supabase, conversation_factory
):
    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")
    conversation = await conversation_factory(user)
    proposal = await _seed(supabase, user, conversation, from_account, to_account)

    long_ago = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    await (
        supabase.table("proposals")
        .update({"created_at": long_ago})
        .eq("id", proposal["id"])
        .execute()
    )

    resp = await client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "proposal_expired"

    row = await _row(supabase, proposal["id"], select="status")
    assert row["status"] == "expired"


async def test_reject_marks_rejected(authed_client, supabase, conversation_factory):
    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")
    conversation = await conversation_factory(user)
    proposal = await _seed(supabase, user, conversation, from_account, to_account)

    resp = await client.post(f"/api/v1/chat/proposals/{proposal['id']}/reject")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"

    row = await _row(supabase, proposal["id"])
    assert row["status"] == "rejected"
    assert row["rejected_at"] is not None

    from_after = (await client.get(f"/api/v1/accounts/{from_account['id']}")).json()
    assert from_after["balance_minor"] == from_account["balance_minor"]


async def test_reject_someone_elses_proposal_returns_404(
    authed_client, authed_client_factory, supabase, conversation_factory
):
    owner_client, owner = authed_client
    from_account = await _open_account(owner_client, "Cont Curent")
    to_account = await _open_account(owner_client, "Economii")
    conversation = await conversation_factory(owner)
    proposal = await _seed(supabase, owner, conversation, from_account, to_account)

    other_client, _other_user = await authed_client_factory()

    resp = await other_client.post(f"/api/v1/chat/proposals/{proposal['id']}/reject")
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Confirm-attempt rate limiting (Step 16, item 5) - see
# proposals_service.CONFIRM_MAX_FAILED_ATTEMPTS. Keyed on (proposal_id,
# user_id), so unlike login lockout these attempts never span two proposals.
# ---------------------------------------------------------------------------


async def test_confirm_allows_a_correct_attempt_under_the_cap_and_clears_it(
    authed_client, supabase, conversation_factory
):
    """Wrong guesses below the 5-attempt cap don't poison a later correct
    one - and a successful confirm leaves nothing behind under its key."""
    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")
    conversation = await conversation_factory(user)
    proposal = await _seed(supabase, user, conversation, from_account, to_account)

    for _ in range(4):
        resp = await client.post(
            f"/api/v1/chat/proposals/{proposal['id']}/confirm",
            json={"auth_method": "password", "credential": "wrong-password"},
        )
        assert resp.status_code == 401, resp.text

    resp = await client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    assert resp.status_code == 200, resp.text

    from app.modules.chat.proposals_service import _confirm_attempt_key

    key_resp = (
        await supabase.table("login_attempts")
        .select("id")
        .eq("email", _confirm_attempt_key(str(user.id), proposal["id"]))
        .execute()
    )
    assert key_resp.data == []


async def test_confirm_locks_out_on_the_sixth_attempt_after_five_failures(
    authed_client, supabase, conversation_factory
):
    """The cap blocks the NEXT attempt outright, before the credential is
    even looked at - so a correct password on the 6th call is rejected with
    429 exactly like a wrong one would be. That is the point: at 5 already-
    failed attempts, nothing about the 6th call is trusted enough to
    evaluate."""
    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")
    conversation = await conversation_factory(user)
    proposal = await _seed(supabase, user, conversation, from_account, to_account)

    for _ in range(5):
        resp = await client.post(
            f"/api/v1/chat/proposals/{proposal['id']}/confirm",
            json={"auth_method": "password", "credential": "wrong-password"},
        )
        assert resp.status_code == 401, resp.text

    resp = await client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    assert resp.status_code == 429, resp.text
    assert resp.json()["error"]["code"] == "proposal_rate_limited"

    row = await _row(supabase, proposal["id"], select="status")
    assert row["status"] == "pending"


async def test_confirm_rate_limit_never_trips_a_fresh_proposals_first_attempt(
    authed_client, supabase, conversation_factory
):
    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")
    conversation = await conversation_factory(user)
    proposal = await _seed(supabase, user, conversation, from_account, to_account)

    resp = await client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    assert resp.status_code == 200, resp.text


async def test_expired_proposal_error_takes_precedence_over_the_rate_limit(
    authed_client, supabase, conversation_factory
):
    """The status/expiry gate runs BEFORE the rate-limit check in
    confirm_proposal, so an expired proposal always reports 'expired', never
    'rate limited', regardless of how many failed attempts it accumulated
    while it was still pending."""
    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")
    conversation = await conversation_factory(user)
    proposal = await _seed(supabase, user, conversation, from_account, to_account)

    for _ in range(5):
        resp = await client.post(
            f"/api/v1/chat/proposals/{proposal['id']}/confirm",
            json={"auth_method": "password", "credential": "wrong-password"},
        )
        assert resp.status_code == 401, resp.text

    long_ago = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    await (
        supabase.table("proposals")
        .update({"created_at": long_ago})
        .eq("id", proposal["id"])
        .execute()
    )

    resp = await client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "password", "credential": "password123"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "proposal_expired"


async def test_credential_never_in_proposal_payload_or_result(
    authed_client, supabase, conversation_factory
):
    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")
    conversation = await conversation_factory(user)
    proposal = await _seed(supabase, user, conversation, from_account, to_account)

    secret_password = "password123"
    resp = await client.post(
        f"/api/v1/chat/proposals/{proposal['id']}/confirm",
        json={"auth_method": "password", "credential": secret_password},
    )
    assert resp.status_code == 200, resp.text

    row = await _row(supabase, proposal["id"])
    dumped = str(row)
    assert secret_password not in dumped
