"""In-app notifications (the header bell icon). create_notification is the
one entry point other modules call to raise one - see accounts/service.py's
referral reward payout for the first (only, so far) caller.
"""

import uuid
from datetime import UTC, datetime

from supabase import AsyncClient

from app.modules.notifications import bus
from app.modules.users.schemas import UserRead

DEFAULT_LIMIT = 20


async def create_notification(
    supabase: AsyncClient,
    user_id: uuid.UUID | str,
    title: str,
    body: str,
    *,
    category: str | None = None,
) -> dict:
    """`category` is an optional stable tag (e.g. "money_received") for
    callers - today just the frontend's pop-up animation - that need to key
    off WHAT happened rather than parse the free-text title/body, which is
    fixed Romanian prose with no locale concept. None for every caller that
    doesn't need one; existing rows have no category either.

    Publishes to `bus` after the insert (never before - a subscriber must
    never be told about a row that could still fail to be written), so any
    open GET /notifications/stream connection for this user gets it
    immediately instead of waiting for that client's own poll."""
    row: dict[str, str] = {"user_id": str(user_id), "title": title, "body": body}
    if category is not None:
        row["category"] = category
    resp = await supabase.table("notifications").insert(row).execute()
    created = resp.data[0]
    bus.publish(str(user_id), created)
    return created


async def list_notifications(supabase: AsyncClient, user: UserRead, limit: int = DEFAULT_LIMIT) -> list[dict]:
    resp = (
        await supabase.table("notifications")
        .select("*")
        .eq("user_id", str(user.id))
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data


async def count_unread(supabase: AsyncClient, user: UserRead) -> int:
    resp = (
        await supabase.table("notifications")
        .select("id", count="exact")
        .eq("user_id", str(user.id))
        .is_("read_at", "null")
        .execute()
    )
    return resp.count or 0


async def mark_all_read(supabase: AsyncClient, user: UserRead) -> None:
    await (
        supabase.table("notifications")
        .update({"read_at": datetime.now(UTC).isoformat()})
        .eq("user_id", str(user.id))
        .is_("read_at", "null")
        .execute()
    )


async def mark_read(supabase: AsyncClient, user: UserRead, notification_id: uuid.UUID | str) -> None:
    """One notification, not the whole inbox - "seen" now means the user
    clicked THIS item, not "opened the dropdown at all" (see mark_all_read
    above, which the frontend no longer calls on open). Scoped by user_id in
    the same update, not a separate ownership check first: a notification_id
    that exists but belongs to someone else silently affects zero rows,
    exactly like one that doesn't exist at all - never a leak, never an
    error the caller has to distinguish."""
    await (
        supabase.table("notifications")
        .update({"read_at": datetime.now(UTC).isoformat()})
        .eq("id", str(notification_id))
        .eq("user_id", str(user.id))
        .is_("read_at", "null")
        .execute()
    )
