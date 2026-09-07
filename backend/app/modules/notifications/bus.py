"""In-process pub/sub so a freshly-created notification reaches an open
GET /notifications/stream connection immediately, instead of that client
having to poll and wait out an interval.

Deliberately in-memory, not Redis/a real message broker: the app runs as a
single uvicorn process with no --workers flag (see docker-entrypoint.sh), so
every subscriber and every publish happen in the same process - there is no
second process that could ever miss a publish. This stops being true the
moment the backend is horizontally scaled; at that point this module needs
to become a real pub/sub backed by something all instances share.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

_subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)


def subscribe(user_id: str) -> asyncio.Queue:
    """One queue per open stream connection - a user with the app open in
    two tabs gets two independent queues, each replaying every notification
    (never split between them)."""
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers[user_id].append(queue)
    return queue


def unsubscribe(user_id: str, queue: asyncio.Queue) -> None:
    """Called from the stream endpoint's `finally` so a closed connection
    (tab closed, network drop) doesn't leak a queue forever."""
    queues = _subscribers.get(user_id)
    if not queues or queue not in queues:
        return
    queues.remove(queue)
    if not queues:
        _subscribers.pop(user_id, None)


def publish(user_id: str, notification: dict) -> None:
    """Fans one notification out to every queue currently subscribed for
    this user. A user with no open stream (app closed, or on a browser that
    never connected) simply has no subscribers - never an error, never
    buffered for later, since list_notifications is the durable source of
    truth this is only ever a low-latency nudge on top of."""
    for queue in _subscribers.get(user_id, []):
        queue.put_nowait(notification)
