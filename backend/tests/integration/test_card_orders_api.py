async def _open_account(client, name="Checking", currency="USD"):
    resp = await client.post("/api/v1/accounts", json={"name": name, "currency": currency})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _order_payload(account_id):
    return {
        "account_id": account_id,
        "full_name": "Andrei Test",
        "phone": "0712345678",
        "address": "Str. Exemplu 1",
        "city": "Bucuresti",
        "postal_code": "010101",
        "country": "Romania",
    }


async def test_create_order_also_issues_a_linked_card(authed_client):
    client, _user = authed_client
    account = await _open_account(client)

    resp = await client.post("/api/v1/card-orders", json=_order_payload(account["id"]))
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["account_id"] == account["id"]
    assert body["status"] == "pending"
    assert body["card_id"] is not None
    assert body["card"]["id"] == body["card_id"]
    assert body["card"]["account_id"] == account["id"]
    assert len(body["card"]["card_number"]) == 16


async def test_list_orders_includes_linked_card(authed_client):
    client, _user = authed_client
    account = await _open_account(client)
    created = (
        await client.post("/api/v1/card-orders", json=_order_payload(account["id"]))
    ).json()

    resp = await client.get("/api/v1/card-orders")
    assert resp.status_code == 200
    orders = resp.json()
    assert len(orders) == 1
    assert orders[0]["id"] == created["id"]
    assert orders[0]["card"]["card_number"] == created["card"]["card_number"]

    cards = (await client.get("/api/v1/cards")).json()
    assert len(cards) == 1
    assert cards[0]["id"] == created["card_id"]


async def test_create_order_with_card_id_reuses_the_existing_virtual_card(authed_client):
    client, _user = authed_client
    account = await _open_account(client)
    card = (
        await client.post("/api/v1/cards", json={"account_id": account["id"]})
    ).json()

    payload = _order_payload(account["id"])
    payload["card_id"] = card["id"]
    resp = await client.post("/api/v1/card-orders", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["card_id"] == card["id"]
    assert body["card"]["card_number"] == card["card_number"]

    # No second card was minted - still just the one.
    cards = (await client.get("/api/v1/cards")).json()
    assert len(cards) == 1


async def test_create_order_with_card_id_rejects_a_card_from_another_account(authed_client):
    client, _user = authed_client
    account = await _open_account(client)
    other_account = await _open_account(client, name="Savings")
    card = (
        await client.post("/api/v1/cards", json={"account_id": other_account["id"]})
    ).json()

    payload = _order_payload(account["id"])
    payload["card_id"] = card["id"]
    resp = await client.post("/api/v1/card-orders", json=payload)
    assert resp.status_code == 422


async def test_create_order_with_card_id_rejects_a_cancelled_card(authed_client):
    client, _user = authed_client
    account = await _open_account(client)
    card = (
        await client.post("/api/v1/cards", json={"account_id": account["id"]})
    ).json()
    await client.delete(f"/api/v1/cards/{card['id']}")

    payload = _order_payload(account["id"])
    payload["card_id"] = card["id"]
    resp = await client.post("/api/v1/card-orders", json=payload)
    assert resp.status_code == 422


async def test_create_order_with_card_id_rejects_a_card_already_ordered(authed_client):
    client, _user = authed_client
    account = await _open_account(client)
    card = (
        await client.post("/api/v1/cards", json={"account_id": account["id"]})
    ).json()

    payload = _order_payload(account["id"])
    payload["card_id"] = card["id"]
    first = await client.post("/api/v1/card-orders", json=payload)
    assert first.status_code == 201, first.text

    second = await client.post("/api/v1/card-orders", json=payload)
    assert second.status_code == 422


async def test_create_order_with_card_id_rejects_an_unowned_card(
    authed_client, authed_client_factory
):
    owner_client, _owner = authed_client
    account = await _open_account(owner_client)

    other_client, _other = await authed_client_factory()
    other_account = await _open_account(other_client)
    other_card = (
        await other_client.post("/api/v1/cards", json={"account_id": other_account["id"]})
    ).json()

    payload = _order_payload(account["id"])
    payload["card_id"] = other_card["id"]
    resp = await owner_client.post("/api/v1/card-orders", json=payload)
    assert resp.status_code == 404


async def test_create_order_rejects_unowned_account(authed_client, authed_client_factory):
    owner_client, _owner = authed_client
    account = await _open_account(owner_client)

    other_client, _other = await authed_client_factory()
    resp = await other_client.post("/api/v1/card-orders", json=_order_payload(account["id"]))
    assert resp.status_code == 404


async def test_card_orders_require_authentication(client):
    resp = await client.get("/api/v1/card-orders")
    assert resp.status_code == 401
