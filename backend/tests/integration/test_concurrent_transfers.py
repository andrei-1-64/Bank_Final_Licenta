"""Sprint 1's Definition of Done, verified: concurrent transfers must never
create or destroy money, and must never let a debit succeed past the
account's real balance. These tests fire genuinely concurrent HTTP
requests (asyncio.gather), each getting its own DB session/connection (see
conftest.py's NullPool-backed test engine), so the row locking in
ledger/service.py is exercised against real Postgres, not simulated.
"""

import asyncio

from app.modules.accounts.service import OPENING_BALANCE_MINOR


async def _open_account(client, name: str, currency: str = "USD") -> dict:
    resp = await client.post("/api/v1/accounts", json={"name": name, "currency": currency})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _open_account_with_zero_balance(client, name: str, currency: str = "USD") -> dict:
    """New accounts start with a welcome balance (see accounts/service.py) -
    drain it to a disposable sink account so tests that need a precise,
    known starting balance can still reason about exact amounts."""
    account = await _open_account(client, name, currency)
    sink = await _open_account(client, f"{name}-sink", currency)
    resp = await _transfer(
        client, account["id"], sink["id"], OPENING_BALANCE_MINOR, f"drain-{account['id']}", currency
    )
    assert resp.status_code == 201, resp.text
    return account


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


async def test_concurrent_transfers_conserve_total_balance(authed_client, seed_balance_factory):
    client, _user = authed_client
    a = await _open_account(client, "A")
    b = await _open_account(client, "B")
    await seed_balance_factory(a["id"], 100_000)

    n = 20
    amount = 1_000  # comfortable headroom: 20 * 1000 = 20_000 <= 100_000

    responses = await asyncio.gather(
        *[_transfer(client, a["id"], b["id"], amount, f"concurrent-{i}") for i in range(n)]
    )

    failures = [r.text for r in responses if r.status_code != 201]
    assert not failures, failures

    a_after = (await client.get(f"/api/v1/accounts/{a['id']}")).json()
    b_after = (await client.get(f"/api/v1/accounts/{b['id']}")).json()

    # Only the user's first-ever account gets the welcome balance (see
    # accounts/service.py) - "a" was opened first, "b" second.
    assert a_after["balance_minor"] == OPENING_BALANCE_MINOR + 100_000 - n * amount
    assert b_after["balance_minor"] == n * amount
    assert (
        a_after["balance_minor"] + b_after["balance_minor"]
        == OPENING_BALANCE_MINOR + 100_000
    )


async def test_concurrent_transfers_cannot_overdraw(authed_client, seed_balance_factory):
    """The double-spend proof: seed exactly enough balance for K transfers,
    fire M > K concurrent requests for the same amount, and prove that
    EXACTLY K succeed, the rest are rejected with insufficient_funds, and
    the balance never goes negative - i.e. the account row lock actually
    serializes concurrent debits instead of letting them race past the
    balance check."""
    client, _user = authed_client
    a = await _open_account_with_zero_balance(client, "A")
    b = await _open_account(client, "B")

    amount = 1_000
    affordable = 5
    await seed_balance_factory(a["id"], amount * affordable)

    attempts = affordable * 3  # far more requests than the account can afford

    responses = await asyncio.gather(
        *[_transfer(client, a["id"], b["id"], amount, f"overdraw-{i}") for i in range(attempts)]
    )

    succeeded = [r for r in responses if r.status_code == 201]
    failed = [r for r in responses if r.status_code == 422]

    assert len(succeeded) == affordable, [r.text for r in responses]
    assert len(failed) == attempts - affordable
    assert all(r.json()["error"]["code"] == "insufficient_funds" for r in failed)

    a_after = (await client.get(f"/api/v1/accounts/{a['id']}")).json()
    b_after = (await client.get(f"/api/v1/accounts/{b['id']}")).json()

    assert a_after["balance_minor"] == 0  # drained exactly, never negative
    # b is this user's 3rd account (after a and a's own drain-sink), so it
    # gets no welcome balance of its own - only the very first account does.
    assert b_after["balance_minor"] == amount * affordable


async def test_concurrent_bidirectional_transfers_do_not_deadlock(
    authed_client, seed_balance_factory
):
    """Fires A->B and B->A transfers concurrently and interleaved - exactly
    the request shape that deadlocks if account row locks aren't acquired
    in a fixed global order (see ledger/service.py::_lock_accounts). No
    timeout/deadlock error, plus balance conservation, is the proof."""
    client, _user = authed_client
    a = await _open_account(client, "A")
    b = await _open_account(client, "B")
    await seed_balance_factory(a["id"], 50_000)
    await seed_balance_factory(b["id"], 50_000)

    amount = 500
    n = 15

    tasks = []
    for i in range(n):
        tasks.append(_transfer(client, a["id"], b["id"], amount, f"a-to-b-{i}"))
        tasks.append(_transfer(client, b["id"], a["id"], amount, f"b-to-a-{i}"))

    responses = await asyncio.gather(*tasks)
    failures = [r.text for r in responses if r.status_code != 201]
    assert not failures, failures

    a_after = (await client.get(f"/api/v1/accounts/{a['id']}")).json()
    b_after = (await client.get(f"/api/v1/accounts/{b['id']}")).json()

    # Equal amounts flowed both ways, so both balances are unchanged from
    # what they started with - "a" (opened first) has its welcome balance
    # plus the 50_000 it was seeded with; "b" (opened second, no welcome
    # balance of its own) has just its 50_000 seed. Total is conserved.
    assert a_after["balance_minor"] == OPENING_BALANCE_MINOR + 50_000
    assert b_after["balance_minor"] == 50_000
    assert (
        a_after["balance_minor"] + b_after["balance_minor"]
        == OPENING_BALANCE_MINOR + 100_000
    )
