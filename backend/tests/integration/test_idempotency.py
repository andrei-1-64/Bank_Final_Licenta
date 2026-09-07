"""Idempotency-Key proof: repeated requests (concurrent or sequential) with
the same key must produce exactly one ledger effect, while different keys
remain genuinely independent operations."""

import asyncio

from app.modules.accounts.service import OPENING_BALANCE_MINOR


async def _transfer(client, from_id, to_id, amount_minor, idem_key, currency="USD"):
    return await client.post(
        "/api/v1/transfers",
        json={
            "from_account_id": from_id,
            "to_account_id": to_id,
            "amount_minor": amount_minor,
            "currency": currency,
        },
        headers={"Idempotency-Key": idem_key},
    )


async def test_concurrent_identical_requests_apply_once(authed_client, seed_balance_factory):
    client, _user = authed_client
    a = (await client.post("/api/v1/accounts", json={"name": "A", "currency": "USD"})).json()
    b = (await client.post("/api/v1/accounts", json={"name": "B", "currency": "USD"})).json()
    await seed_balance_factory(a["id"], 10_000)

    n = 10
    key = "same-key-for-all"

    responses = await asyncio.gather(
        *[_transfer(client, a["id"], b["id"], 1_000, key) for _ in range(n)]
    )

    failures = [r.text for r in responses if r.status_code != 201]
    assert not failures, failures

    transfer_ids = {r.json()["id"] for r in responses}
    assert len(transfer_ids) == 1  # every caller got back the same transfer

    a_after = (await client.get(f"/api/v1/accounts/{a['id']}")).json()
    b_after = (await client.get(f"/api/v1/accounts/{b['id']}")).json()
    # Only the user's first account ("a") gets the welcome balance.
    assert a_after["balance_minor"] == OPENING_BALANCE_MINOR + 9_000  # debited exactly once, not n times
    assert b_after["balance_minor"] == 1_000


async def test_sequential_repeat_with_same_key_is_a_pure_replay(
    authed_client, seed_balance_factory
):
    client, _user = authed_client
    a = (await client.post("/api/v1/accounts", json={"name": "A", "currency": "USD"})).json()
    b = (await client.post("/api/v1/accounts", json={"name": "B", "currency": "USD"})).json()
    await seed_balance_factory(a["id"], 10_000)

    key = "seq-key"
    first = await _transfer(client, a["id"], b["id"], 1_000, key)
    second = await _transfer(client, a["id"], b["id"], 1_000, key)
    third = await _transfer(client, a["id"], b["id"], 1_000, key)

    assert first.status_code == second.status_code == third.status_code == 201
    ids = {first.json()["id"], second.json()["id"], third.json()["id"]}
    assert len(ids) == 1

    a_after = (await client.get(f"/api/v1/accounts/{a['id']}")).json()
    assert a_after["balance_minor"] == OPENING_BALANCE_MINOR + 9_000


async def test_different_idempotency_keys_apply_independently(authed_client, seed_balance_factory):
    client, _user = authed_client
    a = (await client.post("/api/v1/accounts", json={"name": "A", "currency": "USD"})).json()
    b = (await client.post("/api/v1/accounts", json={"name": "B", "currency": "USD"})).json()
    await seed_balance_factory(a["id"], 10_000)

    first = await _transfer(client, a["id"], b["id"], 1_000, "key-1")
    second = await _transfer(client, a["id"], b["id"], 1_000, "key-2")

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]

    a_after = (await client.get(f"/api/v1/accounts/{a['id']}")).json()
    # debited twice - genuinely different operations.
    assert a_after["balance_minor"] == OPENING_BALANCE_MINOR + 8_000
