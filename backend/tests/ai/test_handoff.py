"""Step 15: cross-agent handoff — the turn model, the tool, and the gates.

Offline like the rest of tests/ai: the mock provider is the only provider and
nothing here touches a database.

What is actually being tested is that a handoff is a REQUEST and never more
than that. Every test below that expects a rejection is checking a gate that
exists because the alternative is an agent talking its way somewhere it was
not allowed to go: the DocumentAgent quarantine, the per-source allow-list,
the cycle set, the hop cap, and the statement-mode gate.
"""

from __future__ import annotations

import json
import logging

import pytest
from pydantic import ValidationError

from app.ai.agents.banking_agent import BankingAgent
from app.ai.agents.document_agent import DocumentAgent
from app.ai.agents.insights_agent import InsightsAgent
from app.ai.agents.planning_agent import PlanningAgent
from app.ai.agents.tool_loop import ToolLoopAgent
from app.ai.context import Context
from app.ai.orchestrator import (
    ALLOWED_HANDOFF_TARGETS,
    HANDOFF_REFUSED_REPLY,
    MAX_HOPS,
    Orchestrator,
)
from app.ai.providers.mock_provider import MockProvider
from app.ai.routing import RoutingDecision
from app.ai.schemas import Message, ModelResponse, ToolCall
from app.ai.service import (
    build_banking_tools,
    build_document_tools,
    build_insights_tools,
    build_planning_tools,
)
from app.ai.tools.handoff_tool import (
    HANDOFF_SENTINEL_KEY,
    HANDOFF_TRACE_MARKER,
    HandoffToAgentTool,
)
from app.ai.tools.registry import ToolRegistry
from app.ai.turn import HandoffRequest, TurnDispatchResult, TurnResult
from tests.ai.conftest import OWNED_ACCOUNT_IDS, TEST_USER_ID, FakeSupabase

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def handoff_call(
    target: str = "banking",
    *,
    reason: str = "plata recurenta pe care utilizatorul vrea sa o opreasca",
    hint: str = "Utilizatorul vrea sa anuleze cardul pe care se ia abonamentul.",
    call_id: str = "call-handoff",
) -> ToolCall:
    return ToolCall(
        id=call_id,
        name="handoff_to_agent",
        arguments={"target_agent": target, "reason": reason, "context_hint": hint},
    )


def handoff_script(target: str = "banking") -> list[ModelResponse]:
    """What a model does when it decides to hand the turn on."""
    return [ModelResponse(tool_calls=[handoff_call(target)])]


class _HandoffAgent(ToolLoopAgent):
    """An agent that always requests a handoff to `target`, once.

    Bypasses the tool registry deliberately: several tests need a SOURCE that
    is not allowed to hand off at all (BankingAgent, DocumentAgent), and the
    only honest way to prove dispatch refuses it is to have such an agent
    request one anyway. A real one cannot - it has no such tool.
    """

    fallback_reply = "handoff-agent fallback"

    def __init__(self, name: str, target: str, *, reply: str = "") -> None:
        self.name = name  # type: ignore[misc]
        super().__init__(
            MockProvider([ModelResponse(text="x")], repeat_last=True), ToolRegistry()
        )
        self._target = target
        self._reply = reply
        self.contexts_seen: list[Context] = []
        self.messages_seen: list[list[Message]] = []

    async def run(self, messages, context) -> TurnResult:  # type: ignore[no-untyped-def]
        self.contexts_seen.append(context)
        self.messages_seen.append(list(messages))
        return TurnResult(
            reply=self._reply,
            trace=[],
            handoff=HandoffRequest(
                target_agent=self._target,
                reason=f"{self.name} wants {self._target}",
                context_hint=f"hint from {self.name}",
            ),
        )


