"""Admin panel API.

The point of most of these tests is the GATE, not the payloads: /admin is the
one place in this app that reads across users, so "a normal user cannot reach
it" is the property worth pinning down. `ADMIN_ENDPOINTS` is deliberately the
full list - when a route is added to the module, add it here too, and the
403/401 tests cover it for free.

Requires migration 0016_admin_role.sql on the TEST database (users.role +
admin_totals_by_currency).
"""

import uuid

import pytest
from httpx import AsyncClient as HTTPXAsyncClient
from supabase import AsyncClient

from app.config import settings
from app.modules.users.schemas import UserRead

#: (method, path) for every route in app/modules/admin/router.py.
ADMIN_ENDPOINTS = [
    ("GET", "/api/v1/admin/me"),
    ("GET", "/api/v1/admin/stats"),
    ("GET", "/api/v1/admin/users"),
    ("GET", f"/api/v1/admin/users/{uuid.uuid4()}"),
    ("PATCH", f"/api/v1/admin/users/{uuid.uuid4()}/role"),
    ("PATCH", f"/api/v1/admin/users/{uuid.uuid4()}/blocked"),
    ("POST", f"/api/v1/admin/users/{uuid.uuid4()}/documents"),
    ("GET", f"/api/v1/admin/users/{uuid.uuid4()}/documents"),
    ("GET", f"/api/v1/admin/documents/{uuid.uuid4()}/pdf"),
    ("GET", f"/api/v1/admin/users/{uuid.uuid4()}/transactions"),
    ("GET", "/api/v1/admin/card-orders"),
    ("PATCH", f"/api/v1/admin/card-orders/{uuid.uuid4()}"),
    ("GET", "/api/v1/admin/audit-log"),
]

#: Bodies for the PATCH routes above - each has its own schema, so one shared
#: payload would fail validation before reaching the gate under test.
PATCH_BODIES = {
    "/role": {"role": "admin"},
    "/blocked": {"blocked": True},
}


async def _promote(supabase: AsyncClient, user: UserRead) -> None:
    """The only way to become an admin - a direct database write, exactly as
    an operator would do it in the SQL Editor. There is no endpoint for this
    on purpose (see migrations/0016_admin_role.sql)."""
    await supabase.table("users").update({"role": "admin"}).eq("id", str(user.id)).execute()


async def _call(client: HTTPXAsyncClient, method: str, path: str):
    if method == "PATCH":
        body = next(
            (payload for suffix, payload in PATCH_BODIES.items() if path.endswith(suffix)),
            {"status": "shipped"},  # the card-orders route
        )
        return await client.patch(path, json=body)
    if method == "POST":
        # The only POST route today - /users/{id}/documents.
        return await client.post(path, json={"title": "T", "body": "B"})
    return await client.get(path)


@pytest.mark.parametrize(("method", "path"), ADMIN_ENDPOINTS)
async def test_regular_user_is_forbidden(authed_client, method, path):
    client, _user = authed_client
    resp = await _call(client, method, path)
    assert resp.status_code == 403, f"{method} {path} was not gated"


@pytest.mark.parametrize(("method", "path"), ADMIN_ENDPOINTS)
async def test_anonymous_is_unauthorized(client, method, path):
    resp = await _call(client, method, path)
    assert resp.status_code == 401, f"{method} {path} was reachable without a session"


async def test_admin_can_list_users(supabase, authed_client, user_factory):
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    other = await user_factory(email="someone-else@example.com")

    resp = await admin_client.get("/api/v1/admin/users")

    assert resp.status_code == 200
    emails = {row["email"] for row in resp.json()}
    # The whole point: an admin sees users other than themselves.
    assert other.email in emails
    assert admin.email in emails


async def test_user_search_filters(supabase, authed_client, user_factory):
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    await user_factory(email="findme@example.com", first_name="Zoltan")

    resp = await admin_client.get("/api/v1/admin/users", params={"search": "Zoltan"})

    assert resp.status_code == 200
    assert [row["email"] for row in resp.json()] == ["findme@example.com"]


async def test_user_detail_never_exposes_secrets(
    supabase, authed_client, user_factory, account_factory
):
    """The service reads from `users` and `cards`, whose rows also hold
    password_hash and the card's PAN/CVV/expiry. None of that may reach the
    response - see the projections in admin/schemas.py."""
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    target = await user_factory()
    account = await account_factory(target)
    await supabase.table("cards").insert(
        {
            "account_id": account["id"],
            "last4": "4321",
            "status": "active",
            "card_number": "4111111111114321",
            "cvv": "123",
            "expiry_month": 12,
            "expiry_year": 2030,
        }
    ).execute()

    resp = await admin_client.get(f"/api/v1/admin/users/{target.id}")

    assert resp.status_code == 200
    body = resp.text
    for secret in ("password_hash", "card_number", "cvv", "expiry_month", "expiry_year"):
        assert secret not in body, f"{secret} leaked into the admin user detail"
    assert resp.json()["cards"][0]["last4"] == "4321"


