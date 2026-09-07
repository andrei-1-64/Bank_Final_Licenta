"""GET /api/v1/chat/conversations and .../{id}/messages.

Setup goes through direct-DB fixtures (conversation_factory), not the /chat
endpoint, so these tests don't need the AI provider at all - same reasoning
as authed_client bypassing the login endpoint (see tests/conftest.py).
"""

import uuid

from app.ai.schemas import ModelResponse


async def test_list_conversations_requires_authentication(client):
    resp = await client.get("/api/v1/chat/conversations")

    assert resp.status_code == 401


async def test_list_conversations_is_empty_for_a_new_user(authed_client):
    client, _user = authed_client

    resp = await client.get("/api/v1/chat/conversations")

    assert resp.status_code == 200, resp.text
    assert resp.json() == []


async def test_list_conversations_includes_one_with_zero_messages(
    authed_client, conversation_factory
):
    """A user who opened the chat view but never sent anything still has a
    conversation row - it must show up in their list."""
    client, user = authed_client
    conversation = await conversation_factory(user)

    resp = await client.get("/api/v1/chat/conversations")

    assert resp.status_code == 200, resp.text
    ids = [c["id"] for c in resp.json()]
    assert conversation["id"] in ids


async def test_list_conversations_isolates_users(authed_client_factory, conversation_factory):
    alice_client, alice = await authed_client_factory()
    _bob_client, bob = await authed_client_factory()

    alice_conversation = await conversation_factory(alice)
    await conversation_factory(bob)

    resp = await alice_client.get("/api/v1/chat/conversations")

    assert resp.status_code == 200, resp.text
    ids = [c["id"] for c in resp.json()]
    assert ids == [alice_conversation["id"]]


async def test_get_messages_for_empty_conversation_returns_empty_list(
    authed_client, conversation_factory
):
    client, user = authed_client
    conversation = await conversation_factory(user)

    resp = await client.get(f"/api/v1/chat/conversations/{conversation['id']}/messages")

    assert resp.status_code == 200, resp.text
    assert resp.json() == []


async def test_get_messages_for_unknown_conversation_is_not_found(authed_client):
    client, _user = authed_client

    resp = await client.get(f"/api/v1/chat/conversations/{uuid.uuid4()}/messages")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "conversation_not_found"


async def test_get_messages_for_foreign_conversation_is_not_found(
    authed_client_factory, conversation_factory
):
    """Ownership-checked the same way accounts are - a foreign conversation
    must look identical to a nonexistent one."""
    alice_client, _alice = await authed_client_factory()
    _bob_client, bob = await authed_client_factory()
    bob_conversation = await conversation_factory(bob)

    resp = await alice_client.get(f"/api/v1/chat/conversations/{bob_conversation['id']}/messages")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "conversation_not_found"


async def test_conversation_created_by_chat_appears_in_the_list(authed_client, scripted_provider):
    client, _user = authed_client
    scripted_provider(ModelResponse(text="hi there"))

    chat_resp = await client.post("/api/v1/chat", json={"message": "hello"})
    conversation_id = chat_resp.json()["conversation_id"]

    list_resp = await client.get("/api/v1/chat/conversations")

    assert conversation_id in [c["id"] for c in list_resp.json()]


async def test_conversation_history_includes_routing_on_assistant_messages(
    authed_client, scripted_provider
):
    """The history endpoint surfaces the same routing decision the live reply
    carried (see ChatResponse.routing) - so a reopened conversation can still
    show which agent answered each turn (Step 14)."""
    client, _user = authed_client
    scripted_provider(ModelResponse(text="salut"))

    chat_resp = await client.post("/api/v1/chat", json={"message": "care este soldul meu?"})
    conversation_id = chat_resp.json()["conversation_id"]

    resp = await client.get(f"/api/v1/chat/conversations/{conversation_id}/messages")

    assert resp.status_code == 200, resp.text
    messages = resp.json()
    assert [m["role"] for m in messages] == ["user", "assistant"]

    # Never on the user's own turn.
    assert messages[0]["routing"] is None

    routing = messages[1]["routing"]
    assert routing["agent_name"] == "banking"
    assert routing["confidence"] == 1.0
    assert routing["matched_rule"] == "banking_keywords"
