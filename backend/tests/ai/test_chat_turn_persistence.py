"""How a multi-agent turn is written down, and what comes back over HTTP.

Offline, like the rest of tests/ai: `conversations_service.append_message` is
replaced with a recorder, so this is about the ORDER and the ROUTING ATTACHMENT
that `chat/router.py::_persist_turn` produces - the two things a replayed chain
depends on - rather than about Supabase.

tests/integration/test_chat_handoff.py covers the same ground against the real
database and the real HTTP stack; this file exists so the shape is pinned down
somewhere that cannot flake on a network round trip.
"""

from __future__ import annotations

import uuid

import pytest

from app.ai.routing import RoutingDecision
from app.ai.schemas import Message, ToolCall
from app.ai.turn import TurnDispatchResult, TurnResult
from app.modules.chat import conversations_service
from app.modules.chat import router as chat_router
from app.modules.chat.schemas import ChatResponse

CONVERSATION_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
USER_MESSAGE = "vreau sa vad abonamentele mele recurente"


def insights_decision() -> RoutingDecision:
    return RoutingDecision(
        agent_name="insights",
        reason="Matched rule: keywords 'recurent'",
        confidence=1.0,
        matched_rule="insights_categories",
    )


def banking_decision() -> RoutingDecision:
    return RoutingDecision(
        agent_name="banking",
        reason="plata recurenta pe care utilizatorul vrea sa o opreasca",
        confidence=1.0,
        matched_rule="handoff_from:insights",
        handoff_from="insights",
    )


def two_hop_turn() -> TurnDispatchResult:
    """Insights hands off after one tool call; Banking answers."""
    insights_hop = TurnResult(
        # Empty: the source agent handed off before saying anything, which is
        # the normal case (a model emits either text or tool calls, not both).
        reply="",
        trace=[
            Message(
                role="assistant",
                tool_calls=[ToolCall(id="c1", name="handoff_to_agent", arguments={})],
            ),
            Message(
                role="tool",
                content='{"ok": true}',
                tool_call_id="c1",
                name="handoff_to_agent",
            ),
        ],
        routing=insights_decision(),
    )
    banking_hop = TurnResult(
        reply="Am pregatit o propunere de anulare a cardului.",
        trace=[
            Message(
                role="assistant",
                tool_calls=[ToolCall(id="c2", name="propose_cancel_card", arguments={})],
            ),
            Message(
                role="tool",
                content='{"ok": true}',
                tool_call_id="c2",
                name="propose_cancel_card",
            ),
        ],
        routing=banking_decision(),
    )
    return TurnDispatchResult(hops=[insights_hop, banking_hop])


@pytest.fixture
def recorded(monkeypatch) -> list[tuple[Message, RoutingDecision | None]]:
    """Capture what `_persist_turn` would write, in order."""
    calls: list[tuple[Message, RoutingDecision | None]] = []

    async def fake_append(supabase, conversation_id, message, routing=None):
        assert conversation_id == CONVERSATION_ID
        calls.append((message, routing))
        return {}

    monkeypatch.setattr(conversations_service, "append_message", fake_append)
    return calls


# ---------------------------------------------------------------------------
# Per-hop persistence
# ---------------------------------------------------------------------------


async def test_a_two_hop_turn_is_written_in_execution_order(recorded):
    await chat_router._persist_turn(None, CONVERSATION_ID, USER_MESSAGE, two_hop_turn())

    roles = [message.role for message, _ in recorded]
    # user | insights' trace | insights' (empty) reply | banking's trace | banking's reply
    assert roles == ["user", "assistant", "tool", "assistant", "assistant", "tool", "assistant"]


async def test_routing_rides_on_each_hops_final_assistant_row_only(recorded):
    """The same is_final_reply convention as before Step 15, applied once per
    hop rather than once per turn. An intermediate trace row never carries it -
    a tool result is not a decision."""
    await chat_router._persist_turn(None, CONVERSATION_ID, USER_MESSAGE, two_hop_turn())

    attached = [routing for _, routing in recorded]
    assert [r is not None for r in attached] == [False, False, False, True, False, False, True]

    first_hop, second_hop = attached[3], attached[6]
    assert first_hop is not None and second_hop is not None
    assert first_hop.agent_name == "insights"
    assert first_hop.handoff_from is None
    assert second_hop.agent_name == "banking"
    # THE link a replayed chain is reconstructed from.
    assert second_hop.handoff_from == first_hop.agent_name


