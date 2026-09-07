"""GET /api/v1/insights/spending-by-category - the REST path the dashboard
widget uses, as opposed to the chat-only categorize_transactions tool."""

import uuid
from datetime import UTC, datetime, timedelta


def _iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).date().isoformat()


async def _seed_entry(
    supabase,
    account_id,
    amount_minor: int,
    *,
    direction: str = "debit",
    description: str = "Test entry",
) -> None:
    journal = (
        await supabase.table("journal_transactions")
        .insert(
            {
                "reference": "TEST-INSIGHTS",
                "idempotency_key": f"test-insights-{uuid.uuid4()}",
                "description": description,
            }
        )
        .execute()
    ).data[0]
    await supabase.table("ledger_entries").insert(
        {
            "journal_id": journal["id"],
            "account_id": str(account_id),
            "direction": direction,
            "amount_minor": amount_minor,
            "currency": "RON",
        }
    ).execute()


async def test_spending_by_category_requires_authentication(client):
    resp = await client.get(
        "/api/v1/insights/spending-by-category",
        params={"start_date": _iso(5), "end_date": _iso(0)},
    )
    assert resp.status_code == 401


async def test_spending_by_category_returns_real_totals(authed_client, account_factory, supabase):
    client, user = authed_client
    account = await account_factory(user)
    await _seed_entry(supabase, account["id"], 3_000, description="Netflix abonament")
    await _seed_entry(supabase, account["id"], 15_000, description="Lidl Cluj")

    resp = await client.get(
        "/api/v1/insights/spending-by-category",
        params={"start_date": _iso(5), "end_date": _iso(0)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    by_name = {c["name"]: c for c in body["categories"]}
    assert by_name["Divertisment"]["total_minor"] == 3_000
    assert by_name["Cumpărături alimentare"]["total_minor"] == 15_000
    assert body["total_transactions"] == 2


async def test_spending_by_category_rejects_inverted_range(authed_client):
    client, _user = authed_client
    resp = await client.get(
        "/api/v1/insights/spending-by-category",
        params={"start_date": _iso(0), "end_date": _iso(5)},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
