import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from supabase import AsyncClient

from app.core.dependencies import get_current_user
from app.db.supabase_client import get_supabase
from app.modules.notifications import bus, service
from app.modules.notifications.schemas import NotificationRead, UnreadCountRead
from app.modules.users.schemas import UserRead

router = APIRouter()

#: How often the stream sends an SSE comment line when nothing new has
#: happened - purely a keep-alive so an idle intermediary (a browser, a
#: proxy) doesn't decide the connection is dead and close it; the client's
#: EventSource ignores comment lines entirely.
_HEARTBEAT_SECONDS = 15


@router.get("", response_model=list[NotificationRead])
async def list_my_notifications(
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> list[NotificationRead]:
    return await service.list_notifications(supabase, user)


@router.get("/unread-count", response_model=UnreadCountRead)
async def get_unread_count(
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> UnreadCountRead:
    count = await service.count_unread(supabase, user)
    return UnreadCountRead(count=count)


@router.get("/stream")
async def stream_notifications(
    request: Request,
    user: UserRead = Depends(get_current_user),
) -> StreamingResponse:
    """Server-Sent Events: as long as this connection is open, a new
    notification for this user arrives as one `data:` line within
    milliseconds of being created (see notifications_service.
    create_notification's bus.publish call) - no polling interval to wait
    out. GET, not POST, and no supabase dependency: this endpoint never
    reads or writes a row itself, it only relays what `bus` hands it.

    One long-lived connection per open tab - the frontend's EventSource
    reconnects on its own (built into the browser) if this drops, so the
    only cleanup this needs on its own end is unsubscribing the queue,
    done in `finally` regardless of how the loop below exits.
    """
    queue = bus.subscribe(str(user.id))

    async def event_stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    notification = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                payload = NotificationRead.model_validate(notification).model_dump(mode="json")
                yield f"data: {json.dumps(payload)}\n\n"
        finally:
            bus.unsubscribe(str(user.id), queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            # Disables response buffering on nginx-fronted deployments -
            # harmless here (nothing sits in front of this backend today,
            # see frontend/api.js's API_BASE_URL) but cheap insurance against
            # a proxy holding the whole stream until it closes.
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )


@router.post("/mark-read", status_code=204)
async def mark_read(
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> None:
    await service.mark_all_read(supabase, user)


@router.post("/{notification_id}/mark-read", status_code=204)
async def mark_one_read(
    notification_id: uuid.UUID,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> None:
    await service.mark_read(supabase, user, notification_id)
