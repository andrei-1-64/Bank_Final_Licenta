"""Compound questions answered across a handoff (routing-fix pass).

The bug this covers: "Care e soldul meu și cât am cheltuit luna asta?" routed
to InsightsAgent on the `cheltui` + analytical-marker rule, InsightsAgent
answered the spending half, and the balance half was never addressed or even
acknowledged - the user got a confident answer to half of what they asked.

The fix reuses the Step 15 handoff machinery rather than adding a second path:
the source agent puts its own half in `handoff_to_agent`'s new `answer_so_far`
argument and the remaining half in `context_hint`, and dispatch runs the target
exactly as it does for the recurring-payment demo. Two things made that need a
new argument instead of plain text:

* `ModelResponse` carries EITHER text OR tool_calls, never both (see
  app/ai/schemas.py), and `ToolLoopAgent.run` returns the moment a model emits
  text. A source agent structurally cannot speak AND hand off in one turn.
* `TurnDispatchResult.final_reply` shows the LAST hop only, so even a source
  that could speak would have had its half dropped from the live response
  while still being persisted - see `combined_reply`.

Offline, like the rest of tests/ai: no provider, no network, no database.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.ai.agents.banking_agent import SYSTEM_PROMPT as BANKING_PROMPT
from app.ai.agents.banking_agent import BankingAgent
from app.ai.agents.insights_agent import SYSTEM_PROMPT as INSIGHTS_PROMPT
from app.ai.agents.insights_agent import InsightsAgent
from app.ai.agents.planning_agent import SYSTEM_PROMPT as PLANNING_PROMPT
from app.ai.agents.planning_agent import PlanningAgent
from app.ai.agents.tool_loop import ToolLoopAgent
from app.ai.context import Context
from app.ai.orchestrator import (
    HANDOFF_PARTIAL_NOTE,
    HANDOFF_REFUSED_REPLY,
    MAX_HOPS,
    Orchestrator,
)
from app.ai.providers.mock_provider import MockProvider
from app.ai.schemas import Message, ModelResponse, ToolCall
from app.ai.service import build_banking_tools, build_insights_tools, build_planning_tools
from app.ai.tools.handoff_tool import HANDOFF_SENTINEL_KEY, HandoffToAgentTool
from app.ai.tools.registry import ToolRegistry
from app.ai.turn import HandoffRequest, TurnDispatchResult, TurnResult
from tests.ai.conftest import OWNED_ACCOUNT_IDS, TEST_USER_ID, FakeSupabase

SPENDING_HALF = "Ai cheltuit 1.200,00 RON luna aceasta."
BALANCE_HALF = "Soldul tău este 3.500,00 RON."
COMPOUND_MESSAGE = "care e soldul meu si cat am cheltuit luna asta?"


def compound_handoff_call(
    target: str = "banking",
    *,
    answer: str = SPENDING_HALF,
    hint: str = "Utilizatorul vrea si soldul conturilor sale.",
    call_id: str = "call-compound",
) -> ToolCall:
    """A handoff that carries the source's own half of a compound answer."""
    return ToolCall(
        id=call_id,
        name="handoff_to_agent",
        arguments={
            "target_agent": target,
            "reason": "intrebare compusa; a doua parte e o citire bancara",
            "context_hint": hint,
            "answer_so_far": answer,
        },
    )


def _insights_tools(supabase: FakeSupabase) -> ToolRegistry:
    return build_insights_tools(
        supabase, MockProvider([ModelResponse(text="{}")], repeat_last=True)
    )


def _compound_orchestrator(
    *,
    insights_script: list[ModelResponse],
    banking_script: list[ModelResponse],
) -> Orchestrator:
    """Real Insights + real Banking, each over its own scripted model.

    DocumentAgent is deliberately not registered: nothing here attaches a
    document, and leaving it out keeps these tests about the handoff rather
    than about route()'s attachment branch (covered in
    test_orchestrator_routing.py).
    """
    supabase = FakeSupabase()
    orchestrator = Orchestrator()
    orchestrator.register(
        InsightsAgent(MockProvider(insights_script, repeat_last=True), _insights_tools(supabase))
    )
    orchestrator.register(
        BankingAgent(MockProvider(banking_script, repeat_last=True), build_banking_tools(supabase)),
        default=True,
    )
    return orchestrator


