"""Step 15 over HTTP: a turn that changes agent mid-flight.

Three things are proved here that the offline tests in tests/ai/test_handoff.py
cannot:

1. The DEMO end to end - a real recurring charge in the ledger, InsightsAgent
   detecting it, handing off, and BankingAgent producing a real `cancel_card`
   proposal row - with the model scripted deterministically, because the point
   is the mechanism and not the model's judgment.
2. PERSISTENCE per hop: each agent's contribution is its own set of rows with
   its own routing_metadata, and the chain is reconstructable from ordering.
3. The RESPONSE contract: `routing_chain` for the frontend, and the
   backward-compatible `routing` duplicate for anything not updated yet.

The model is a scripted `MockProvider` shared by both agents of a turn (see the
`scripted_provider` fixture), so the script below reads as the whole turn in
order: Insights' calls first, then Banking's.
"""

import calendar
import json
import uuid
from datetime import UTC, datetime

from app.ai.orchestrator import HANDOFF_REFUSED_REPLY
from app.ai.schemas import ModelResponse, ToolCall

# The message that starts the demo. "recurent" is an InsightsAgent routing
# keyword (insights_categories), so routing resolves by rule and never spends a
# scripted response on the LLM classifier.
DEMO_MESSAGE = "vreau să văd abonamentele mele recurente"

RECURRING_MERCHANT = "GymPass"
RECURRING_AMOUNT_MINOR = 12_000


def _handoff_call(
    target: str = "banking",
    *,
    call_id: str = "c-handoff",
    hint: str = (
        "Utilizatorul vrea să scape de abonamentul GymPass, 120,00 RON pe lună. "
        "Propune anularea cardului pe care se ia plata."
    ),
) -> ToolCall:
    return ToolCall(
        id=call_id,
        name="handoff_to_agent",
        arguments={
            "target_agent": target,
            "reason": "plată recurentă pe care utilizatorul vrea să o oprească",
            "context_hint": hint,
        },
    )


def _months_back(n: int) -> datetime:
    """The 5th of the month `n` months ago - a day every month has, so the
    arithmetic never has to handle month-end overflow (same helper as
    test_detect_recurring_payments_tool.py)."""
    now = datetime.now(UTC)
    month_index = now.month - 1 - n
    year = now.year + month_index // 12
    month = month_index % 12 + 1
    day = min(5, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day, hour=12, minute=0, second=0, microsecond=0)


async def _seed_recurring_charge(supabase, account_id, when: datetime) -> None:
    """One debit entry, straight into the ledger.

    Single-sided on purpose, exactly as test_detect_recurring_payments_tool.py
    does it: this is test scaffolding for a READ path, and going through
    post_transaction would need a funded counterparty account that has nothing
    to do with what is being tested.
    """
    journal = (
        await supabase.table("journal_transactions")
        .insert(
            {
                "reference": "TEST-HANDOFF",
                "idempotency_key": f"test-handoff-{uuid.uuid4()}",
                "description": RECURRING_MERCHANT,
            }
        )
        .execute()
    ).data[0]
    await supabase.table("ledger_entries").insert(
        {
            "journal_id": journal["id"],
            "account_id": str(account_id),
            "direction": "debit",
            "amount_minor": RECURRING_AMOUNT_MINOR,
            "currency": "RON",
            "created_at": when.isoformat(),
        }
    ).execute()


async def _open_account(client, name="Cont Curent", currency="RON"):
    resp = await client.post("/api/v1/accounts", json={"name": name, "currency": currency})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _tool_payload(provider, call_index: int, tool_name: str) -> dict:
    """The result the model was shown for `tool_name`, out of one provider call."""
    for message in provider.calls[call_index]:
        if message.role == "tool" and message.name == tool_name:
            return json.loads(message.content or "{}")
    raise AssertionError(f"{tool_name} result not found in provider call {call_index}")


# ---------------------------------------------------------------------------
# The demo: Insights -> Banking -> cancel_card
# ---------------------------------------------------------------------------


