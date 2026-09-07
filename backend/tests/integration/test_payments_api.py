async def _open_account(client, name="Checking", currency="USD"):
    resp = await client.post("/api/v1/accounts", json={"name": name, "currency": currency})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_create_payment_moves_balance_between_users(
    authed_client, authed_client_factory, enroll_face
):
    payer, payer_user = authed_client
    payer_account = await _open_account(payer, "Payer")

    payee, _payee_user = await authed_client_factory()
    payee_account = await _open_account(payee, "Payee")

    # First payment to a person the payer has never paid before - now a
    # mandatory face-confirmation trigger (see
    # face_auth/service.py::enforce_face_confirmation).
    face_token = await enroll_face(payer_user.id)
    resp = await payer.post(
        "/api/v1/payments",
        json={
            "from_account_id": payer_account["id"],
            "to_iban": payee_account["iban"],
            "beneficiary_name": "Payee Person",
            "amount_minor": 1_000,
        },
        headers={"Idempotency-Key": "payment-1", "X-Face-Confirmation": face_token},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["to_account_id"] == payee_account["id"]
    assert body["to_iban"] == payee_account["iban"]
    assert body["amount_minor"] == 1_000
    assert body["status"] == "completed"

    payer_after = (await payer.get(f"/api/v1/accounts/{payer_account['id']}")).json()
    payee_after = (await payee.get(f"/api/v1/accounts/{payee_account['id']}")).json()
    assert payer_after["balance_minor"] == payer_account["balance_minor"] - 1_000
    assert payee_after["balance_minor"] == payee_account["balance_minor"] + 1_000


async def test_create_payment_can_be_confirmed_with_password_instead_of_face(
    authed_client, authed_client_factory, enroll_face
):
    """The frontend only offers this after several failed face captures in
    the same modal session (see requestFaceConfirmationToken in app.js), but
    the backend itself just accepts whichever credential it's given - see
    enforce_face_confirmation's docstring."""
    payer, payer_user = authed_client
    payer_account = await _open_account(payer, "Payer")

    payee, _payee_user = await authed_client_factory()
    payee_account = await _open_account(payee, "Payee")

    # Enrollment is still a precondition either way - only which credential
    # proves "still you" changes. user_factory always seeds "password123"
    # (see tests/conftest.py).
    await enroll_face(payer_user.id)
    resp = await payer.post(
        "/api/v1/payments",
        json={
            "from_account_id": payer_account["id"],
            "to_iban": payee_account["iban"],
            "beneficiary_name": "Payee Person",
            "amount_minor": 1_000,
        },
        headers={"Idempotency-Key": "payment-password-1", "X-Step-Up-Password": "password123"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "completed"


async def test_create_payment_rejects_a_wrong_step_up_password(
    authed_client, authed_client_factory, enroll_face
):
    payer, payer_user = authed_client
    payer_account = await _open_account(payer, "Payer")

    payee, _payee_user = await authed_client_factory()
    payee_account = await _open_account(payee, "Payee")

    await enroll_face(payer_user.id)
    resp = await payer.post(
        "/api/v1/payments",
        json={
            "from_account_id": payer_account["id"],
            "to_iban": payee_account["iban"],
            "beneficiary_name": "Payee Person",
            "amount_minor": 1_000,
        },
        headers={"Idempotency-Key": "payment-password-2", "X-Step-Up-Password": "wrong-password"},
    )
    assert resp.status_code == 401, resp.text

    # Rejected, not just unconfirmed - no money moved.
    payer_after = (await payer.get(f"/api/v1/accounts/{payer_account['id']}")).json()
    payee_after = (await payee.get(f"/api/v1/accounts/{payee_account['id']}")).json()
    assert payer_after["balance_minor"] == payer_account["balance_minor"]
    assert payee_after["balance_minor"] == payee_account["balance_minor"]


async def test_create_payment_saves_and_updates_beneficiary(
    authed_client, authed_client_factory, enroll_face
):
    payer, payer_user = authed_client
    payer_account = await _open_account(payer, "Payer")

    payee, _payee_user = await authed_client_factory()
    payee_account = await _open_account(payee, "Payee")

    face_token = await enroll_face(payer_user.id)
    await payer.post(
        "/api/v1/payments",
        json={
            "from_account_id": payer_account["id"],
            "to_iban": payee_account["iban"],
            "beneficiary_name": "First Name",
            "amount_minor": 500,
        },
        headers={"Idempotency-Key": "payment-a", "X-Face-Confirmation": face_token},
    )

    contacts = (await payer.get("/api/v1/beneficiaries")).json()
    assert len(contacts) == 1
    assert contacts[0]["iban"] == payee_account["iban"]
    assert contacts[0]["display_name"] == "First Name"

    # Paying the same IBAN again with a different name updates the contact,
    # not duplicates it.
    await payer.post(
        "/api/v1/payments",
        json={
            "from_account_id": payer_account["id"],
            "to_iban": payee_account["iban"],
            "beneficiary_name": "Updated Name",
            "amount_minor": 100,
        },
        headers={"Idempotency-Key": "payment-b"},
    )
    contacts = (await payer.get("/api/v1/beneficiaries")).json()
    assert len(contacts) == 1
    assert contacts[0]["display_name"] == "Updated Name"


async def test_payment_rejects_unknown_iban(authed_client):
    client, _user = authed_client
    account = await _open_account(client)

    resp = await client.post(
        "/api/v1/payments",
        json={
            "from_account_id": account["id"],
            "to_iban": "RO49AAAA1B31007593840000",
            "beneficiary_name": "Nobody",
            "amount_minor": 100,
        },
        headers={"Idempotency-Key": "k1"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "iban_not_found"


async def test_payment_rejects_unowned_from_account(authed_client, authed_client_factory):
    owner, _owner_user = authed_client
    owner_account = await _open_account(owner)

    other, _other_user = await authed_client_factory()
    other_account = await _open_account(other)

    resp = await other.post(
        "/api/v1/payments",
        json={
            "from_account_id": owner_account["id"],
            "to_iban": other_account["iban"],
            "beneficiary_name": "Someone",
            "amount_minor": 100,
        },
        headers={"Idempotency-Key": "k1"},
    )
    assert resp.status_code == 404


async def test_payment_rejects_same_account(authed_client):
    client, _user = authed_client
    account = await _open_account(client)

    resp = await client.post(
        "/api/v1/payments",
        json={
            "from_account_id": account["id"],
            "to_iban": account["iban"],
            "beneficiary_name": "Myself",
            "amount_minor": 100,
        },
        headers={"Idempotency-Key": "k1"},
    )
    assert resp.status_code == 422


async def test_payment_rejects_currency_mismatch(authed_client, authed_client_factory):
    payer, _payer_user = authed_client
    payer_account = await _open_account(payer, "Payer", currency="USD")

    payee, _payee_user = await authed_client_factory()
    payee_account = await _open_account(payee, "Payee", currency="EUR")

    resp = await payer.post(
        "/api/v1/payments",
        json={
            "from_account_id": payer_account["id"],
            "to_iban": payee_account["iban"],
            "beneficiary_name": "Payee",
            "amount_minor": 100,
        },
        headers={"Idempotency-Key": "k1"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "currency_mismatch"


async def test_payment_requires_idempotency_key(authed_client, authed_client_factory):
    payer, _payer_user = authed_client
    payer_account = await _open_account(payer)
    payee, _payee_user = await authed_client_factory()
    payee_account = await _open_account(payee)

    resp = await payer.post(
        "/api/v1/payments",
        json={
            "from_account_id": payer_account["id"],
            "to_iban": payee_account["iban"],
            "beneficiary_name": "Payee",
            "amount_minor": 100,
        },
    )
    assert resp.status_code == 400


async def _pay(payer, payer_account, payee_account, amount_minor, key, *, face_token=None, **extra):
    headers = {"Idempotency-Key": key}
    if face_token is not None:
        headers["X-Face-Confirmation"] = face_token
    return await payer.post(
        "/api/v1/payments",
        json={
            "from_account_id": payer_account["id"],
            "to_iban": payee_account["iban"],
            "beneficiary_name": "Netflix",
            "amount_minor": amount_minor,
            **extra,
        },
        headers=headers,
    )


async def _mark_as_subscription(client, iban, display_name="Netflix", **extra):
    resp = await client.post(
        "/api/v1/beneficiaries",
        json={"iban": iban, "display_name": display_name, "is_subscription": True, **extra},
    )
    assert resp.status_code == 201, resp.text


async def test_payment_blocks_subscription_price_increase(
    authed_client, authed_client_factory, enroll_face
):
    payer, payer_user = authed_client
    payer_account = await _open_account(payer, "Payer")
    payee, _payee_user = await authed_client_factory()
    payee_account = await _open_account(payee, "Payee")
    await _mark_as_subscription(payer, payee_account["iban"])

    # Two prior payments at the same amount establish the "recurring at
    # this price" pattern (see _detect_subscription_price_increase). Only
    # the first needs a face token - it's the only "new person" payment.
    face_token = await enroll_face(payer_user.id)
    assert (
        await _pay(payer, payer_account, payee_account, 4_000, "p1", face_token=face_token)
    ).status_code == 201
    assert (await _pay(payer, payer_account, payee_account, 4_000, "p2")).status_code == 201

    resp = await _pay(payer, payer_account, payee_account, 6_000, "p3")
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"]["code"] == "subscription_price_increase"
    assert body["error"]["details"]["previous_amount_minor"] == 4_000
    assert body["error"]["details"]["new_amount_minor"] == 6_000
    assert body["error"]["details"]["beneficiary_name"] == "Netflix"

    # Blocked, so no third payment actually happened.
    payer_after = (await payer.get(f"/api/v1/accounts/{payer_account['id']}")).json()
    assert payer_after["balance_minor"] == payer_account["balance_minor"] - 8_000


async def test_payment_price_increase_can_be_confirmed_and_retried(
    authed_client, authed_client_factory, enroll_face
):
    payer, payer_user = authed_client
    payer_account = await _open_account(payer, "Payer")
    payee, _payee_user = await authed_client_factory()
    payee_account = await _open_account(payee, "Payee")
    await _mark_as_subscription(payer, payee_account["iban"])

    face_token = await enroll_face(payer_user.id)
    await _pay(payer, payer_account, payee_account, 4_000, "q1", face_token=face_token)
    await _pay(payer, payer_account, payee_account, 4_000, "q2")

    blocked = await _pay(payer, payer_account, payee_account, 6_000, "q3")
    assert blocked.status_code == 409

    # Same idempotency key, now with the user's explicit go-ahead.
    resp = await _pay(
        payer, payer_account, payee_account, 6_000, "q3", confirm_price_increase=True
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["amount_minor"] == 6_000


async def test_payment_price_increase_includes_saved_website(
    authed_client, authed_client_factory, enroll_face
):
    payer, payer_user = authed_client
    payer_account = await _open_account(payer, "Payer")
    payee, _payee_user = await authed_client_factory()
    payee_account = await _open_account(payee, "Payee")

    await _mark_as_subscription(payer, payee_account["iban"], website="https://netflix.com")
    face_token = await enroll_face(payer_user.id)
    await _pay(payer, payer_account, payee_account, 4_000, "r1", face_token=face_token)
    await _pay(payer, payer_account, payee_account, 4_000, "r2")

    resp = await _pay(payer, payer_account, payee_account, 6_000, "r3")
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["website"] == "https://netflix.com"


async def test_payment_blocks_price_increase_via_known_subscription_name(
    authed_client, authed_client_factory, enroll_face
):
    """The new automatic trigger: a recipient NAME matching the hardcoded
    known-subscription list (see known_subscriptions.py) is enough on its
    own - no beneficiary ever saved, no is_subscription flag ever set. Same
    recurring-then-higher pattern and default beneficiary_name="Netflix" as
    _pay - deliberately does NOT call _mark_as_subscription."""
    payer, payer_user = authed_client
    payer_account = await _open_account(payer, "Payer")
    payee, _payee_user = await authed_client_factory()
    payee_account = await _open_account(payee, "Payee")

    face_token = await enroll_face(payer_user.id)
    assert (
        await _pay(payer, payer_account, payee_account, 4_000, "v1", face_token=face_token)
    ).status_code == 201
    assert (await _pay(payer, payer_account, payee_account, 4_000, "v2")).status_code == 201

    resp = await _pay(payer, payer_account, payee_account, 6_000, "v3")
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"]["code"] == "subscription_price_increase"
    assert body["error"]["details"]["website"] == "https://www.netflix.com/cancelplan"


async def test_payment_name_match_is_case_insensitive_and_substring(
    authed_client, authed_client_factory, enroll_face
):
    payer, payer_user = authed_client
    payer_account = await _open_account(payer, "Payer")
    payee, _payee_user = await authed_client_factory()
    payee_account = await _open_account(payee, "Payee")

    face_token = await enroll_face(payer_user.id)
    name = "SPOTIFY AB (Sweden)"
    await _pay(payer, payer_account, payee_account, 3_000, "w1", beneficiary_name=name, face_token=face_token)
    await _pay(payer, payer_account, payee_account, 3_000, "w2", beneficiary_name=name)

    resp = await _pay(payer, payer_account, payee_account, 5_000, "w3", beneficiary_name=name)
    assert resp.status_code == 409, resp.text


async def test_payment_without_recurring_history_is_not_blocked(
    authed_client, authed_client_factory, enroll_face
):
    """A single prior payment isn't a "pattern" yet - only the second
    repeat proves it's recurring, same threshold as the tool this reuses
    the logic from (detect_recurring_payments)."""
    payer, payer_user = authed_client
    payer_account = await _open_account(payer, "Payer")
    payee, _payee_user = await authed_client_factory()
    payee_account = await _open_account(payee, "Payee")

    face_token = await enroll_face(payer_user.id)
    await _pay(payer, payer_account, payee_account, 4_000, "s1", face_token=face_token)
    resp = await _pay(payer, payer_account, payee_account, 6_000, "s2")
    assert resp.status_code == 201, resp.text


async def test_payment_to_an_unmarked_person_is_never_blocked(
    authed_client, authed_client_factory, enroll_face
):
    """The whole point of is_subscription: a friend/family member paid the
    same recurring amount twice, then a higher one (e.g. splitting a
    bigger bill), must NOT be mistaken for "a subscription raised its
    price" just because the payment pattern looks the same. Only a contact
    explicitly marked is_subscription can ever trigger the block - see
    test_payment_blocks_subscription_price_increase for the contrasting
    case with the same amounts, marked."""
    payer, payer_user = authed_client
    payer_account = await _open_account(payer, "Payer")
    payee, _payee_user = await authed_client_factory()
    payee_account = await _open_account(payee, "Payee")

    face_token = await enroll_face(payer_user.id)
    await _pay(payer, payer_account, payee_account, 4_000, "t1", beneficiary_name="Ana", face_token=face_token)
    await _pay(payer, payer_account, payee_account, 4_000, "t2", beneficiary_name="Ana")
    resp = await _pay(payer, payer_account, payee_account, 6_000, "t3", beneficiary_name="Ana")
    assert resp.status_code == 201, resp.text


async def test_payment_to_a_saved_person_marked_not_subscription_is_never_blocked(
    authed_client, authed_client_factory, enroll_face
):
    """Same as above, but the contact IS saved (auto-saved by the payments
    themselves) - is_subscription defaults to false, so it still must not
    block."""
    payer, payer_user = authed_client
    payer_account = await _open_account(payer, "Payer")
    payee, _payee_user = await authed_client_factory()
    payee_account = await _open_account(payee, "Payee")
    face_token = await enroll_face(payer_user.id)

    await _pay(payer, payer_account, payee_account, 4_000, "u1", beneficiary_name="Ana", face_token=face_token)
    await _pay(payer, payer_account, payee_account, 4_000, "u2", beneficiary_name="Ana")

    contacts = (await payer.get("/api/v1/beneficiaries")).json()
    assert contacts[0]["is_subscription"] is False

    resp = await _pay(payer, payer_account, payee_account, 6_000, "u3", beneficiary_name="Ana")
    assert resp.status_code == 201, resp.text


async def test_payments_and_beneficiaries_require_authentication(client):
    resp = await client.get("/api/v1/payments")
    assert resp.status_code == 401

    resp = await client.get("/api/v1/beneficiaries")
    assert resp.status_code == 401