class _RecordingAgent(ToolLoopAgent):
    """A terminal agent that records exactly what it was run with."""

    fallback_reply = "recording fallback"

    def __init__(self, name: str, reply: str = "final answer") -> None:
        self.name = name  # type: ignore[misc]
        super().__init__(
            MockProvider([ModelResponse(text=reply)], repeat_last=True), ToolRegistry()
        )
        self._reply = reply
        self.contexts_seen: list[Context] = []
        self.messages_seen: list[list[Message]] = []

    async def run(self, messages, context) -> TurnResult:  # type: ignore[no-untyped-def]
        self.contexts_seen.append(context)
        self.messages_seen.append(list(messages))
        return TurnResult(reply=self._reply, trace=[])


def _insights_tools(supabase: FakeSupabase | None = None) -> ToolRegistry:
    """The real insights registry, including `handoff_to_agent`.

    `build_insights_tools` takes a provider for the categorisation classifier
    (see categorize_transactions.py). Nothing in this file categorises
    anything, so it gets its own throwaway model rather than sharing - and
    eating responses from - the agent's scripted one.
    """
    return build_insights_tools(
        supabase or FakeSupabase(), MockProvider([ModelResponse(text="{}")], repeat_last=True)
    )


def _insights_orchestrator(
    insights_script: list[ModelResponse],
    banking_script: list[ModelResponse],
    *,
    supabase: FakeSupabase | None = None,
) -> Orchestrator:
    """A real Insights + real Banking orchestrator over two scripted models."""
    db = supabase or FakeSupabase()
    orchestrator = Orchestrator()
    orchestrator.register(
        InsightsAgent(MockProvider(insights_script, repeat_last=True), _insights_tools(db))
    )
    orchestrator.register(
        BankingAgent(MockProvider(banking_script, repeat_last=True), build_banking_tools(db)),
        default=True,
    )
    return orchestrator


# ---------------------------------------------------------------------------
# 1. The turn types
# ---------------------------------------------------------------------------


def test_handoff_request_is_frozen():
    """A record of a decision already taken - nothing downstream may retarget
    it after the fact, same reasoning as RoutingDecision."""
    request = HandoffRequest(target_agent="banking", reason="r", context_hint="h")

    with pytest.raises(ValidationError):
        request.target_agent = "documents"  # type: ignore[misc]


def test_turn_result_defaults_to_a_plain_finished_turn():
    result = TurnResult(reply="done")

    assert result.trace == []
    assert result.handoff is None
    # The agent doesn't know why it was picked; dispatch stamps this on.
    assert result.routing is None


def test_turn_result_is_mutable_so_dispatch_can_stamp_routing():
    """Deliberately NOT frozen, unlike HandoffRequest - see TurnResult."""
    result = TurnResult(reply="done")
    decision = RoutingDecision(agent_name="banking", reason="r", confidence=1.0)

    result.routing = decision

    assert result.routing is decision


def test_turn_dispatch_result_needs_at_least_one_hop():
    with pytest.raises(ValidationError):
        TurnDispatchResult(hops=[])


def test_turn_dispatch_result_exposes_the_chain_and_the_flat_transcript():
    first = TurnResult(
        reply="",
        trace=[Message(role="assistant", content=None)],
        routing=RoutingDecision(agent_name="insights", reason="r1", confidence=1.0),
    )
    second = TurnResult(
        reply="visible answer",
        trace=[],
        routing=RoutingDecision(
            agent_name="banking", reason="r2", confidence=1.0, handoff_from="insights"
        ),
    )
    turn = TurnDispatchResult(hops=[first, second])

    assert [d.agent_name for d in turn.routing_chain] == ["insights", "banking"]
    # The last hop is the one that answered the user.
    assert turn.final_reply == "visible answer"
    # Every hop contributes its trace AND its assistant row - including the
    # empty one, which is where the first hop's routing_metadata rides.
    assert [m.role for m in turn.new_messages] == ["assistant", "assistant", "assistant"]


def test_final_reply_falls_back_to_the_last_hop_that_said_anything():
    """A last hop that produced no text still shows the user something."""
    turn = TurnDispatchResult(
        hops=[TurnResult(reply="something"), TurnResult(reply="")]
    )

    assert turn.final_reply == "something"


