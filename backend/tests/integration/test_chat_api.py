"""POST /api/v1/chat - the AI layer over HTTP.

The model is always a scripted `MockProvider`, injected by overriding the
`get_model_provider` dependency (the same seam `get_supabase` uses), so nothing
here needs Azure credentials or touches the network. History now lives
server-side (conversations/messages tables); the client only holds a
conversation_id, so these tests assert on that plus what got persisted.
"""

import json
import uuid

import pytest

from app.ai.schemas import ModelResponse, ToolCall
from app.modules.accounts.service import OPENING_BALANCE_MINOR
from app.modules.chat.schemas import MAX_MESSAGE_CHARS


async def test_chat_requires_authentication(client):
    resp = await client.post("/api/v1/chat", json={"message": "hello"})

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_chat_returns_reply_and_a_conversation_id(authed_client, scripted_provider):
    client, _user = authed_client
    scripted_provider(ModelResponse(text="Hello! How can I help with your banking?"))

    resp = await client.post("/api/v1/chat", json={"message": "hello"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reply"] == "Hello! How can I help with your banking?"
    assert uuid.UUID(body["conversation_id"])  # a real id was assigned


async def test_chat_threads_context_into_the_ai_service(authed_client, scripted_provider):
    """End to end through HTTP: the tool loop runs against the caller's real
    accounts, with the account resolved from the session - not from the model."""
    client, _user = authed_client

    account = (
        await client.post("/api/v1/accounts", json={"name": "Checking", "currency": "USD"})
    ).json()

    provider = scripted_provider(
        # First turn: the model asks for a balance, naming no account.
        ModelResponse(tool_calls=[ToolCall(id="c1", name="get_balance", arguments={})]),
        ModelResponse(text="Your balance is available."),
    )

    resp = await client.post("/api/v1/chat", json={"message": "what's my balance?"})

    assert resp.status_code == 200, resp.text

    # The tool result fed back to the model carries this user's real account
    # and its real opening balance.
    tool_messages = [m for m in provider.calls[1] if m.role == "tool"]
    assert len(tool_messages) == 1
    payload = json.loads(tool_messages[0].content or "{}")

    assert payload["ok"] is True
    assert payload["result"]["account_id"] == account["id"]
    assert payload["result"]["balance_minor"] == OPENING_BALANCE_MINOR
    assert payload["result"]["source"] == "ledger"


async def test_chat_language_field_reaches_the_agents_system_prompt(
    authed_client, scripted_provider
):
    """The frontend's language switcher (document.documentElement.lang) rides
    on the request body straight through to Context.language and from there
    into the language directive appended to the system prompt (see
    app/ai/language_directive.py) - end to end, not just at the unit level."""
    client, _user = authed_client
    provider = scripted_provider(ModelResponse(text="ok"))

    # A banking keyword ("balance") so routing resolves by rule with no LLM
    # classification call first - keeps calls[0] the banking agent's own
    # system prompt, not the routing classifier's (see BANKING_ROUTING_RULES).
    resp = await client.post(
        "/api/v1/chat", json={"message": "what's my balance?", "language": "fr"}
    )

    assert resp.status_code == 200, resp.text
    system_message = provider.calls[0][0]
    assert system_message.role == "system"
    assert "franceză" in system_message.content


async def test_chat_omitted_language_defaults_to_romanian_with_no_directive(
    authed_client, scripted_provider
):
    """Every existing caller - nobody had a `language` field before this -
    must see byte-identical behavior: no directive appended at all."""
    client, _user = authed_client
    provider = scripted_provider(ModelResponse(text="ok"))

    resp = await client.post("/api/v1/chat", json={"message": "what's my balance?"})

    assert resp.status_code == 200, resp.text
    system_message = provider.calls[0][0]
    assert "SUPRASCRIE" not in system_message.content


async def test_chat_surfaces_a_card_order_proposal(authed_client, scripted_provider):
    """propose_card_order (app/ai/tools/banking/propose_card_order.py) never
    writes anything itself - it's a separate, older propose-only tool that
    predates the proposals table/confirm-reject flow (see
    app/modules/chat/proposals_service.py) and isn't wired into it, so
    ChatResponse.proposal stays None for this tool specifically (unlike the
    five propose_* tools in app/ai/tools/propose_tools.py - see
    test_proposals_confirm.py for those). Its result currently only reaches
    the user as prose in `reply` and in the raw tool-result trace. This test
    proves the tool still runs correctly and, crucially, that nothing gets
    written - no card order is ever actually placed by the chat turn alone."""
    client, _user = authed_client
    account = (
        await client.post("/api/v1/accounts", json={"name": "Checking", "currency": "USD"})
    ).json()

    scripted_provider(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="propose_card_order",
                    arguments={
                        "full_name": "Ana Pop",
                        "phone": "0712345678",
                        "address": "Str. Exemplu 1",
                        "city": "Cluj-Napoca",
                        "postal_code": "400000",
                        "country": "Romania",
                    },
                )
            ]
        ),
        ModelResponse(text="Iată comanda pregătită pentru confirmare."),
    )

    resp = await client.post("/api/v1/chat", json={"message": "vreau un card fizic nou"})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["proposal"] is None
    assert body["reply"] == "Iată comanda pregătită pentru confirmare."

    history_resp = await client.get(f"/api/v1/chat/conversations/{body['conversation_id']}/messages")
    tool_messages = [m for m in history_resp.json() if m["role"] == "tool"]
    assert len(tool_messages) == 1
    tool_result = json.loads(tool_messages[0]["content"])
    assert tool_result["ok"] is True
    assert tool_result["result"]["full_name"] == "Ana Pop"
    assert tool_result["result"]["account_id"] == account["id"]

    # No card order was actually placed - the tool only proposed one.
    orders = (await client.get("/api/v1/card-orders")).json()
    assert orders == []


