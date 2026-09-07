from app.modules.accounts.service import OPENING_BALANCE_MINOR
from app.modules.cards.card_numbers import luhn_is_valid


async def _open_account(client, name="Checking", currency="USD"):
    resp = await client.post("/api/v1/accounts", json={"name": name, "currency": currency})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_issue_card_returns_full_number(authed_client):
    client, _user = authed_client
    account = await _open_account(client)

    resp = await client.post("/api/v1/cards", json={"account_id": account["id"]})
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert luhn_is_valid(body["card_number"])
    assert len(body["card_number"]) == 16
    assert body["card_number"].endswith(body["last4"])
    assert 1 <= body["expiry_month"] <= 12
    assert body["expiry_year"] > 2026
    assert len(body["cvv"]) == 3
    assert body["status"] == "active"
    assert body["account_id"] == account["id"]


async def test_list_cards_includes_full_number(authed_client):
    """Full card number/expiry/CVV are persisted and stay viewable from the
    list - a deliberate departure from real-world PAN/CVV storage practice,
    made because these are fake numbers with no real card network behind
    them (see card_numbers.py docstring)."""
    client, _user = authed_client
    account = await _open_account(client)
    issued = (await client.post("/api/v1/cards", json={"account_id": account["id"]})).json()

    resp = await client.get("/api/v1/cards")
    assert resp.status_code == 200
    cards = resp.json()
    assert len(cards) == 1
    assert cards[0]["card_number"] == issued["card_number"]
    assert cards[0]["cvv"] == issued["cvv"]
    assert len(cards[0]["last4"]) == 4


async def test_cancel_card(authed_client):
    client, _user = authed_client
    account = await _open_account(client)
    issued = (await client.post("/api/v1/cards", json={"account_id": account["id"]})).json()

    resp = await client.delete(f"/api/v1/cards/{issued['id']}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # Cancelling again is a harmless no-op, not an error.
    resp = await client.delete(f"/api/v1/cards/{issued['id']}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # Still listed (audit trail), just shown as cancelled.
    cards = (await client.get("/api/v1/cards")).json()
    assert cards[0]["status"] == "cancelled"


async def test_cancel_unknown_card_is_404(authed_client):
    client, _user = authed_client
    resp = await client.delete("/api/v1/cards/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_issue_card_rejects_closed_account(authed_client):
    client, _user = authed_client
    account = await _open_account(client)
    sink = await _open_account(client, name="Sink")

    # New accounts start with a welcome balance (see accounts/service.py),
    # so it has to be drained before the account is empty enough to close.
    drain = await client.post(
        "/api/v1/transfers",
        json={
            "from_account_id": account["id"],
            "to_account_id": sink["id"],
            "amount_minor": OPENING_BALANCE_MINOR,
            "currency": "USD",
        },
        headers={"Idempotency-Key": "drain-opening-balance"},
    )
    assert drain.status_code == 201, drain.text

    close = await client.post(f"/api/v1/accounts/{account['id']}/close")
    assert close.status_code == 200, close.text

    resp = await client.post("/api/v1/cards", json={"account_id": account["id"]})
    assert resp.status_code == 409


async def test_issue_card_rejects_unowned_account(authed_client, authed_client_factory):
    owner_client, _owner = authed_client
    account = await _open_account(owner_client)

    other_client, _other = await authed_client_factory()
    resp = await other_client.post("/api/v1/cards", json={"account_id": account["id"]})
    assert resp.status_code == 404


async def test_cards_require_authentication(client):
    resp = await client.get("/api/v1/cards")
    assert resp.status_code == 401


async def test_freeze_and_unfreeze_card(authed_client):
    client, _user = authed_client
    account = await _open_account(client)
    issued = (await client.post("/api/v1/cards", json={"account_id": account["id"]})).json()

    freeze_resp = await client.post(f"/api/v1/cards/{issued['id']}/freeze")
    assert freeze_resp.status_code == 200, freeze_resp.text
    assert freeze_resp.json()["status"] == "frozen"

    # Still listed as frozen.
    cards = (await client.get("/api/v1/cards")).json()
    assert cards[0]["status"] == "frozen"

    unfreeze_resp = await client.post(f"/api/v1/cards/{issued['id']}/unfreeze")
    assert unfreeze_resp.status_code == 200, unfreeze_resp.text
    assert unfreeze_resp.json()["status"] == "active"


async def test_freeze_cancelled_card_is_rejected(authed_client):
    client, _user = authed_client
    account = await _open_account(client)
    issued = (await client.post("/api/v1/cards", json={"account_id": account["id"]})).json()
    await client.delete(f"/api/v1/cards/{issued['id']}")

    resp = await client.post(f"/api/v1/cards/{issued['id']}/freeze")
    assert resp.status_code == 422


async def test_freeze_unowned_card_is_404(authed_client, authed_client_factory):
    owner_client, _owner = authed_client
    account = await _open_account(owner_client)
    issued = (await owner_client.post("/api/v1/cards", json={"account_id": account["id"]})).json()

    other_client, _other = await authed_client_factory()
    resp = await other_client.post(f"/api/v1/cards/{issued['id']}/freeze")
    assert resp.status_code == 404


async def test_update_spending_limit(authed_client):
    client, _user = authed_client
    account = await _open_account(client)
    issued = (
        await client.post(
            "/api/v1/cards", json={"account_id": account["id"], "spending_limit_minor": 10_000}
        )
    ).json()
    assert issued["spending_limit_minor"] == 10_000

    resp = await client.patch(
        f"/api/v1/cards/{issued['id']}/spending-limit", json={"spending_limit_minor": 50_000}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["spending_limit_minor"] == 50_000

    # null removes the limit entirely.
    resp = await client.patch(
        f"/api/v1/cards/{issued['id']}/spending-limit", json={"spending_limit_minor": None}
    )
    assert resp.status_code == 200
    assert resp.json()["spending_limit_minor"] is None