def test_routing_decision_handoff_from_defaults_to_none_and_needs_no_migration():
    """Old routing_metadata rows have no `handoff_from` key at all. They must
    read back as a plain first-hop decision, not fail validation - that is what
    makes this field additive over JSONB with no migration."""
    stored_before_step_15 = {
        "agent_name": "banking",
        "reason": "Matched rule: keywords 'sold'",
        "confidence": 1.0,
        "matched_rule": "banking_keywords",
    }

    decision = RoutingDecision(**stored_before_step_15)

    assert decision.handoff_from is None
    assert "handoff_from" in decision.model_dump()


# ---------------------------------------------------------------------------
# 2. The tool and the loop
# ---------------------------------------------------------------------------


async def test_handoff_tool_returns_the_sentinel(context):
    result = await HandoffToAgentTool().execute(handoff_call("planning"), context)

    assert result.ok
    assert result.data is not None
    assert result.data[HANDOFF_SENTINEL_KEY]["target"] == "planning"


async def test_handoff_tool_rejects_a_missing_argument(context):
    """Model-authored arguments go through the schema like any other tool's."""
    call = ToolCall(id="c", name="handoff_to_agent", arguments={"target_agent": "banking"})

    result = await HandoffToAgentTool().execute(call, context)

    assert not result.ok
    assert "invalid input" in (result.error or "")


async def test_tool_loop_stops_at_the_handoff_and_reports_it(context):
    """The sentinel ends the loop: there is nothing left for this model to say
    once another agent owns the rest of the turn."""
    provider = MockProvider(
        [
            ModelResponse(tool_calls=[handoff_call("banking")]),
            # Would be used if the loop kept going. It must not be.
            ModelResponse(text="this must never be reached"),
        ]
    )
    agent = InsightsAgent(provider, _insights_tools())

    result = await agent.run([Message(role="user", content="abonamente?")], context)

    assert result.handoff is not None
    assert result.handoff.target_agent == "banking"
    assert result.reply == ""
    assert provider.call_count == 1


async def test_tool_loop_strips_the_sentinel_out_of_the_trace(context):
    """The raw sentinel is protocol plumbing. Persisting it would replay it
    into a later prompt and teach the model to imitate the shape rather than
    call the tool."""
    provider = MockProvider([ModelResponse(tool_calls=[handoff_call("banking")])])
    agent = InsightsAgent(provider, _insights_tools())

    result = await agent.run([Message(role="user", content="abonamente?")], context)

    tool_messages = [m for m in result.trace if m.role == "tool"]
    assert len(tool_messages) == 1
    payload = json.loads(tool_messages[0].content or "{}")
    assert HANDOFF_SENTINEL_KEY not in json.dumps(payload)
    assert payload["result"] == {"status": HANDOFF_TRACE_MARKER}


async def test_a_failed_handoff_tool_call_is_not_a_handoff(context):
    """A rejected argument set degrades to an ordinary tool error, and the loop
    carries on - it does not become a handoff to nowhere."""
    bad_call = ToolCall(id="c", name="handoff_to_agent", arguments={})
    provider = MockProvider(
        [ModelResponse(tool_calls=[bad_call]), ModelResponse(text="scuze, reformulez")]
    )
    agent = InsightsAgent(provider, _insights_tools())

    result = await agent.run([Message(role="user", content="abonamente?")], context)

    assert result.handoff is None
    assert result.reply == "scuze, reformulez"


# ---------------------------------------------------------------------------
# 3. Which agents hold the tool at all
# ---------------------------------------------------------------------------


def test_banking_agent_cannot_hand_off_because_it_has_no_such_tool():
    """BankingAgent is TERMINAL. The check is structural, not prompt-level:
    the tool simply is not in its registry, so no phrasing can reach it."""
    assert build_banking_tools(FakeSupabase()).get("handoff_to_agent") is None
    assert "banking" not in ALLOWED_HANDOFF_TARGETS