# ---------------------------------------------------------------------------
# 1. The happy path: both halves answered, in one turn, via a real handoff
# ---------------------------------------------------------------------------


async def test_compound_question_answers_both_halves_through_a_real_handoff(context):
    """Asserts the HANDOFF happened - a routing chain with `handoff_from` set -
    not merely that both figures appear somewhere in the text, which a single
    agent inventing numbers would also satisfy."""
    orchestrator = _compound_orchestrator(
        insights_script=[ModelResponse(tool_calls=[compound_handoff_call("banking")])],
        banking_script=[ModelResponse(text=BALANCE_HALF)],
    )

    turn = await orchestrator.dispatch(
        [Message(role="user", content=COMPOUND_MESSAGE)], COMPOUND_MESSAGE, context
    )

    # The turn really did change agent, and the chain records where from.
    assert [d.agent_name for d in turn.routing_chain] == ["insights", "banking"]
    assert turn.routing_chain[0].handoff_from is None
    assert turn.routing_chain[1].handoff_from == "insights"
    assert turn.routing_chain[1].matched_rule == "handoff_from:insights"

    # Both halves reach the user, in the order they were produced.
    assert turn.combined_reply == f"{SPENDING_HALF}\n\n{BALANCE_HALF}"


async def test_the_source_half_would_be_dropped_by_final_reply_alone(context):
    """Why `combined_reply` had to exist. `final_reply` answers "which hop
    finished the turn" and still does - it is simply not what the HTTP layer
    should show for a compound answer, which is why chat/router.py moved."""
    orchestrator = _compound_orchestrator(
        insights_script=[ModelResponse(tool_calls=[compound_handoff_call("banking")])],
        banking_script=[ModelResponse(text=BALANCE_HALF)],
    )

    turn = await orchestrator.dispatch(
        [Message(role="user", content=COMPOUND_MESSAGE)], COMPOUND_MESSAGE, context
    )

    assert turn.final_reply == BALANCE_HALF
    assert SPENDING_HALF not in turn.final_reply
    assert SPENDING_HALF in turn.combined_reply


async def test_the_source_half_is_persisted_as_its_own_hop(context):
    """The half the source answered is a real hop with its own reply, so the
    transcript stored by _persist_turn matches what the user was shown live."""
    orchestrator = _compound_orchestrator(
        insights_script=[ModelResponse(tool_calls=[compound_handoff_call("banking")])],
        banking_script=[ModelResponse(text=BALANCE_HALF)],
    )

    turn = await orchestrator.dispatch(
        [Message(role="user", content=COMPOUND_MESSAGE)], COMPOUND_MESSAGE, context
    )

    assert [hop.reply for hop in turn.hops] == [SPENDING_HALF, BALANCE_HALF]


# ---------------------------------------------------------------------------
# 2. The remaining half is never silently dropped
# ---------------------------------------------------------------------------


async def test_a_refused_compound_handoff_still_flags_the_unanswered_half():
    """The statement-mode gate refuses insights -> banking while a statement is
    active. The source's half must still show, WITH a note that the rest went
    unanswered - otherwise the gate reintroduces exactly the silent half-answer
    this work removed."""
    orchestrator = _compound_orchestrator(
        insights_script=[ModelResponse(tool_calls=[compound_handoff_call("banking")])],
        banking_script=[ModelResponse(text="should never run")],
    )
    statement_context = Context(
        user_id=TEST_USER_ID, account_ids=OWNED_ACCOUNT_IDS, statement_id="stmt-1111"
    )

    turn = await orchestrator.dispatch(
        [Message(role="user", content=COMPOUND_MESSAGE)], COMPOUND_MESSAGE, statement_context
    )

    assert len(turn.hops) == 1  # the gate held
    assert SPENDING_HALF in turn.combined_reply
    assert HANDOFF_PARTIAL_NOTE in turn.combined_reply


