"""GET /api/v1/notifications, /unread-count, /stream, and the two
mark-read endpoints (bulk and single). Notifications have no creation
endpoint of their own - every row here is seeded directly (mirroring how
notifications_service.create_notification would insert it), the same
"real DB, no HTTP for setup" pattern other tests use for state their own
feature doesn't create (see e.g. enroll_face in conftest.py).
"""

import asyncio
from datetime import UTC, datetime

import pytest

from app.modules.notifications import bus
from app.modules.notifications import service as notifications_service


async def _seed(supabase, user_id, *, title="Test", body="Body", category=None, read=False):
    row = {"user_id": str(user_id), "title": title, "body": body}
    if category is not None:
        row["category"] = category
    if read:
        row["read_at"] = datetime.now(UTC).isoformat()
    resp = await supabase.table("notifications").insert(row).execute()
    return resp.data[0]


async def test_notifications_require_authentication(client):
    resp = await client.get("/api/v1/notifications")
    assert resp.status_code == 401


async def test_list_notifications_returns_newest_first(authed_client, supabase):
    client, user = authed_client
    await _seed(supabase, user.id, title="First")
    await _seed(supabase, user.id, title="Second")

    resp = await client.get("/api/v1/notifications")
    assert resp.status_code == 200, resp.text
    titles = [n["title"] for n in resp.json()]
    assert titles == ["Second", "First"]


async def test_list_notifications_does_not_leak_other_users(
    authed_client, authed_client_factory, supabase
):
    client, user = authed_client
    _other_client, other_user = await authed_client_factory()
    await _seed(supabase, user.id, title="Mine")
    await _seed(supabase, other_user.id, title="Theirs")

    body = (await client.get("/api/v1/notifications")).json()
    assert [n["title"] for n in body] == ["Mine"]


async def test_unread_count_only_counts_unread(authed_client, supabase):
    client, user = authed_client
    await _seed(supabase, user.id, read=False)
    await _seed(supabase, user.id, read=False)
    await _seed(supabase, user.id, read=True)

    resp = await client.get("/api/v1/notifications/unread-count")
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 2


async def test_mark_one_read_only_affects_that_notification(authed_client, supabase):
    client, user = authed_client
    first = await _seed(supabase, user.id, title="First")
    await _seed(supabase, user.id, title="Second")

    resp = await client.post(f"/api/v1/notifications/{first['id']}/mark-read")
    assert resp.status_code == 204, resp.text

    listed = {n["title"]: n["read_at"] for n in (await client.get("/api/v1/notifications")).json()}
    assert listed["First"] is not None
    assert listed["Second"] is None

    unread = (await client.get("/api/v1/notifications/unread-count")).json()
    assert unread["count"] == 1


async def test_mark_one_read_cannot_mark_someone_elses_notification(
    authed_client, authed_client_factory, supabase
):
    """Scoped by user_id in the same update, not a separate ownership check -
    see notifications/service.py::mark_read's docstring: a foreign id
    silently affects zero rows, exactly like one that doesn't exist."""
    client, _user = authed_client
    _other_client, other_user = await authed_client_factory()
    theirs = await _seed(supabase, other_user.id, title="Theirs")

    resp = await client.post(f"/api/v1/notifications/{theirs['id']}/mark-read")
    assert resp.status_code == 204, resp.text

    # Reads the table directly rather than needing the other user's own
    # authenticated client - the point is just to prove `client`'s call had
    # no effect on a row it doesn't own.
    row = (
        await supabase.table("notifications").select("read_at").eq("id", theirs["id"]).execute()
    ).data[0]
    assert row["read_at"] is None


async def test_mark_one_read_is_idempotent(authed_client, supabase):
    client, user = authed_client
    notif = await _seed(supabase, user.id)

    first = await client.post(f"/api/v1/notifications/{notif['id']}/mark-read")
    second = await client.post(f"/api/v1/notifications/{notif['id']}/mark-read")

    assert first.status_code == 204
    assert second.status_code == 204


