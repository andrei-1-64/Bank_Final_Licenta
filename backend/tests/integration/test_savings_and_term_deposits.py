"""Savings ("Cont de economii") and fixed-term deposit ("Cont cu dobândă
fixă") accounts - see accounts/service.py's PRODUCT_* constants and
accrue_interest_if_due for the lazy interest-crediting mechanics this
exercises via direct DB backdating (no real waiting for a month/maturity).
"""

from datetime import UTC, date, datetime, timedelta

from app.modules.accounts.service import (
    OPENING_BALANCE_MINOR,
    SAVINGS_INTEREST_RATE_BPS,
    TERM_DEPOSIT_RATES_BPS,
    _add_months,
)


async def _backdate(supabase, account_id, **fields) -> None:
    await supabase.table("accounts").update(fields).eq("id", account_id).execute()


async def test_account_products_lists_savings_rate_and_term_options(client):
    resp = await client.get("/api/v1/accounts/products")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["savings_interest_rate_bps"] == SAVINGS_INTEREST_RATE_BPS
    assert {o["term_months"] for o in body["term_deposit_options"]} == set(TERM_DEPOSIT_RATES_BPS)


async def test_open_savings_account_sets_rate(authed_client):
    client, _user = authed_client
    resp = await client.post(
        "/api/v1/accounts",
        json={"name": "Economii", "currency": "RON", "product_type": "savings"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["product_type"] == "savings"
    assert body["interest_rate_bps"] == SAVINGS_INTEREST_RATE_BPS
    assert body["term_months"] is None
    assert body["maturity_date"] is None


async def test_open_term_deposit_sets_rate_and_maturity(authed_client):
    client, _user = authed_client
    resp = await client.post(
        "/api/v1/accounts",
        json={
            "name": "Depozit 12 luni",
            "currency": "RON",
            "product_type": "term_deposit",
            "term_months": 12,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["product_type"] == "term_deposit"
    assert body["interest_rate_bps"] == TERM_DEPOSIT_RATES_BPS[12]
    assert body["term_months"] == 12
    assert body["maturity_date"] == _add_months(date.today(), 12).isoformat()


async def test_open_term_deposit_rejects_unknown_term(authed_client):
    client, _user = authed_client
    resp = await client.post(
        "/api/v1/accounts",
        json={
            "name": "Depozit invalid",
            "currency": "RON",
            "product_type": "term_deposit",
            "term_months": 7,
        },
    )
    assert resp.status_code == 422, resp.text


async def test_open_account_rejects_unknown_product_type(authed_client):
    client, _user = authed_client
    resp = await client.post(
        "/api/v1/accounts",
        json={"name": "???", "currency": "RON", "product_type": "crypto"},
    )
    assert resp.status_code == 422


async def test_term_deposit_blocks_outgoing_transfer_before_maturity(authed_client):
    client, _user = authed_client
    deposit = (
        await client.post(
            "/api/v1/accounts",
            json={
                "name": "Depozit",
                "currency": "USD",
                "product_type": "term_deposit",
                "term_months": 3,
            },
        )
    ).json()
    sink = (
        await client.post("/api/v1/accounts", json={"name": "Sink", "currency": "USD"})
    ).json()

    resp = await client.post(
        "/api/v1/transfers",
        json={
            "from_account_id": deposit["id"],
            "to_account_id": sink["id"],
            "amount_minor": 100,
            "currency": "USD",
        },
        headers={"Idempotency-Key": "term-deposit-lock-1"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "term_deposit_locked"


async def test_term_deposit_still_accepts_incoming_transfer(authed_client):
    client, _user = authed_client
    checking = (
        await client.post("/api/v1/accounts", json={"name": "Curent", "currency": "USD"})
    ).json()
    deposit = (
        await client.post(
            "/api/v1/accounts",
            json={
                "name": "Depozit",
                "currency": "USD",
                "product_type": "term_deposit",
                "term_months": 3,
            },
        )
    ).json()

    resp = await client.post(
        "/api/v1/transfers",
        json={
            "from_account_id": checking["id"],
            "to_account_id": deposit["id"],
            "amount_minor": 500,
            "currency": "USD",
        },
        headers={"Idempotency-Key": "term-deposit-funding-1"},
    )
    assert resp.status_code == 201, resp.text


async def test_term_deposit_blocks_close_before_maturity(authed_client):
    # The lock check runs before the zero-balance check in close_account,
    # so this is blocked as locked regardless of the account's balance
    # (including the referral welcome balance authed_client's user gets).
    client, _user = authed_client
    deposit = (
        await client.post(
            "/api/v1/accounts",
            json={
                "name": "Depozit gol",
                "currency": "USD",
                "product_type": "term_deposit",
                "term_months": 3,
            },
        )
    ).json()

    resp = await client.post(f"/api/v1/accounts/{deposit['id']}/close")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "term_deposit_locked"


async def test_term_deposit_unlocks_after_maturity(authed_client, supabase):
    client, _user = authed_client
    deposit = (
        await client.post(
            "/api/v1/accounts",
            json={
                "name": "Depozit matur",
                "currency": "USD",
                "product_type": "term_deposit",
                "term_months": 3,
            },
        )
    ).json()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    await _backdate(supabase, deposit["id"], maturity_date=yesterday)

    sink = (
        await client.post("/api/v1/accounts", json={"name": "Sink", "currency": "USD"})
    ).json()
    resp = await client.post(
        "/api/v1/transfers",
        json={
            "from_account_id": deposit["id"],
            "to_account_id": sink["id"],
            "amount_minor": 100,
            "currency": "USD",
        },
        headers={"Idempotency-Key": "term-deposit-unlocked-1"},
    )
    assert resp.status_code == 201, resp.text


async def test_term_deposit_credits_interest_lump_sum_at_maturity(authed_client, supabase):
    client, _user = authed_client
    deposit = (
        await client.post(
            "/api/v1/accounts",
            json={
                "name": "Depozit dobandit",
                "currency": "USD",
                "product_type": "term_deposit",
                "term_months": 12,
            },
        )
    ).json()
    # authed_client's user is referral-eligible by default (see conftest.py).
    assert deposit["balance_minor"] == OPENING_BALANCE_MINOR

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    await _backdate(supabase, deposit["id"], maturity_date=yesterday)

    resp = await client.get(f"/api/v1/accounts/{deposit['id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    rate_bps = TERM_DEPOSIT_RATES_BPS[12]
    expected_interest = OPENING_BALANCE_MINOR * rate_bps * 12 // (12 * 10_000)
    assert body["balance_minor"] == OPENING_BALANCE_MINOR + expected_interest

    # Idempotent: reading it again doesn't pay the lump sum twice.
    resp = await client.get(f"/api/v1/accounts/{deposit['id']}")
    assert resp.json()["balance_minor"] == body["balance_minor"]


async def test_savings_account_accrues_monthly_interest(authed_client, supabase):
    client, _user = authed_client
    savings = (
        await client.post(
            "/api/v1/accounts",
            json={"name": "Economii", "currency": "USD", "product_type": "savings"},
        )
    ).json()
    opened_at = datetime.now(UTC) - timedelta(days=95)  # a little over 3 full months
    await _backdate(supabase, savings["id"], created_at=opened_at.isoformat())

    resp = await client.get(f"/api/v1/accounts/{savings['id']}")
    assert resp.status_code == 200, resp.text
    balance_after = resp.json()["balance_minor"]
    assert balance_after > OPENING_BALANCE_MINOR  # interest was actually credited

    # Idempotent: re-reading doesn't re-credit the same months again.
    resp = await client.get(f"/api/v1/accounts/{savings['id']}")
    assert resp.json()["balance_minor"] == balance_after