async def test_insights_hands_off_to_banking_which_proposes_cancel_card(
    authed_client, scripted_provider, supabase
):
    """THE Step 15 demo, end to end over HTTP.

    The user asks about subscriptions; InsightsAgent finds a real recurring
    charge in the ledger, hands the turn to BankingAgent, and BankingAgent -
    which InsightsAgent has no tools to imitate - creates a pending cancel_card
    proposal. One HTTP request, two agents, one Context.
    """
    client, user = authed_client
    account = await _open_account(client)
    card = (await client.post("/api/v1/cards", json={"account_id": account["id"]})).json()

    # Three months of the same charge: what detect_recurring_payments looks for.
    for n in range(3):
        await _seed_recurring_charge(supabase, account["id"], _months_back(n))

    provider = scripted_provider(
        # --- hop 1: InsightsAgent ---
        ModelResponse(
            tool_calls=[
                ToolCall(id="c1", name="detect_recurring_payments", arguments={"months_back": 6})
            ]
        ),
        ModelResponse(tool_calls=[_handoff_call("banking")]),
        # --- hop 2: BankingAgent, prompted with the context hint ---
        ModelResponse(tool_calls=[ToolCall(id="c3", name="list_cards", arguments={})]),
        ModelResponse(
            tool_calls=[
                ToolCall(id="c4", name="propose_cancel_card", arguments={"card_id": card["id"]})
            ]
        ),
        ModelResponse(
            text="Am pregătit o propunere de anulare a cardului. Confirmă în aplicație."
        ),
    )

    resp = await client.post("/api/v1/chat", json={"message": DEMO_MESSAGE})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 1. InsightsAgent really did find the recurring charge - not a stub.
    recurring = _tool_payload(provider, 0, "detect_recurring_payments")
    assert recurring["ok"] is True
    names = [p["name"] for p in recurring["result"]["recurring_payments"]]
    assert RECURRING_MERCHANT in names

    # 2. The turn changed hands, and the chain says so.
    chain = body["routing_chain"]
    assert [hop["agent_name"] for hop in chain] == ["insights", "banking"]
    assert chain[0]["handoff_from"] is None
    assert chain[1]["handoff_from"] == "insights"
    assert chain[1]["matched_rule"] == "handoff_from:insights"

    # 3. BankingAgent was prompted with the source's context_hint, appended as
    #    a user turn - and it saw the original question too, not just the hint.
    banking_prompt = [m for m in provider.calls[2] if m.role == "user"]
    assert [m.content for m in banking_prompt][0] == DEMO_MESSAGE
    assert RECURRING_MERCHANT in (banking_prompt[-1].content or "")

    # 4. The proposal exists, is pending, and nothing was actually cancelled.
    assert body["proposal"] is not None
    assert body["proposal"]["proposal_type"] == "cancel_card"
    row = (
        await supabase.table("proposals")
        .select("*")
        .eq("id", body["proposal"]["id"])
        .maybe_single()
        .execute()
    ).data
    assert row["status"] == "pending"
    assert row["payload"]["card_id"] == card["id"]
    assert row["user_id"] == str(user.id)

    cards_now = (await client.get("/api/v1/cards")).json()
    assert cards_now[0]["status"] == "active"

    # 5. The user sees the LAST agent's reply - the one that finished the turn.
    assert body["reply"].startswith("Am pregătit o propunere")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def test_a_two_hop_turn_persists_one_routing_row_per_hop(
    authed_client, scripted_provider, supabase
):
    """Each agent's contribution is its own set of rows with its own routing
    row, and the chain is reconstructable from ordering alone - which is how
    the frontend replays it (see agentChainsByMessage in frontend/app.js)."""
    client, _user = authed_client
    await _open_account(client)

    scripted_provider(
        ModelResponse(tool_calls=[_handoff_call("banking")]),
        ModelResponse(text="Iată ce pot face."),
    )

    resp = await client.post("/api/v1/chat", json={"message": DEMO_MESSAGE})
    assert resp.status_code == 200, resp.text
    conversation_id = resp.json()["conversation_id"]

    rows = (
        await supabase.table("messages")
        .select("role, content, name, routing_metadata")
        .eq("conversation_id", conversation_id)
        .order("created_at")
        .execute()
    ).data

    # user | insights' handoff tool-call turn | its (stripped) tool result |
    # insights' empty assistant row | banking's assistant row
    assert [row["role"] for row in rows] == ["user", "assistant", "tool", "assistant", "assistant"]

    # Routing rides on each hop's FINAL assistant row and nowhere else - the
    # same is_final_reply convention as before, applied once per hop.
    assert [row["routing_metadata"] is not None for row in rows] == [
        False,
        False,
        False,
        True,
        True,
    ]

    first_hop, second_hop = rows[3]["routing_metadata"], rows[4]["routing_metadata"]
    assert first_hop["agent_name"] == "insights"
    assert first_hop["handoff_from"] is None
    assert second_hop["agent_name"] == "banking"
    # THE link that reconstructs the chain on replay.
    assert second_hop["handoff_from"] == first_hop["agent_name"]

    # The first hop handed off before saying anything, so its row is empty.
    # It is still written: the routing row lives on it, and dropping it would
    # erase the first half of every chain from the stored history.
    assert not rows[3]["content"]
    assert rows[4]["content"] == "Iată ce pot face."


async def test_the_model_authored_context_hint_is_never_stored_as_a_user_turn(
    authed_client, scripted_provider, supabase
):
    """The synthetic prompt the target agent sees is MODEL-authored. Storing it
    as a `user` row would put words in the user's mouth in their own transcript
    - and feed them back as real user input on the next turn."""
    client, _user = authed_client
    await _open_account(client)

    scripted_provider(
        ModelResponse(tool_calls=[_handoff_call("banking")]),
        ModelResponse(text="gata"),
    )

    resp = await client.post("/api/v1/chat", json={"message": DEMO_MESSAGE})
    conversation_id = resp.json()["conversation_id"]

    rows = (
        await supabase.table("messages")
        .select("role, content")
        .eq("conversation_id", conversation_id)
        .order("created_at")
        .execute()
    ).data

    user_rows = [row for row in rows if row["role"] == "user"]
    assert [row["content"] for row in user_rows] == [DEMO_MESSAGE]