async def test_admin_advances_card_order_status(
    supabase, authed_client, user_factory, account_factory
):
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    owner = await user_factory()
    account = await account_factory(owner)
    order = (
        await supabase.table("card_orders")
        .insert(
            {
                "account_id": account["id"],
                "full_name": "Ion Popescu",
                "phone": "0700000000",
                "address": "Str. Exemplu 1",
                "city": "Cluj",
                "postal_code": "400000",
                "country": "Romania",
            }
        )
        .execute()
    ).data[0]

    resp = await admin_client.patch(
        f"/api/v1/admin/card-orders/{order['id']}", json={"status": "shipped"}
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "shipped"

    audit = (
        await supabase.table("audit_log")
        .select("*")
        .eq("action", "admin.card_order_status")
        .execute()
    )
    assert len(audit.data) == 1
    # The ACTOR is logged, not the order's owner.
    assert audit.data[0]["user_id"] == str(admin.id)
    assert audit.data[0]["metadata_json"] == {"from": "pending", "to": "shipped"}


async def test_card_order_status_rejects_unknown_value(supabase, authed_client):
    admin_client, admin = authed_client
    await _promote(supabase, admin)

    resp = await admin_client.patch(
        f"/api/v1/admin/card-orders/{uuid.uuid4()}", json={"status": "pending"}
    )

    # 'pending' is not a valid transition target (see CardOrderStatusUpdate),
    # so this fails validation before the order is even looked up.
    assert resp.status_code == 422


async def test_revoking_admin_takes_effect_immediately(supabase, authed_client):
    """The role is re-read per request rather than cached on the session, so
    demoting an admin does not wait for their 7-day cookie to expire."""
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    assert (await admin_client.get("/api/v1/admin/stats")).status_code == 200

    await supabase.table("users").update({"role": "customer"}).eq(
        "id", str(admin.id)
    ).execute()

    assert (await admin_client.get("/api/v1/admin/stats")).status_code == 403


async def test_blocking_locks_the_user_out(
    supabase, authed_client, authed_client_factory, user_factory
):
    """The property that matters: an ALREADY logged-in user stops working the
    moment they are blocked, not at their next login.

    The status is 401, not 403: blocking deletes the user's session rows, so
    `get_current_user` fails on "no such session" before it ever reads
    `blocked_at`. That is the intended order - the session teardown is the
    faster, harder stop. The `blocked_at` guard is what catches a session
    that somehow survives, and is covered by the next test.
    """
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    victim = await user_factory()
    victim_client, _ = await authed_client_factory(victim)

    assert (await victim_client.get("/api/v1/users/me")).status_code == 200

    resp = await admin_client.patch(
        f"/api/v1/admin/users/{victim.id}/blocked", json={"blocked": True}
    )
    assert resp.status_code == 200
    assert resp.json()["blocked_at"] is not None

    blocked = await victim_client.get("/api/v1/users/me")
    assert blocked.status_code == 401


async def test_blocked_user_is_refused_even_with_a_live_session(
    supabase, authed_client, authed_client_factory, user_factory, session_token_factory
):
    """The `blocked_at` guard in get_current_user, isolated.

    Blocking normally deletes the user's sessions, which would mask this
    check - so here a session is minted directly AFTER the block (the
    factory writes to `sessions` without going through start_session, which
    would itself refuse). That reproduces the case the guard exists for: a
    session that exists while the account is blocked.
    """
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    victim = await user_factory()

    await admin_client.patch(
        f"/api/v1/admin/users/{victim.id}/blocked", json={"blocked": True}
    )

    # Session created after the block, bypassing start_session.
    token = await session_token_factory(victim)
    victim_client, _ = await authed_client_factory(victim)
    victim_client.cookies.set(settings.SESSION_COOKIE_NAME, token)

    resp = await victim_client.get("/api/v1/users/me")

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "account_blocked"


async def test_blocked_user_cannot_log_in_again(
    supabase, client, authed_client, user_factory
):
    """Blocking also has to survive a fresh login - the check lives in
    start_session, which every login path goes through."""
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    victim = await user_factory()

    await admin_client.patch(
        f"/api/v1/admin/users/{victim.id}/blocked", json={"blocked": True}
    )

    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": victim.email, "password": "password123"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "account_blocked"


async def test_unblocking_restores_access(
    supabase, client, authed_client, user_factory
):
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    victim = await user_factory()

    await admin_client.patch(
        f"/api/v1/admin/users/{victim.id}/blocked", json={"blocked": True}
    )
    await admin_client.patch(
        f"/api/v1/admin/users/{victim.id}/blocked", json={"blocked": False}
    )

    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": victim.email, "password": "password123"},
    )
    assert resp.status_code == 200