def test_document_agent_cannot_hand_off_because_it_has_no_such_tool():
    """THE isolation invariant (see build_document_tools). DocumentAgent reads
    untrusted uploaded content; a handoff tool in this registry would end its
    quarantine in one line."""
    tools = build_document_tools(FakeSupabase())

    assert tools.names() == ["read_document", "summarize_statement"]
    assert tools.get("handoff_to_agent") is None


def test_insights_and_planning_are_the_only_agents_that_can_hand_off():
    assert _insights_tools().get("handoff_to_agent") is not None
    assert build_planning_tools(FakeSupabase()).get("handoff_to_agent") is not None
    assert set(ALLOWED_HANDOFF_TARGETS) == {"insights", "planning"}


def test_documents_is_not_a_permitted_target_of_any_source():
    """The quarantine's other direction, at the table level."""
    for targets in ALLOWED_HANDOFF_TARGETS.values():
        assert "documents" not in targets


# ---------------------------------------------------------------------------
# 4. Dispatch: the happy paths
# ---------------------------------------------------------------------------


async def test_single_agent_turn_is_a_one_hop_chain(context):
    """Unchanged behaviour, expressed in the new shape."""
    orchestrator = _insights_orchestrator(
        [ModelResponse(text="analiza mea")], [ModelResponse(text="unused")]
    )

    turn = await orchestrator.dispatch(
        [Message(role="user", content="analizeaza-mi cheltuielile")],
        "analizeaza-mi cheltuielile",
        context,
    )

    assert len(turn.hops) == 1
    assert turn.final_reply == "analiza mea"
    assert turn.hops[0].handoff is None
    assert turn.routing_chain[0].handoff_from is None


async def test_two_hop_handoff_runs_both_agents_and_records_both(context):
    """Insights hands off; Banking finishes the turn. Two hops, two routing
    rows, and the second one names where it came from."""
    orchestrator = _insights_orchestrator(
        handoff_script("banking"), [ModelResponse(text="Am pregatit propunerea.")]
    )

    turn = await orchestrator.dispatch(
        [Message(role="user", content="ce abonamente recurente am?")],
        "ce abonamente recurente am?",
        context,
    )

    assert [d.agent_name for d in turn.routing_chain] == ["insights", "banking"]
    assert turn.final_reply == "Am pregatit propunerea."

    first, second = turn.routing_chain
    assert first.handoff_from is None
    assert second.handoff_from == "insights"
    assert second.matched_rule == "handoff_from:insights"
    assert second.confidence == 1.0
    # The reason on the target hop is the SOURCE's stated reason, so an audit
    # trail says why the turn changed hands rather than just that it did.
    assert "opreasca" in second.reason


async def test_the_target_agent_is_prompted_with_the_context_hint(context):
    """The hint becomes the target's user message - and only that. It is
    appended to the conversation, never substituted for it."""
    target = _RecordingAgent("banking")
    orchestrator = Orchestrator()
    orchestrator.register(_HandoffAgent("insights", "banking"))
    orchestrator.register(target)

    await orchestrator.dispatch(
        [Message(role="user", content="ce abonamente am?")], "ce abonamente am?", context
    )

    seen = target.messages_seen[0]
    assert [m.content for m in seen] == ["ce abonamente am?", "hint from insights"]
    assert seen[-1].role == "user"


async def test_both_agents_run_on_the_exact_same_context_object(context):
    """Identity is built once per turn and never rebuilt on a handoff, so a
    handoff cannot widen account access by construction. Asserted by identity,
    not equality - an equal copy would still be a second Context."""
    source = _HandoffAgent("insights", "banking")
    target = _RecordingAgent("banking")
    orchestrator = Orchestrator()
    orchestrator.register(source)
    orchestrator.register(target)

    await orchestrator.dispatch([Message(role="user", content="hi")], "hi", context)

    assert source.contexts_seen[0] is context
    assert target.contexts_seen[0] is context