async def test_the_handoff_sentinel_never_reaches_the_stored_transcript(
    authed_client, scripted_provider, supabase
):
    """The sentinel is protocol plumbing. Persisting it would replay it into a
    later prompt and teach the model to imitate the shape instead of calling
    the tool."""
    client, _user = authed_client
    await _open_account(client)

    scripted_provider(
        ModelResponse(tool_calls=[_handoff_call("banking")]),
        ModelResponse(text="gata"),
    )

    resp = await client.post("/api/v1/chat", json={"message": DEMO_MESSAGE})
    conversation_id = resp.json()["conversation_id"]

    rows = (
        await supabase.table("messages")
        .select("content")
        .eq("conversation_id", conversation_id)
        .order("created_at")
        .execute()
    ).data

    assert "__handoff__" not in json.dumps(rows)


async def test_history_endpoint_reads_both_hops_back_with_their_routing(
    authed_client, scripted_provider
):
    """What the frontend's history replay actually consumes."""
    client, _user = authed_client
    await _open_account(client)

    scripted_provider(
        ModelResponse(tool_calls=[_handoff_call("banking")]),
        ModelResponse(text="gata"),
    )

    resp = await client.post("/api/v1/chat", json={"message": DEMO_MESSAGE})
    conversation_id = resp.json()["conversation_id"]

    messages = (
        await client.get(f"/api/v1/chat/conversations/{conversation_id}/messages")
    ).json()

    routed = [m for m in messages if m.get("routing")]
    assert [m["routing"]["agent_name"] for m in routed] == ["insights", "banking"]
    assert routed[1]["routing"]["handoff_from"] == "insights"


# ---------------------------------------------------------------------------
# The response contract
# ---------------------------------------------------------------------------


async def test_single_agent_turn_returns_a_one_element_chain(
    authed_client, scripted_provider
):
    """No handoff is the same shape as a handoff, one hop shorter - a client
    has one thing to render, not two cases."""
    client, _user = authed_client
    scripted_provider(ModelResponse(text="Soldul tău este disponibil."))

    body = (await client.post("/api/v1/chat", json={"message": "care este soldul meu?"})).json()

    assert [hop["agent_name"] for hop in body["routing_chain"]] == ["banking"]
    assert body["routing_chain"][0]["handoff_from"] is None


async def test_routing_stays_populated_for_clients_that_predate_the_chain(
    authed_client, scripted_provider
):
    """`routing` is kept as the LAST hop's decision: it is the agent that
    produced `reply`, which is what a single-agent client would have wanted to
    label the reply with anyway."""
    client, _user = authed_client
    await _open_account(client)

    scripted_provider(
        ModelResponse(tool_calls=[_handoff_call("banking")]),
        ModelResponse(text="gata"),
    )

    body = (await client.post("/api/v1/chat", json={"message": DEMO_MESSAGE})).json()

    assert body["routing"]["agent_name"] == "banking"
    assert body["routing"] == body["routing_chain"][-1]


# ---------------------------------------------------------------------------
# The gates, over HTTP
# ---------------------------------------------------------------------------


async def test_a_handoff_to_documents_is_refused_over_http(
    authed_client, scripted_provider
):
    """The quarantine holds through the real stack: the turn ends with the
    source agent's own reply rather than reaching DocumentAgent."""
    client, _user = authed_client
    await _open_account(client)

    provider = scripted_provider(ModelResponse(tool_calls=[_handoff_call("documents")]))

    body = (await client.post("/api/v1/chat", json={"message": DEMO_MESSAGE})).json()

    assert [hop["agent_name"] for hop in body["routing_chain"]] == ["insights"]
    assert body["proposal"] is None
    # Exactly one provider call: InsightsAgent's loop stops at the handoff, and
    # DocumentAgent is never reached, so nothing else asks the model anything.
    assert provider.call_count == 1
    # The source agent had written nothing when it asked, so the user gets the
    # refusal fallback rather than an empty bubble (see HANDOFF_REFUSED_REPLY).
    assert body["reply"] == HANDOFF_REFUSED_REPLY


async def test_statement_mode_blocks_the_handoff_over_http(
    authed_client, scripted_provider, supabase
):
    """With a statement active the whole turn goes to DocumentAgent anyway (the
    context-first override), so the banking gate is never even the thing that
    stops it - belt and braces, and this proves the outer one still holds after
    Step 15 restructured dispatch."""
    client, user = authed_client
    await _open_account(client)

    statement = (
        await supabase.table("statements")
        .insert(
            # Only the columns Step 13's schema requires - this test needs a
            # statement to EXIST and be owned, not to have any rows in it.
            {"user_id": str(user.id), "bank_name": "BanK", "currency": "RON"}
        )
        .execute()
    ).data[0]

    scripted_provider(ModelResponse(text="Extrasul conține..."))

    body = (
        await client.post(
            "/api/v1/chat",
            json={"message": DEMO_MESSAGE, "statement_id": statement["id"]},
        )
    ).json()

    assert [hop["agent_name"] for hop in body["routing_chain"]] == ["documents"]