async def test_an_ordinary_refused_handoff_is_left_exactly_as_it_was():
    """Regression guard on the branch above. A handoff with no `answer_so_far`
    is the pre-existing Step 15 shape: a source that finished its thought and
    merely also asked for help. Appending a "ask me separately" note to that
    would be noise, so the note is conditional on `answer_so_far`."""
    turn = TurnDispatchResult(
        hops=[
            TurnResult(
                reply="analiza mea completa",
                handoff=HandoffRequest(
                    target_agent="documents", reason="r", context_hint="h"
                ),
            )
        ]
    )

    assert HANDOFF_PARTIAL_NOTE not in turn.combined_reply
    assert turn.combined_reply == "analiza mea completa"


async def test_a_silent_refused_handoff_still_gets_the_refusal_reply(context):
    """The other pre-existing branch, unchanged: nothing said and nowhere to
    go still produces HANDOFF_REFUSED_REPLY rather than an empty bubble."""
    orchestrator = _compound_orchestrator(
        insights_script=[
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="handoff_to_agent",
                        arguments={
                            "target_agent": "documents",
                            "reason": "r",
                            "context_hint": "h",
                        },
                    )
                ]
            )
        ],
        banking_script=[ModelResponse(text="unused")],
    )

    turn = await orchestrator.dispatch(
        [Message(role="user", content="analizeaza")], "analizeaza", context
    )

    assert turn.combined_reply == HANDOFF_REFUSED_REPLY


# ---------------------------------------------------------------------------
# 3. The hop budget still binds
# ---------------------------------------------------------------------------


async def test_a_compound_chain_cannot_exceed_max_hops(context):
    """Three sub-intents, three agents, two handoffs - exactly the budget, not
    a hop more. insights -> planning -> banking is the longest legal chain
    ALLOWED_HANDOFF_TARGETS permits, and banking is terminal."""
    supabase = FakeSupabase()
    orchestrator = Orchestrator()
    orchestrator.register(
        InsightsAgent(
            MockProvider(
                [ModelResponse(tool_calls=[compound_handoff_call("planning", answer="A")])],
                repeat_last=True,
            ),
            _insights_tools(supabase),
        )
    )
    orchestrator.register(
        PlanningAgent(
            MockProvider(
                [
                    ModelResponse(
                        tool_calls=[
                            compound_handoff_call("banking", answer="B", call_id="call-2")
                        ]
                    )
                ],
                repeat_last=True,
            ),
            build_planning_tools(supabase),
        )
    )
    orchestrator.register(
        BankingAgent(MockProvider([ModelResponse(text="C")], repeat_last=True),
                     build_banking_tools(supabase)),
        default=True,
    )

    turn = await orchestrator.dispatch(
        [Message(role="user", content="analiza")], "analiza", context
    )

    assert len(turn.hops) == MAX_HOPS + 1
    assert [d.agent_name for d in turn.routing_chain] == ["insights", "planning", "banking"]
    assert turn.combined_reply == "A\n\nB\n\nC"


# ---------------------------------------------------------------------------
# 4. THE SECURITY BOUNDARY: an auto-handoff can never reach an execution
# ---------------------------------------------------------------------------


class _NamedAccountQuery:
    """Answers any PostgREST builder chain with one canned row that has both a
    `name` (propose_transfer builds its summary from it) and a UUID `id`
    (_insufficient_funds_error parses it). Same technique as
    test_injection_hardening.py's fake."""

    def __init__(self, row: dict) -> None:
        self._row = row

    async def execute(self) -> SimpleNamespace:
        return SimpleNamespace(data=self._row)

    def __getattr__(self, _name: str):
        return lambda *_a, **_kw: self


