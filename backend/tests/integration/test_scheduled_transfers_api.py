"""POST/GET /api/v1/scheduled-transfers and its lazy execution, triggered
from GET /accounts (see accounts/router.py::list_accounts and
scheduled_transfers/service.py's module docstring - no cron, same pattern
as savings/term-deposit interest accrual)."""

from datetime import UTC, datetime, timedelta

from app.modules.accounts.service import OPENING_BALANCE_MINOR


async def _open_two_accounts(client, currency="USD"):
    a = (await client.post("/api/v1/accounts", json={"name": "A", "currency": currency})).json()
    b = (await client.post("/api/v1/accounts", json={"name": "B", "currency": currency})).json()
    return a, b


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat()


async def test_create_scheduled_transfer(authed_client):
    client, _user = authed_client
    a, b = await _open_two_accounts(client)

    resp = await client.post(
        "/api/v1/scheduled-transfers",
        json={
            "from_account_id": a["id"],
            "to_account_id": b["id"],
            "amount_minor": 500,
            "currency": "USD",
            "frequency": "monthly",
            "start_at": _iso(timedelta(days=7)),
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert body["frequency"] == "monthly"
    assert body["amount_minor"] == 500


async def test_create_scheduled_transfer_rejects_same_account(authed_client):
    client, _user = authed_client
    a, _b = await _open_two_accounts(client)

    resp = await client.post(
        "/api/v1/scheduled-transfers",
        json={
            "from_account_id": a["id"],
            "to_account_id": a["id"],
            "amount_minor": 500,
            "currency": "USD",
            "start_at": _iso(timedelta(days=1)),
        },
    )
    assert resp.status_code == 422


async def test_create_scheduled_transfer_rejects_currency_mismatch(authed_client):
    client, _user = authed_client
    a, b = await _open_two_accounts(client, currency="USD")
    b["currency"] = "EUR"  # doesn't change the real account - the request just claims EUR

    resp = await client.post(
        "/api/v1/scheduled-transfers",
        json={
            "from_account_id": a["id"],
            "to_account_id": b["id"],
            "amount_minor": 500,
            "currency": "EUR",
            "start_at": _iso(timedelta(days=1)),
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "currency_mismatch"


async def test_due_one_time_transfer_executes_on_next_accounts_read(authed_client):
    client, _user = authed_client
    a, b = await _open_two_accounts(client)

    resp = await client.post(
        "/api/v1/scheduled-transfers",
        json={
            "from_account_id": a["id"],
            "to_account_id": b["id"],
            "amount_minor": 500,
            "currency": "USD",
            "start_at": _iso(-timedelta(minutes=1)),  # already due
        },
    )
    scheduled = resp.json()

    # The lazy trigger: GET /accounts, not a background job.
    accounts = (await client.get("/api/v1/accounts")).json()
    a_after = next(acc for acc in accounts if acc["id"] == a["id"])
    b_after = next(acc for acc in accounts if acc["id"] == b["id"])
    assert a_after["balance_minor"] == OPENING_BALANCE_MINOR - 500
    assert b_after["balance_minor"] == 500  # "b" is this user's 2nd account, no welcome balance

    listed = (await client.get("/api/v1/scheduled-transfers")).json()
    row = next(s for s in listed if s["id"] == scheduled["id"])
    assert row["status"] == "completed"
    assert row["last_run_at"] is not None

    # Idempotent: a second GET /accounts must not fire it again.
    accounts_again = (await client.get("/api/v1/accounts")).json()
    a_again = next(acc for acc in accounts_again if acc["id"] == a["id"])
    assert a_again["balance_minor"] == OPENING_BALANCE_MINOR - 500


async def test_due_recurring_transfer_reschedules_instead_of_completing(authed_client):
    client, _user = authed_client
    a, b = await _open_two_accounts(client)

    created = (
        await client.post(
            "/api/v1/scheduled-transfers",
            json={
                "from_account_id": a["id"],
                "to_account_id": b["id"],
                "amount_minor": 500,
                "currency": "USD",
                "frequency": "weekly",
                "start_at": _iso(-timedelta(minutes=1)),
            },
        )
    ).json()
    original_next_run = created["next_run_at"]

    await client.get("/api/v1/accounts")

    listed = (await client.get("/api/v1/scheduled-transfers")).json()
    row = next(s for s in listed if s["id"] == created["id"])
    assert row["status"] == "active"
    assert row["next_run_at"] > original_next_run
    assert row["last_run_at"] is not None


async def test_not_due_transfer_does_not_execute(authed_client):
    client, _user = authed_client
    a, b = await _open_two_accounts(client)

    await client.post(
        "/api/v1/scheduled-transfers",
        json={
            "from_account_id": a["id"],
            "to_account_id": b["id"],
            "amount_minor": 500,
            "currency": "USD",
            "start_at": _iso(timedelta(days=30)),
        },
    )

    accounts = (await client.get("/api/v1/accounts")).json()
    a_after = next(acc for acc in accounts if acc["id"] == a["id"])
    assert a_after["balance_minor"] == OPENING_BALANCE_MINOR


async def test_due_transfer_with_insufficient_funds_pauses_with_error(authed_client):
    client, _user = authed_client
    a, b = await _open_two_accounts(client)

    created = (
        await client.post(
            "/api/v1/scheduled-transfers",
            json={
                "from_account_id": a["id"],
                "to_account_id": b["id"],
                "amount_minor": OPENING_BALANCE_MINOR * 100,  # far more than available
                "currency": "USD",
                "start_at": _iso(-timedelta(minutes=1)),
            },
        )
    ).json()

    await client.get("/api/v1/accounts")

    listed = (await client.get("/api/v1/scheduled-transfers")).json()
    row = next(s for s in listed if s["id"] == created["id"])
    assert row["status"] == "paused"
    assert row["last_error"]


async def test_cancel_scheduled_transfer(authed_client):
    client, _user = authed_client
    a, b = await _open_two_accounts(client)
    created = (
        await client.post(
            "/api/v1/scheduled-transfers",
            json={
                "from_account_id": a["id"],
                "to_account_id": b["id"],
                "amount_minor": 500,
                "currency": "USD",
                "start_at": _iso(timedelta(days=1)),
            },
        )
    ).json()

    resp = await client.post(f"/api/v1/scheduled-transfers/{created['id']}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # Cancelled means it never fires, even once it's technically "due".
    resp2 = await client.post(f"/api/v1/scheduled-transfers/{created['id']}/cancel")
    assert resp2.status_code == 422


async def test_pause_and_resume_scheduled_transfer(authed_client):
    client, _user = authed_client
    a, b = await _open_two_accounts(client)
    created = (
        await client.post(
            "/api/v1/scheduled-transfers",
            json={
                "from_account_id": a["id"],
                "to_account_id": b["id"],
                "amount_minor": 500,
                "currency": "USD",
                "start_at": _iso(-timedelta(minutes=1)),
            },
        )
    ).json()

    pause_resp = await client.post(f"/api/v1/scheduled-transfers/{created['id']}/pause")
    assert pause_resp.status_code == 200
    assert pause_resp.json()["status"] == "paused"

    # Paused, so due-or-not doesn't matter - it must not fire.
    await client.get("/api/v1/accounts")
    listed = (await client.get("/api/v1/scheduled-transfers")).json()
    assert next(s for s in listed if s["id"] == created["id"])["status"] == "paused"

    resume_resp = await client.post(f"/api/v1/scheduled-transfers/{created['id']}/resume")
    assert resume_resp.status_code == 200
    assert resume_resp.json()["status"] == "active"

    await client.get("/api/v1/accounts")
    listed_after = (await client.get("/api/v1/scheduled-transfers")).json()
    assert next(s for s in listed_after if s["id"] == created["id"])["status"] == "completed"


async def test_scheduled_transfers_require_authentication(client):
    resp = await client.get("/api/v1/scheduled-transfers")
    assert resp.status_code == 401