async def test_admin_cannot_block_or_demote_themselves(supabase, authed_client):
    """Guards against an admin locking themselves - and possibly the last
    admin seat - out of the panel."""
    admin_client, admin = authed_client
    await _promote(supabase, admin)

    blocked = await admin_client.patch(
        f"/api/v1/admin/users/{admin.id}/blocked", json={"blocked": True}
    )
    demoted = await admin_client.patch(
        f"/api/v1/admin/users/{admin.id}/role", json={"role": "customer"}
    )

    assert blocked.status_code == 403
    assert demoted.status_code == 403


async def test_role_change_promotes_and_demotes(supabase, authed_client, authed_client_factory):
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    other_client, other = await authed_client_factory()

    # Not an admin yet.
    assert (await other_client.get("/api/v1/admin/me")).status_code == 403

    promote = await admin_client.patch(
        f"/api/v1/admin/users/{other.id}/role", json={"role": "admin"}
    )
    assert promote.status_code == 200
    assert promote.json()["role"] == "admin"
    assert (await other_client.get("/api/v1/admin/me")).status_code == 200

    await admin_client.patch(
        f"/api/v1/admin/users/{other.id}/role", json={"role": "customer"}
    )
    assert (await other_client.get("/api/v1/admin/me")).status_code == 403


async def test_role_change_rejects_unknown_role(supabase, authed_client, user_factory):
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    other = await user_factory()

    resp = await admin_client.patch(
        f"/api/v1/admin/users/{other.id}/role", json={"role": "superuser"}
    )

    assert resp.status_code == 422


async def test_transactions_can_be_filtered_by_card(
    supabase, authed_client, user_factory, account_factory
):
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    owner = await user_factory()
    account = await account_factory(owner, currency="RON")

    cards = (
        await supabase.table("cards")
        .insert(
            [
                {"account_id": account["id"], "last4": "1111", "status": "active"},
                {"account_id": account["id"], "last4": "2222", "status": "active"},
            ]
        )
        .execute()
    ).data
    card_a, card_b = cards[0], cards[1]

    journal = (
        await supabase.table("journal_transactions")
        .insert(
            {
                "reference": "TEST",
                "idempotency_key": f"admin-tx-{uuid.uuid4()}",
                "description": "Cumparatura test",
            }
        )
        .execute()
    ).data[0]
    await supabase.table("ledger_entries").insert(
        [
            {
                "journal_id": journal["id"],
                "account_id": account["id"],
                "direction": "debit",
                "amount_minor": 1000,
                "currency": "RON",
                "card_id": card_a["id"],
            },
            {
                "journal_id": journal["id"],
                "account_id": account["id"],
                "direction": "debit",
                "amount_minor": 2000,
                "currency": "RON",
                "card_id": card_b["id"],
            },
            # No card at all - a transfer-like entry. Must still show in the
            # unfiltered list, and must never show under a card filter.
            {
                "journal_id": journal["id"],
                "account_id": account["id"],
                "direction": "credit",
                "amount_minor": 5000,
                "currency": "RON",
            },
        ]
    ).execute()

    all_tx = await admin_client.get(f"/api/v1/admin/users/{owner.id}/transactions")
    assert all_tx.status_code == 200
    assert len(all_tx.json()) == 3

    filtered = await admin_client.get(
        f"/api/v1/admin/users/{owner.id}/transactions",
        params={"card_id": card_a["id"]},
    )
    assert filtered.status_code == 200
    rows = filtered.json()
    assert len(rows) == 1
    assert rows[0]["amount_minor"] == 1000
    assert rows[0]["card_last4"] == "1111"


async def test_transactions_of_another_users_card_are_not_returned(
    supabase, authed_client, user_factory, account_factory
):
    """A card id from someone else narrows to nothing rather than leaking
    across users - "user X's transactions" always means X's."""
    admin_client, admin = authed_client
    await _promote(supabase, admin)

    owner = await user_factory()
    await account_factory(owner, currency="RON")
    stranger = await user_factory()
    stranger_account = await account_factory(stranger, currency="RON")
    stranger_card = (
        await supabase.table("cards")
        .insert({"account_id": stranger_account["id"], "last4": "9999", "status": "active"})
        .execute()
    ).data[0]

    resp = await admin_client.get(
        f"/api/v1/admin/users/{owner.id}/transactions",
        params={"card_id": stranger_card["id"]},
    )

    assert resp.status_code == 200
    assert resp.json() == []


async def test_stats_counts_users_and_accounts(
    supabase, authed_client, user_factory, account_factory, seed_balance_factory
):
    admin_client, admin = authed_client
    await _promote(supabase, admin)
    other = await user_factory()
    account = await account_factory(other, currency="RON")
    await seed_balance_factory(account["id"], 250_00, "RON")

    resp = await admin_client.get("/api/v1/admin/stats")

    assert resp.status_code == 200
    stats = resp.json()
    assert stats["total_users"] >= 2
    assert stats["total_accounts"] >= 1
    ron = next(row for row in stats["totals_by_currency"] if row["currency"] == "RON")
    assert ron["total_minor"] == 250_00