async def test_route_is_not_called_again_on_a_handoff(context, monkeypatch):
    """Re-running route() would re-apply the context-first override, so every
    handoff in a document/statement conversation would ping-pong back into
    DocumentAgent - through the isolation that override exists to protect."""
    orchestrator = Orchestrator()
    orchestrator.register(_HandoffAgent("insights", "banking"))
    orchestrator.register(_RecordingAgent("banking"))

    calls: list[str] = []
    original = orchestrator.route

    def counting_route(message: str, ctx: Context) -> RoutingDecision:
        calls.append(message)
        return original(message, ctx)

    monkeypatch.setattr(orchestrator, "route", counting_route)

    turn = await orchestrator.dispatch(
        [Message(role="user", content="analiza")], "analiza", context
    )

    assert len(turn.hops) == 2
    assert calls == ["analiza"]


async def test_insights_may_hand_off_to_planning(context):
    """The second permitted edge out of Insights."""
    orchestrator = Orchestrator()
    orchestrator.register(_HandoffAgent("insights", "planning"))
    orchestrator.register(_RecordingAgent("banking"))
    orchestrator.register(
        PlanningAgent(
            MockProvider([ModelResponse(text="planul tau")]), build_planning_tools(FakeSupabase())
        )
    )

    turn = await orchestrator.dispatch(
        [Message(role="user", content="analiza")], "analiza", context
    )

    assert [d.agent_name for d in turn.routing_chain] == ["insights", "planning"]
    assert turn.final_reply == "planul tau"


# ---------------------------------------------------------------------------
# 5. Dispatch: every gate
# ---------------------------------------------------------------------------


async def test_hop_cap_ends_the_turn(context, caplog):
    """Two agents that each want the other to answer must not burn a request
    between them. After MAX_HOPS the chain ends with whatever the last agent
    that ran had to say."""
    orchestrator = Orchestrator()
    orchestrator.register(_HandoffAgent("insights", "planning", reply="insights text"))
    # planning -> banking -> planning would cycle, so the third hop is what
    # trips the cap rather than the visited set: give each a fresh target.
    orchestrator.register(_HandoffAgent("planning", "banking", reply="planning text"))
    orchestrator.register(_HandoffAgent("banking", "docs", reply="banking text"), default=False)

    with caplog.at_level(logging.WARNING, logger="app.ai.orchestrator"):
        turn = await orchestrator.dispatch(
            [Message(role="user", content="analiza")], "analiza", context
        )

    assert len(turn.hops) == MAX_HOPS + 1
    assert turn.final_reply == "banking text"
    assert "handoff cap reached" in caplog.text


async def test_a_cycle_back_to_an_agent_that_already_ran_is_rejected(
    context, caplog, monkeypatch
):
    """No A->B->A. Coming back would re-read the same data and reach the same
    conclusion - the hop cap would stop it eventually, but late and after
    paying for it.

    ALLOWED_HANDOFF_TARGETS is widened here on purpose. As it actually ships,
    no cycle is even reachable: banking is terminal, so nothing can hand back.
    The visited set is defense in depth for the day someone adds an edge - and
    a guard nobody can reach is a guard nobody knows is broken. Widening the
    table is what makes this test exercise the check rather than re-testing
    the allow-list one line above it."""
    monkeypatch.setitem(ALLOWED_HANDOFF_TARGETS, "banking", frozenset({"insights"}))

    orchestrator = Orchestrator()
    orchestrator.register(_HandoffAgent("insights", "banking", reply="insights text"))
    orchestrator.register(_HandoffAgent("banking", "insights", reply="banking text"))

    with caplog.at_level(logging.WARNING, logger="app.ai.orchestrator"):
        turn = await orchestrator.dispatch(
            [Message(role="user", content="analiza")], "analiza", context
        )

    assert [d.agent_name for d in turn.routing_chain] == ["insights", "banking"]
    assert turn.final_reply == "banking text"
    assert "already ran this turn" in caplog.text