class _NamedAccountSupabase:
    def __init__(self, row: dict) -> None:
        self._row = row

    def table(self, *_a: object, **_kw: object) -> _NamedAccountQuery:
        return _NamedAccountQuery(self._row)


async def test_a_compound_handoff_carrying_an_action_still_only_proposes(monkeypatch):
    """The boundary the compound path must not become a way around.

    A message mixing a read-only half with an ACTION ("care e soldul meu si
    trimite 50 RON in economii") must never end with money moved. The prompts
    tell the source agent not to auto-hand-off for an action at all - but a
    prompt is not a guarantee, so this proves the STRUCTURAL half: even when
    the handoff happens anyway and BankingAgent acts on the hint, the only
    thing it can reach is propose_transfer, which creates a pending proposal.
    The real transfer-execution service raises if it is touched at all.
    """
    account_row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "Cont Curent",
        "currency": "RON",
    }
    supabase = _NamedAccountSupabase(account_row)
    action_context = Context(
        user_id=TEST_USER_ID, account_ids=OWNED_ACCOUNT_IDS, conversation_id="conv-1"
    )

    async def fake_get_balance(*_a: object, **_kw: object) -> int:
        return 10_000_000

    async def fake_create_proposal(*_a: object, **_kw: object) -> dict:
        return {"id": "prop-1"}

    async def fail_if_called(*_a: object, **_kw: object) -> None:
        raise AssertionError(
            "a compound handoff must never reach the real transfer-execution service"
        )

    monkeypatch.setattr("app.modules.ledger.service.get_balance", fake_get_balance)
    monkeypatch.setattr(
        "app.modules.chat.proposals_service.create_proposal", fake_create_proposal
    )
    monkeypatch.setattr("app.modules.transfers.service.create_transfer", fail_if_called)

    orchestrator = Orchestrator()
    orchestrator.register(
        InsightsAgent(
            MockProvider(
                [
                    ModelResponse(
                        tool_calls=[
                            compound_handoff_call(
                                "banking",
                                answer=SPENDING_HALF,
                                hint="Utilizatorul vrea sa trimita 50 RON in Economii.",
                            )
                        ]
                    )
                ],
                repeat_last=True,
            ),
            _insights_tools(FakeSupabase()),
        )
    )
    orchestrator.register(
        BankingAgent(
            MockProvider(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="t1",
                                name="propose_transfer",
                                arguments={
                                    "from_account_id": OWNED_ACCOUNT_IDS[0],
                                    "to_account_id": OWNED_ACCOUNT_IDS[1],
                                    "amount_minor": 5_000,
                                    "currency": "RON",
                                },
                            )
                        ]
                    ),
                    ModelResponse(
                        text="Am pregătit o propunere de transfer. Confirmă în aplicație."
                    ),
                ],
                repeat_last=True,
            ),
            build_banking_tools(supabase),
        ),
        default=True,
    )

    # Phrased so it routes to Insights (the `cheltui` stem plus the `cat`
    # analytical marker) - a message leading with "sold" would go straight to
    # Banking and never exercise the handoff this test is about.
    message = "cat am cheltuit luna asta si trimite 50 RON in economii"
    turn = await orchestrator.dispatch(
        [Message(role="user", content=message)], message, action_context
    )

    # BankingAgent ran and produced a PROPOSAL, not a movement of money. The
    # execution service raising above is what proves the second half.
    import json

    tool_messages = [m for hop in turn.hops for m in hop.trace if m.role == "tool"]
    proposals = [m for m in tool_messages if m.name == "propose_transfer"]
    assert len(proposals) == 1

    payload = json.loads(proposals[0].content or "{}")
    assert payload["ok"] is True
    assert "proposal_id" in payload["result"]

    # Nothing anywhere in the turn claims the transfer happened.
    whole_turn = json.dumps([m.model_dump() for m in tool_messages]).lower()
    assert "executed" not in whole_turn
    assert "completed" not in whole_turn