@pytest.mark.parametrize("message", ["", "   ", "\n\t "])
async def test_chat_rejects_empty_message(authed_client, scripted_provider, message):
    """Blank and whitespace-only alike - never let an empty prompt reach the model."""
    client, _user = authed_client
    scripted_provider(ModelResponse(text="unused"))

    resp = await client.post("/api/v1/chat", json={"message": message})

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "validation_error"


async def test_chat_rejects_oversized_message(authed_client, scripted_provider):
    client, _user = authed_client
    scripted_provider(ModelResponse(text="unused"))

    resp = await client.post("/api/v1/chat", json={"message": "x" * (MAX_MESSAGE_CHARS + 1)})

    assert resp.status_code == 422


async def test_chat_persists_conversation_across_turns(authed_client, scripted_provider):
    """The whole point of Step 5: a second turn against the same
    conversation_id sees the first turn, without the client resending it."""
    client, _user = authed_client
    scripted_provider(ModelResponse(text="first answer"))

    first = await client.post("/api/v1/chat", json={"message": "first question"})
    assert first.status_code == 200, first.text
    conversation_id = first.json()["conversation_id"]

    scripted_provider(ModelResponse(text="second answer"))
    second = await client.post(
        "/api/v1/chat",
        json={"message": "second question", "conversation_id": conversation_id},
    )
    assert second.status_code == 200, second.text
    assert second.json()["conversation_id"] == conversation_id

    history_resp = await client.get(f"/api/v1/chat/conversations/{conversation_id}/messages")
    assert history_resp.status_code == 200, history_resp.text
    history = history_resp.json()

    assert [(m["role"], m["content"]) for m in history] == [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "second question"),
        ("assistant", "second answer"),
    ]


