from app.modules.accounts.service import OPENING_BALANCE_MINOR


async def test_create_transfer_moves_balance(authed_client, seed_balance_factory):
    client, _user = authed_client
    a = (await client.post("/api/v1/accounts", json={"name": "A", "currency": "USD"})).json()
    b = (await client.post("/api/v1/accounts", json={"name": "B", "currency": "USD"})).json()
    await seed_balance_factory(a["id"], 10_000)

    resp = await client.post(
        "/api/v1/transfers",
        json={
            "from_account_id": a["id"],
            "to_account_id": b["id"],
            "amount_minor": 2_500,
            "currency": "USD",
        },
        headers={"Idempotency-Key": "transfer-1"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["amount_minor"] == 2_500
    assert body["status"] == "completed"

    # Only the user's FIRST account ever gets the welcome balance (see
    # accounts/service.py) - "a" was opened first, "b" second, so "b"
    # starts at zero.
    a_after = (await client.get(f"/api/v1/accounts/{a['id']}")).json()
    b_after = (await client.get(f"/api/v1/accounts/{b['id']}")).json()
    assert a_after["balance_minor"] == OPENING_BALANCE_MINOR + 10_000 - 2_500
    assert b_after["balance_minor"] == 2_500


async def test_create_transfer_notifies_the_owner_with_the_money_category(
    authed_client, seed_balance_factory
):
    """A transfer moves money into an account just as much as a payment
    does - it gets the same real-time pop-up/animation on the frontend
    (category="money_received", see notifications/bus.py), even though
    transfers are strictly own-account-to-own-account (see
    transfers/service.py's module docstring) rather than cross-user."""
    client, _user = authed_client
    a = (await client.post("/api/v1/accounts", json={"name": "A", "currency": "USD"})).json()
    b = (await client.post("/api/v1/accounts", json={"name": "B", "currency": "USD"})).json()
    await seed_balance_factory(a["id"], 10_000)

    resp = await client.post(
        "/api/v1/transfers",
        json={
            "from_account_id": a["id"],
            "to_account_id": b["id"],
            "amount_minor": 2_500,
            "currency": "USD",
        },
        headers={"Idempotency-Key": "transfer-notif-1"},
    )
    assert resp.status_code == 201, resp.text

    notifications = (await client.get("/api/v1/notifications")).json()
    assert len(notifications) == 1
    assert notifications[0]["category"] == "money_received"
    assert notifications[0]["title"] == "Ai primit bani"


async def test_transfer_requires_idempotency_key(authed_client, seed_balance_factory):
    client, _user = authed_client
    a = (await client.post("/api/v1/accounts", json={"name": "A", "currency": "USD"})).json()
    b = (await client.post("/api/v1/accounts", json={"name": "B", "currency": "USD"})).json()
    await seed_balance_factory(a["id"], 10_000)

    resp = await client.post(
        "/api/v1/transfers",
        json={
            "from_account_id": a["id"],
            "to_account_id": b["id"],
            "amount_minor": 100,
            "currency": "USD",
        },
    )
    assert resp.status_code == 400


async def test_transfer_insufficient_funds(authed_client):
    client, _user = authed_client
    a = (await client.post("/api/v1/accounts", json={"name": "A", "currency": "USD"})).json()
    b = (await client.post("/api/v1/accounts", json={"name": "B", "currency": "USD"})).json()

    # Every new account starts with a welcome balance (see
    # accounts/service.py), so the request has to exceed that too.
    resp = await client.post(
        "/api/v1/transfers",
        json={
            "from_account_id": a["id"],
            "to_account_id": b["id"],
            "amount_minor": OPENING_BALANCE_MINOR + 100,
            "currency": "USD",
        },
        headers={"Idempotency-Key": "k1"},
    )
    assert resp.status_code == 422


async def test_transfer_same_account_rejected(authed_client, seed_balance_factory):
    client, _user = authed_client
    a = (await client.post("/api/v1/accounts", json={"name": "A", "currency": "USD"})).json()
    await seed_balance_factory(a["id"], 10_000)

    resp = await client.post(
        "/api/v1/transfers",
        json={
            "from_account_id": a["id"],
            "to_account_id": a["id"],
            "amount_minor": 100,
            "currency": "USD",
        },
        headers={"Idempotency-Key": "k1"},
    )
    assert resp.status_code == 422


async def test_transfer_to_unowned_account_not_found(
    authed_client, authed_client_factory, seed_balance_factory
):
    owner_client, _owner = authed_client
    a = (await owner_client.post("/api/v1/accounts", json={"name": "A", "currency": "USD"})).json()
    await seed_balance_factory(a["id"], 10_000)

    other_client, _other = await authed_client_factory()
    other_account = (
        await other_client.post("/api/v1/accounts", json={"name": "Other", "currency": "USD"})
    ).json()

    resp = await owner_client.post(
        "/api/v1/transfers",
        json={
            "from_account_id": a["id"],
            "to_account_id": other_account["id"],
            "amount_minor": 100,
            "currency": "USD",
        },
        headers={"Idempotency-Key": "k1"},
    )
    assert resp.status_code == 404


async def test_repeated_idempotency_key_returns_same_transfer(authed_client, seed_balance_factory):
    client, _user = authed_client
    a = (await client.post("/api/v1/accounts", json={"name": "A", "currency": "USD"})).json()
    b = (await client.post("/api/v1/accounts", json={"name": "B", "currency": "USD"})).json()
    await seed_balance_factory(a["id"], 10_000)

    payload = {
        "from_account_id": a["id"],
        "to_account_id": b["id"],
        "amount_minor": 1_000,
        "currency": "USD",
    }
    headers = {"Idempotency-Key": "same-key"}

    first = await client.post("/api/v1/transfers", json=payload, headers=headers)
    second = await client.post("/api/v1/transfers", json=payload, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    a_after = (await client.get(f"/api/v1/accounts/{a['id']}")).json()
    assert a_after["balance_minor"] == OPENING_BALANCE_MINOR + 9_000  # only debited once


async def test_get_and_list_transfers(authed_client, seed_balance_factory):
    client, _user = authed_client
    a = (await client.post("/api/v1/accounts", json={"name": "A", "currency": "USD"})).json()
    b = (await client.post("/api/v1/accounts", json={"name": "B", "currency": "USD"})).json()
    await seed_balance_factory(a["id"], 10_000)

    created = (
        await client.post(
            "/api/v1/transfers",
            json={
                "from_account_id": a["id"],
                "to_account_id": b["id"],
                "amount_minor": 500,
                "currency": "USD",
            },
            headers={"Idempotency-Key": "k1"},
        )
    ).json()

    resp = await client.get(f"/api/v1/transfers/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]

    resp = await client.get("/api/v1/transfers")
    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert created["id"] in ids