# ---------------------------------------------------------------------------
# 5. The tool argument itself
# ---------------------------------------------------------------------------


async def test_answer_so_far_rides_on_the_sentinel(context):
    result = await HandoffToAgentTool().execute(compound_handoff_call("banking"), context)

    assert result.ok
    assert result.data is not None
    assert result.data[HANDOFF_SENTINEL_KEY]["answer_so_far"] == SPENDING_HALF


async def test_answer_so_far_is_optional_and_defaults_to_empty(context):
    """A handoff written before this argument existed - and the ordinary Step
    15 demo path, which still has nothing to say - must keep working."""
    call = ToolCall(
        id="c1",
        name="handoff_to_agent",
        arguments={"target_agent": "banking", "reason": "r", "context_hint": "h"},
    )

    result = await HandoffToAgentTool().execute(call, context)

    assert result.ok
    assert result.data is not None
    assert result.data[HANDOFF_SENTINEL_KEY]["answer_so_far"] == ""


def test_a_sentinel_missing_answer_so_far_entirely_still_parses():
    """`_handoff_from` uses .get for this one field, unlike the three required
    ones - a payload from before the field existed is a valid handoff with
    nothing to say, not a malformed one to discard."""
    from app.ai.agents.tool_loop import _handoff_from
    from app.ai.schemas import ToolResult

    legacy = ToolResult(
        name="handoff_to_agent",
        data={HANDOFF_SENTINEL_KEY: {"target": "banking", "reason": "r", "context_hint": "h"}},
    )

    handoff = _handoff_from(legacy)

    assert handoff is not None
    assert handoff.target_agent == "banking"
    assert handoff.answer_so_far == ""


# ---------------------------------------------------------------------------
# 6. The prompts that trigger it (static, like test_verbosity_regression.py)
# ---------------------------------------------------------------------------


def test_handoff_capable_agents_are_told_how_to_split_a_compound_question():
    """Insights and Planning are the only agents holding `handoff_to_agent`
    (see build_insights_tools / build_planning_tools), so they are the only
    two that can execute the split."""
    for prompt in (INSIGHTS_PROMPT, PLANNING_PROMPT):
        assert "ÎNTREBARE COMPUSĂ" in prompt
        assert "answer_so_far" in prompt or "răspunsul tău" in prompt.lower()
        # The read-only boundary must be stated, not implied.
        assert "CITEȘTE" in prompt
        assert "ACȚIUNE" in prompt


def test_every_agent_is_told_to_acknowledge_what_it_cannot_cover():
    """Including BankingAgent, which holds no handoff tool at all and so can
    only acknowledge - it is terminal by design (see build_banking_tools)."""
    for prompt in (INSIGHTS_PROMPT, PLANNING_PROMPT, BANKING_PROMPT):
        assert "întreabă-mă separat" in prompt


def test_banking_agent_still_has_no_handoff_tool():
    """The compound work must not have made BankingAgent non-terminal to get
    the second half answered."""
    names = set(build_banking_tools(FakeSupabase()).names())

    assert "handoff_to_agent" not in names


def test_document_agent_still_has_no_handoff_tool():
    """Fix 1 narrowed DocumentAgent's ROUTING, and must not have touched its
    isolation. It still holds exactly two tools and no way to reach an agent
    that can act."""
    from app.ai.service import build_document_tools

    names = set(build_document_tools(FakeSupabase()).names())

    assert "handoff_to_agent" not in names
    assert names == {"read_document", "summarize_statement"}


def test_tool_loop_agent_is_still_the_only_run_implementation():
    """A guard on the shape of the fix: the compound split rides on the shared
    loop's handoff branch, so no agent may have grown its own `run`."""
    for agent_type in (InsightsAgent, BankingAgent, PlanningAgent):
        assert agent_type.run is ToolLoopAgent.run