async def test_documents_is_rejected_as_a_handoff_target(context, caplog):
    """The quarantine, enforced in dispatch rather than only by omission from
    the allow-list - this one is a security boundary, not a policy knob."""
    orchestrator = Orchestrator()
    orchestrator.register(_HandoffAgent("insights", "documents", reply="insights text"))
    orchestrator.register(
        DocumentAgent(
            MockProvider([ModelResponse(text="nu")]), build_document_tools(FakeSupabase())
        )
    )
    orchestrator.register(_RecordingAgent("banking"))

    with caplog.at_level(logging.WARNING, logger="app.ai.orchestrator"):
        turn = await orchestrator.dispatch(
            [Message(role="user", content="analiza")], "analiza", context
        )

    assert len(turn.hops) == 1
    assert turn.final_reply == "insights text"
    assert "quarantined" in caplog.text


async def test_an_agent_with_no_allow_list_cannot_hand_off_at_all(context, caplog):
    """BankingAgent has no real way to ask - it holds no handoff tool. If it
    somehow did, dispatch would still refuse: it has no allow-list entry."""
    orchestrator = Orchestrator()
    orchestrator.register(_HandoffAgent("banking", "insights", reply="banking text"))
    orchestrator.register(_RecordingAgent("insights"))

    with caplog.at_level(logging.WARNING, logger="app.ai.orchestrator"):
        turn = await orchestrator.dispatch(
            [Message(role="user", content="sold")], "sold", context
        )

    assert len(turn.hops) == 1
    assert turn.final_reply == "banking text"
    assert "not in that agent's allow-list" in caplog.text


async def test_a_target_outside_the_allow_list_is_rejected(context, caplog):
    """`target_agent` is a name the MODEL produced. Insights may reach banking
    and planning; naming anything else is refused here, not in the tool that
    carried the argument."""
    orchestrator = Orchestrator()
    orchestrator.register(_HandoffAgent("insights", "docs", reply="insights text"))
    orchestrator.register(_RecordingAgent("docs"))
    orchestrator.register(_RecordingAgent("banking"))

    with caplog.at_level(logging.WARNING, logger="app.ai.orchestrator"):
        turn = await orchestrator.dispatch(
            [Message(role="user", content="analiza")], "analiza", context
        )

    assert len(turn.hops) == 1
    assert turn.final_reply == "insights text"
    assert "not in that agent's allow-list" in caplog.text


async def test_an_invented_target_name_is_rejected(context, caplog):
    orchestrator = Orchestrator()
    orchestrator.register(_HandoffAgent("insights", "superuser", reply="insights text"))
    orchestrator.register(_RecordingAgent("banking"))

    with caplog.at_level(logging.WARNING, logger="app.ai.orchestrator"):
        turn = await orchestrator.dispatch(
            [Message(role="user", content="analiza")], "analiza", context
        )

    assert len(turn.hops) == 1
    # Never log the model's invented name as free text.
    assert "superuser" not in caplog.text


# ---------------------------------------------------------------------------
# 6. The statement-mode gate (the Step 13 interaction)
# ---------------------------------------------------------------------------


def _statement_context() -> Context:
    return Context(
        user_id=TEST_USER_ID,
        account_ids=OWNED_ACCOUNT_IDS,
        statement_id="stmt-0001",
    )


async def _dispatch_insights_to_banking(ctx: Context) -> TurnDispatchResult:
    orchestrator = Orchestrator()
    orchestrator.register(_HandoffAgent("insights", "banking", reply="insights text"))
    orchestrator.register(_RecordingAgent("banking"))
    # route() would send a statement-mode turn to DocumentAgent, which is not
    # registered here; with it absent, route falls through to keyword rules and
    # lands on insights. That keeps this test about the GATE, not about routing.
    return await orchestrator.dispatch(
        [Message(role="user", content="analizeaza")], "analizeaza", ctx
    )


