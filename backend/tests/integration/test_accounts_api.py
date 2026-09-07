from app.modules.accounts.service import OPENING_BALANCE_MINOR
from app.modules.auth.validation import validate_iban


async def test_open_list_get_account(authed_client):
    client, _user = authed_client

    resp = await client.post("/api/v1/accounts", json={"name": "Checking", "currency": "usd"})
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["name"] == "Checking"
    assert created["currency"] == "USD"
    assert created["status"] == "active"
    # Every new account starts with a welcome balance (see accounts/service.py).
    assert created["balance_minor"] == OPENING_BALANCE_MINOR
    assert validate_iban(created["iban"])

    resp = await client.get("/api/v1/accounts")
    assert resp.status_code == 200
    accounts = resp.json()
    assert len(accounts) == 1
    assert accounts[0]["id"] == created["id"]

    resp = await client.get(f"/api/v1/accounts/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


async def test_open_account_rejects_bad_currency(authed_client):
    client, _user = authed_client
    resp = await client.post("/api/v1/accounts", json={"name": "Bad", "currency": "US"})
    assert resp.status_code == 422


async def test_close_account_requires_zero_balance(authed_client, seed_balance_factory):
    client, _user = authed_client
    resp = await client.post("/api/v1/accounts", json={"name": "Checking", "currency": "USD"})
    account_id = resp.json()["id"]
    await seed_balance_factory(account_id, 500)

    resp = await client.post(f"/api/v1/accounts/{account_id}/close")
    assert resp.status_code == 409

    resp = await client.get(f"/api/v1/accounts/{account_id}")
    assert resp.json()["status"] == "active"


async def test_close_account_succeeds_once_balance_is_zero(authed_client):
    client, _user = authed_client
    account = (
        await client.post("/api/v1/accounts", json={"name": "To Close", "currency": "USD"})
    ).json()
    sink = (
        await client.post("/api/v1/accounts", json={"name": "Sink", "currency": "USD"})
    ).json()

    # New accounts start with a welcome balance (see accounts/service.py) -
    # has to be moved out before the account is empty enough to close.
    resp = await client.post(
        "/api/v1/transfers",
        json={
            "from_account_id": account["id"],
            "to_account_id": sink["id"],
            "amount_minor": OPENING_BALANCE_MINOR,
            "currency": "USD",
        },
        headers={"Idempotency-Key": "drain-opening-balance"},
    )
    assert resp.status_code == 201, resp.text

    resp = await client.post(f"/api/v1/accounts/{account['id']}/close")
    assert resp.status_code == 200
    assert resp.json()["status"] == "closed"


async def test_accounts_require_authentication(client):
    resp = await client.get("/api/v1/accounts")
    assert resp.status_code == 401


async def test_expired_session_is_rejected(client, user_factory, session_token_factory):
    from app.config import settings

    user = await user_factory()
    token = await session_token_factory(user, expired=True)
    client.cookies.set(settings.SESSION_COOKIE_NAME, token)

    resp = await client.get("/api/v1/accounts")
    assert resp.status_code == 401


async def test_open_account_without_referral_bonus_starts_at_zero(
    authed_client_factory, user_factory
):
    user = await user_factory(referral_bonus_eligible=False)
    client, _user = await authed_client_factory(user)

    resp = await client.post("/api/v1/accounts", json={"name": "Checking", "currency": "USD"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["balance_minor"] == 0


async def test_cannot_see_another_users_account(authed_client, authed_client_factory):
    owner_client, _owner = authed_client
    resp = await owner_client.post("/api/v1/accounts", json={"name": "Private", "currency": "USD"})
    account_id = resp.json()["id"]

    other_client, _other = await authed_client_factory()
    resp = await other_client.get(f"/api/v1/accounts/{account_id}")
    assert resp.status_code == 404

    resp = await other_client.get("/api/v1/accounts")
    assert resp.json() == []