async def test_chat_with_unknown_conversation_id_is_not_found(authed_client, scripted_provider):
    client, _user = authed_client
    scripted_provider(ModelResponse(text="unused"))

    resp = await client.post(
        "/api/v1/chat",
        json={"message": "hi", "conversation_id": str(uuid.uuid4())},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "conversation_not_found"


async def test_tool_call_turns_are_persisted(authed_client, scripted_provider):
    """Tool-call round trips must survive a page refresh too, not just plain
    text turns - that's the whole reason messages.tool_calls is JSONB."""
    client, _user = authed_client

    scripted_provider(
        ModelResponse(tool_calls=[ToolCall(id="c1", name="get_balance", arguments={})]),
        ModelResponse(text="Soldul tău este disponibil."),
    )

    resp = await client.post("/api/v1/chat", json={"message": "care este soldul meu?"})
    assert resp.status_code == 200, resp.text
    conversation_id = resp.json()["conversation_id"]

    history_resp = await client.get(f"/api/v1/chat/conversations/{conversation_id}/messages")
    assert history_resp.status_code == 200, history_resp.text
    history = history_resp.json()

    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
    assert history[0]["content"] == "care este soldul meu?"
    assert history[1]["tool_calls"][0]["name"] == "get_balance"
    assert history[1]["tool_calls"][0]["id"] == "c1"
    assert history[2]["name"] == "get_balance"
    assert history[2]["tool_call_id"] == "c1"
    assert history[3]["content"] == "Soldul tău este disponibil."


async def test_chat_endpoint_includes_routing_in_response(authed_client, scripted_provider):
    """The routing decision travels back to the client, so a UI can show which
    agent answered (Step 14)."""
    client, _user = authed_client
    scripted_provider(ModelResponse(text="salut"))

    resp = await client.post("/api/v1/chat", json={"message": "care este soldul meu?"})

    assert resp.status_code == 200, resp.text
    routing = resp.json()["routing"]

    assert routing["agent_name"] == "banking"
    assert routing["confidence"] == 1.0
    assert routing["matched_rule"] == "banking_keywords"
    assert "sold" in routing["reason"]
    # Never echoes what the user typed.
    assert "care este soldul meu?" not in routing["reason"]


async def test_chat_endpoint_persists_routing_metadata_on_assistant_message(
    authed_client, scripted_provider, supabase
):
    """Persisted on the assistant turn the routed agent produced - and on
    nothing else."""
    client, _user = authed_client
    scripted_provider(
        ModelResponse(tool_calls=[ToolCall(id="c1", name="get_balance", arguments={})]),
        ModelResponse(text="Soldul tău este disponibil."),
    )

    resp = await client.post("/api/v1/chat", json={"message": "care este soldul meu?"})
    assert resp.status_code == 200, resp.text
    conversation_id = resp.json()["conversation_id"]

    rows = (
        await supabase.table("messages")
        .select("role, content, routing_metadata")
        .eq("conversation_id", conversation_id)
        .order("created_at")
        .execute()
    ).data

    # user, assistant(tool_calls), tool, assistant(final)
    assert [row["role"] for row in rows] == ["user", "assistant", "tool", "assistant"]
    assert [row["routing_metadata"] is None for row in rows] == [True, True, True, False]

    stored = rows[-1]["routing_metadata"]
    assert stored["agent_name"] == "banking"
    assert stored["matched_rule"] == "banking_keywords"
    assert stored["confidence"] == 1.0


async def test_chat_isolates_users(authed_client_factory, scripted_provider):
    """User B cannot read user A's account, even naming its id explicitly."""
    alice_client, _alice = await authed_client_factory()
    bob_client, _bob = await authed_client_factory()

    alice_account = (
        await alice_client.post(
            "/api/v1/accounts", json={"name": "Alice Checking", "currency": "USD"}
        )
    ).json()

    # Bob's model tries to read Alice's account by id.
    provider = scripted_provider(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="get_balance",
                    arguments={"account_id": alice_account["id"]},
                )
            ]
        ),
        ModelResponse(text="I can only see your own accounts."),
    )

    resp = await bob_client.post("/api/v1/chat", json={"message": "balance of that account"})

    assert resp.status_code == 200, resp.text

    payload = json.loads(
        [m for m in provider.calls[1] if m.role == "tool"][0].content or "{}"
    )
    assert payload["ok"] is False
    assert "access denied" in payload["error"]
    # The refused identifier is never echoed back to the model.
    assert alice_account["id"] not in payload["error"]

    # And Alice's own request still works normally, in her own conversation.
    scripted_provider(ModelResponse(text="fine"))
    alice_resp = await alice_client.post("/api/v1/chat", json={"message": "hello"})
    assert alice_resp.status_code == 200
    assert alice_resp.json()["reply"] == "fine"