async def test_statement_mode_blocks_a_handoff_into_banking(caplog):
    """With a statement active the insights tools read `statement_rows`, so the
    ids they report are statement_rows.id values and NOT ledger references. A
    banking action taken off the back of one would be reasoning about ids that
    mean something else entirely - silently unsafe, so it is refused."""
    with caplog.at_level(logging.WARNING, logger="app.ai.orchestrator"):
        turn = await _dispatch_insights_to_banking(_statement_context())

    assert len(turn.hops) == 1
    assert turn.final_reply == "insights text"
    assert "statement_rows ids" in caplog.text


async def test_the_same_handoff_proceeds_with_no_statement_active(context):
    """The mirror of the test above: the gate is about statement mode, not
    about the insights -> banking edge."""
    turn = await _dispatch_insights_to_banking(context)

    assert [d.agent_name for d in turn.routing_chain] == ["insights", "banking"]


async def test_statement_mode_does_not_block_a_handoff_into_planning():
    """The gate is scoped to banking - PlanningAgent takes no action on ids."""
    orchestrator = Orchestrator()
    orchestrator.register(_HandoffAgent("insights", "planning", reply="insights text"))
    orchestrator.register(_RecordingAgent("planning"))
    orchestrator.register(_RecordingAgent("banking"))

    turn = await orchestrator.dispatch(
        [Message(role="user", content="analizeaza")], "analizeaza", _statement_context()
    )

    assert [d.agent_name for d in turn.routing_chain] == ["insights", "planning"]


async def test_a_statement_conversation_still_routes_to_documents_first():
    """The context-first override is untouched by Step 15: with a statement
    active and DocumentAgent registered, the turn starts there - and ends
    there, since DocumentAgent has no handoff tool."""
    orchestrator = Orchestrator()
    orchestrator.register(
        DocumentAgent(
            MockProvider([ModelResponse(text="extrasul contine...")]),
            build_document_tools(FakeSupabase()),
        )
    )
    orchestrator.register(_RecordingAgent("banking"))

    turn = await orchestrator.dispatch(
        [Message(role="user", content="transfera 50 RON")],
        "transfera 50 RON",
        _statement_context(),
    )

    assert [d.agent_name for d in turn.routing_chain] == ["documents"]


async def test_a_refused_handoff_never_leaves_the_user_with_an_empty_reply(context):
    """A real agent that ends its turn on a handoff call has written NOTHING -
    a model emits either text or tool calls, never both. So every gate above
    would hand the user a blank bubble if dispatch did not cover for it."""
    orchestrator = Orchestrator()
    # A REAL InsightsAgent, whose loop stops at the sentinel with reply="".
    orchestrator.register(
        InsightsAgent(
            MockProvider(handoff_script("documents")), _insights_tools()
        )
    )
    orchestrator.register(_RecordingAgent("banking"))

    turn = await orchestrator.dispatch(
        [Message(role="user", content="abonamente?")], "abonamente?", context
    )

    assert len(turn.hops) == 1
    assert turn.hops[0].handoff is not None  # it asked, and was refused
    assert turn.final_reply == HANDOFF_REFUSED_REPLY


async def test_an_honoured_handoff_leaves_the_source_reply_alone(context):
    """The fallback is scoped to a REFUSED request. A source that handed off
    successfully is represented by an empty reply on purpose - the target's
    answer is what the user reads for that leg."""
    orchestrator = Orchestrator()
    orchestrator.register(
        InsightsAgent(
            MockProvider(handoff_script("banking")), _insights_tools()
        )
    )
    orchestrator.register(_RecordingAgent("banking", reply="raspunsul bancar"))

    turn = await orchestrator.dispatch(
        [Message(role="user", content="abonamente?")], "abonamente?", context
    )

    assert turn.hops[0].reply == ""
    assert turn.final_reply == "raspunsul bancar"
