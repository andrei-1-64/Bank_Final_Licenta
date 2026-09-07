"""app/modules/notifications/bus.py - the in-process pub/sub that lets
GET /notifications/stream push a new notification the instant
create_notification makes one, instead of the client having to poll.
Pure asyncio, no DB, no HTTP - see test_notifications_api.py for the one
test that exercises create_notification's actual publish call end to end.
"""

import asyncio

import pytest

from app.modules.notifications import bus


async def test_publish_delivers_to_a_subscribed_queue():
    queue = bus.subscribe("user-1")
    try:
        bus.publish("user-1", {"title": "Hi"})
        assert await asyncio.wait_for(queue.get(), timeout=1) == {"title": "Hi"}
    finally:
        bus.unsubscribe("user-1", queue)


async def test_publish_to_a_user_with_no_subscribers_does_not_raise():
    bus.publish("nobody-listening", {"title": "Hi"})


async def test_two_subscribers_for_the_same_user_both_get_it():
    """A user with the app open in two tabs - each queue replays the
    notification independently, never split between them."""
    first = bus.subscribe("user-2")
    second = bus.subscribe("user-2")
    try:
        bus.publish("user-2", {"title": "Hi"})
        assert (await asyncio.wait_for(first.get(), timeout=1))["title"] == "Hi"
        assert (await asyncio.wait_for(second.get(), timeout=1))["title"] == "Hi"
    finally:
        bus.unsubscribe("user-2", first)
        bus.unsubscribe("user-2", second)


async def test_publish_only_reaches_the_named_users_queue():
    mine = bus.subscribe("user-3")
    try:
        bus.publish("someone-else", {"title": "Not for you"})
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(mine.get(), timeout=0.3)
    finally:
        bus.unsubscribe("user-3", mine)


async def test_unsubscribe_stops_further_delivery():
    queue = bus.subscribe("user-4")
    bus.unsubscribe("user-4", queue)

    bus.publish("user-4", {"title": "Too late"})

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), timeout=0.3)


def test_unsubscribe_an_unknown_queue_does_not_raise():
    """A double-unsubscribe (or a queue that was never registered) is a
    no-op, not an error - the stream endpoint's `finally` calls this
    unconditionally on every disconnect path."""
    bus.unsubscribe("never-subscribed", asyncio.Queue())


def test_unsubscribe_cleans_up_the_empty_entry():
    """Not observable from the public API alone, so this reaches into the
    module's own state - the point is that a user with zero open tabs
    doesn't leave an empty list sitting in the dict forever."""
    queue = bus.subscribe("user-5")
    bus.unsubscribe("user-5", queue)
    assert "user-5" not in bus._subscribers