async def test_a_hop_that_said_nothing_still_gets_its_row(recorded):
    """Its routing row lives on that assistant row. Dropping it because the
    content is empty would erase the first half of every chain from the stored
    history - the frontend skips empty-content rows when drawing bubbles, so
    keeping it costs no blank bubble."""
    await chat_router._persist_turn(None, CONVERSATION_ID, USER_MESSAGE, two_hop_turn())

    empty_row, routing = recorded[3]
    assert empty_row.role == "assistant"
    assert empty_row.content == ""
    assert routing is not None


async def test_the_model_authored_context_hint_is_never_written_as_a_user_turn(recorded):
    """Storing it as a `user` row would put words in the user's mouth in their
    own transcript - and feed them back as real user input next turn."""
    await chat_router._persist_turn(None, CONVERSATION_ID, USER_MESSAGE, two_hop_turn())

    user_rows = [message for message, _ in recorded if message.role == "user"]
    assert [m.content for m in user_rows] == [USER_MESSAGE]


async def test_persist_returns_every_written_message_for_proposal_extraction(recorded):
    """`_extract_proposal` scans this, so a propose_* result produced by the
    SECOND hop has to be in it - otherwise a handed-off proposal would never
    reach ChatResponse.proposal."""
    written = await chat_router._persist_turn(
        None, CONVERSATION_ID, USER_MESSAGE, two_hop_turn()
    )

    assert [m.name for m in written if m.role == "tool"] == [
        "handoff_to_agent",
        "propose_cancel_card",
    ]


async def test_a_single_agent_turn_persists_exactly_as_it_used_to(recorded):
    """No handoff means one hop, and one hop is the pre-Step-15 layout: user
    turn, trace, final reply carrying the routing."""
    turn = TurnDispatchResult(
        hops=[
            TurnResult(
                reply="Soldul tau este disponibil.",
                trace=[Message(role="assistant", tool_calls=[])],
                routing=RoutingDecision(agent_name="banking", reason="r", confidence=1.0),
            )
        ]
    )

    await chat_router._persist_turn(None, CONVERSATION_ID, "care e soldul", turn)

    assert [m.role for m, _ in recorded] == ["user", "assistant", "assistant"]
    assert [r is not None for _, r in recorded] == [False, False, True]


# ---------------------------------------------------------------------------
# The response contract
# ---------------------------------------------------------------------------


def test_chat_response_carries_the_whole_chain():
    turn = two_hop_turn()

    response = ChatResponse(
        reply=turn.final_reply,
        conversation_id=CONVERSATION_ID,
        routing_chain=turn.routing_chain,
        routing=turn.routing_chain[-1],
    )

    assert [d.agent_name for d in response.routing_chain] == ["insights", "banking"]
    assert response.reply == "Am pregatit o propunere de anulare a cardului."


def test_chat_response_routing_duplicates_the_last_hop_for_older_clients():
    """The LAST hop rather than the first: it is the agent that produced the
    reply, which is what a client predating the chain would have wanted to
    label that reply with."""
    turn = two_hop_turn()

    response = ChatResponse(
        reply=turn.final_reply,
        conversation_id=CONVERSATION_ID,
        routing_chain=turn.routing_chain,
        routing=turn.routing_chain[-1],
    )

    assert response.routing == response.routing_chain[-1]
    assert response.routing is not None and response.routing.agent_name == "banking"


def test_chat_response_defaults_to_an_empty_chain():
    """Additive field: a response built without it is still valid, so nothing
    that constructs a ChatResponse elsewhere breaks on the new contract."""
    response = ChatResponse(reply="hi", conversation_id=CONVERSATION_ID)

    assert response.routing_chain == []
    assert response.routing is None