async def test_bulk_mark_read_still_marks_everything(authed_client, supabase):
    """The original bulk endpoint stays available - only the frontend's
    default behavior changed to per-item marking (see app.js)."""
    client, user = authed_client
    await _seed(supabase, user.id)
    await _seed(supabase, user.id)

    resp = await client.post("/api/v1/notifications/mark-read")
    assert resp.status_code == 204, resp.text

    unread = (await client.get("/api/v1/notifications/unread-count")).json()
    assert unread["count"] == 0


async def test_category_is_returned_when_set(authed_client, supabase):
    client, user = authed_client
    await _seed(supabase, user.id, category="money_received")
    await _seed(supabase, user.id)

    categories = {n["category"] for n in (await client.get("/api/v1/notifications")).json()}
    assert categories == {"money_received", None}


async def test_payment_received_notification_carries_the_money_category(
    authed_client, authed_client_factory, enroll_face
):
    """End to end: create_notification's category param actually reaches
    the received-money notification (see payments/service.py)."""
    payer, payer_user = authed_client
    payee, _payee_user = await authed_client_factory()

    payer_account = (
        await payer.post("/api/v1/accounts", json={"name": "Payer", "currency": "USD"})
    ).json()
    payee_account = (
        await payee.post("/api/v1/accounts", json={"name": "Payee", "currency": "USD"})
    ).json()

    # First payment to a new person is a mandatory Face ID trigger (see
    # face_auth/service.py::enforce_face_confirmation) - unrelated to what
    # this test checks, but required for the payment to succeed at all.
    face_token = await enroll_face(payer_user.id)
    resp = await payer.post(
        "/api/v1/payments",
        json={
            "from_account_id": payer_account["id"],
            "to_iban": payee_account["iban"],
            "beneficiary_name": "Payee Person",
            "amount_minor": 1_000,
        },
        headers={"Idempotency-Key": "notif-payment-1", "X-Face-Confirmation": face_token},
    )
    assert resp.status_code == 201, resp.text

    notifications = (await payee.get("/api/v1/notifications")).json()
    assert len(notifications) == 1
    assert notifications[0]["category"] == "money_received"


async def test_stream_requires_authentication(client):
    resp = await client.get("/api/v1/notifications/stream")
    assert resp.status_code == 401


async def test_creating_a_notification_publishes_it_to_a_subscribed_queue(supabase, user_factory):
    """GET /notifications/stream itself isn't exercised here: httpx's
    ASGITransport fully buffers a StreamingResponse's body when it's wrapped
    by this app's two BaseHTTPMiddleware layers (rate limiting, request-id)
    - a known ASGITransport-in-tests limitation, not a real bug (verified
    live against the actual running server: a real payment's notification
    arrived on an open stream in well under a second). See
    test_notification_bus.py for the bus module's own unit tests.

    What this DOES prove end to end: create_notification - the one thing
    every real caller (payments, admin, auth) actually invokes - really
    publishes to `bus`, not just inserts a row."""
    user = await user_factory()
    user_id = str(user.id)
    queue = bus.subscribe(user_id)
    try:
        await notifications_service.create_notification(
            supabase, user_id, "Test", "Body", category="money_received"
        )
        published = await asyncio.wait_for(queue.get(), timeout=5)
    finally:
        bus.unsubscribe(user_id, queue)

    assert published["title"] == "Test"
    assert published["category"] == "money_received"


async def test_creating_a_notification_does_not_publish_to_a_different_users_queue(
    supabase, user_factory
):
    user = await user_factory()
    other_user = await user_factory()
    user_id = str(user.id)
    queue = bus.subscribe(user_id)
    try:
        await notifications_service.create_notification(supabase, str(other_user.id), "Not for you", "Body")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.5)
    finally:
        bus.unsubscribe(user_id, queue)
