"""The centralized scope guardrail (orchestration hardening pass): a message
OBVIOUSLY outside the banking/finance domain is declined before `route()`
ever runs and before any agent is selected - see
app/ai/agents/scope_guardrail.py's module docstring for the two-layer design
(this centralized check, backstopped by each agent's own OFF_TOPIC_GUARDRAIL
prompt text).

Offline, same as the rest of tests/ai: no real provider, no database.
"""

from __future__ import annotations

import logging

import pytest

from app.ai.agents.banking_agent import BankingAgent
from app.ai.agents.document_agent import DocumentAgent
from app.ai.agents.scope_guardrail import OFF_TOPIC_DECLINE_MESSAGE, is_out_of_scope
from app.ai.agents.tool_loop import ToolLoopAgent
from app.ai.context import Context
from app.ai.orchestrator import SCOPE_GUARDRAIL_AGENT_NAME, Orchestrator
from app.ai.providers.mock_provider import MockProvider
from app.ai.schemas import Message, ModelResponse
from app.ai.service import build_banking_tools, build_document_tools
from app.ai.tools.registry import ToolRegistry
from tests.ai.conftest import OWNED_ACCOUNT_IDS, TEST_USER_ID, FakeSupabase

# ---------------------------------------------------------------------------
# 1. `is_out_of_scope` - the classifier itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "scrie-mi o poezie despre toamna",
        "spune-mi o gluma",
        "ce vreme e afara, e meteo bun maine?",
        "care e capitala Frantei",
        "cine a scris Romeo si Julieta",
        "scrie-mi cod python pentru un algoritm de sortare",
        "cum gatesc o prajitura cu ciocolata",
        "da-mi o reteta de ciorba",
        "rezolva ecuatia x + 2 = 5",
        "ajuta-ma la tema la matematica",
        "tradu in engleza propozitia asta",
        "recomanda-mi un film bun de weekend",
    ],
    ids=[
        "poem",
        "joke",
        "weather",
        "trivia-capital",
        "trivia-author",
        "coding-help",
        "recipe-cooking",
        "recipe-noun",
        "homework-equation",
        "homework-phrase",
        "translation",
        "movie-recommendation",
    ],
)
def test_obviously_off_topic_messages_are_flagged(message: str) -> None:
    assert is_out_of_scope(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "care este soldul meu",
        "ce se intampla cu banii mei",
        "vreau sa fac un transfer de 100 RON",
        "cat costa un card nou",
        "poti sa ma ajuti cu ceva?",
        "as vrea sa deschid un cont la banca",
        "unde s-au dus banii mei luna asta",
        "vreau sa economisesc pentru o vacanta",
        "ce comision am pentru contul curent",
    ],
    ids=[
        "balance",
        "vague-money",
        "transfer",
        "card-cost",
        "vague-help",
        "open-account",
        "spending-analysis",
        "savings-goal",
        "fee-question",
    ],
)
def test_in_domain_and_ambiguous_banking_messages_are_never_flagged(message: str) -> None:
    assert is_out_of_scope(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "care e piata de capital acum",  # "capital" the financial term, not "capitala "
        "am nevoie de cod postal pentru livrare",  # "cod" overlaps card-order flow
        "de vreme ce am facut transferul, mai am ceva de facut?",  # "vreme" substring
        "am o tema de discutat cu banca",  # bare "tema", not the homework phrase
        "ce cont recomandati pentru economii",  # bare "recomanda", not a film/book ask
    ],
    ids=[
        "capital-markets-not-capital-city",
        "postal-code-not-coding-help",
        "de-vreme-ce-not-weather",
        "tema-generic-not-homework",
        "recomanda-generic-not-entertainment",
    ],
)
def test_known_near_collisions_are_not_flagged(message: str) -> None:
    """Regression guard for the specific collisions considered and rejected
    when the phrase list was designed (see scope_guardrail.py's comment on
    `_OFF_TOPIC_PHRASES`) - each of these shares a substring with an
    off-topic phrase but is a legitimate banking message."""
    assert is_out_of_scope(message) is False


# ---------------------------------------------------------------------------
# 2. Orchestrator.dispatch: the guardrail runs before route(), before any agent
# ---------------------------------------------------------------------------


class _ExplodingAgent(ToolLoopAgent):
    """Fails the test if it is ever run - used to prove the guardrail really
    never invokes an agent, rather than just asserting on the final reply."""

    fallback_reply = "should never be used"

    def __init__(self, name: str) -> None:
        self.name = name  # type: ignore[misc]
        super().__init__(MockProvider([ModelResponse(text="x")]), ToolRegistry())

    async def run(self, messages, context):  # type: ignore[no-untyped-def]
        raise AssertionError(f"agent {self.name!r} must not run for an off-topic message")


def _banking_only_orchestrator() -> Orchestrator:
    return Orchestrator([_ExplodingAgent("banking")], default="banking")


async def test_off_topic_message_is_declined_with_no_agent_and_no_route_call(
    context, monkeypatch
):
    orchestrator = _banking_only_orchestrator()
    route_calls: list[str] = []
    original_route = orchestrator.route
    monkeypatch.setattr(
        orchestrator,
        "route",
        lambda message, ctx: (route_calls.append(message), original_route(message, ctx))[1],
    )

    turn = await orchestrator.dispatch(
        [Message(role="user", content="spune-mi o gluma")], "spune-mi o gluma", context
    )

    assert route_calls == []  # route() was never even called
    assert len(turn.hops) == 1
    assert turn.final_reply == OFF_TOPIC_DECLINE_MESSAGE
    assert turn.routing_chain[0].agent_name == SCOPE_GUARDRAIL_AGENT_NAME
    assert turn.routing_chain[0].confidence == 1.0
    assert turn.routing_chain[0].handoff_from is None


async def test_off_topic_decline_is_logged_for_observability(context, caplog):
    orchestrator = _banking_only_orchestrator()

    with caplog.at_level(logging.INFO, logger="app.ai.orchestrator"):
        await orchestrator.dispatch(
            [Message(role="user", content="spune-mi o gluma")], "spune-mi o gluma", context
        )

    assert "scope guardrail declined" in caplog.text
    # Never log the user's message text itself.
    assert "gluma" not in caplog.text


async def test_off_topic_turn_round_trips_like_any_other_turn(context):
    """The decline is a normal one-hop TurnDispatchResult - it persists and
    replays exactly like a real agent's turn (see turn.py's new_messages)."""
    orchestrator = _banking_only_orchestrator()

    turn = await orchestrator.dispatch(
        [Message(role="user", content="scrie-mi o poezie")], "scrie-mi o poezie", context
    )

    assert [m.role for m in turn.new_messages] == ["assistant"]
    assert turn.new_messages[0].content == OFF_TOPIC_DECLINE_MESSAGE


async def test_in_domain_message_still_reaches_the_real_agent(context):
    """The guardrail must not swallow legitimate banking traffic."""
    banking = BankingAgent(
        MockProvider([ModelResponse(text="Soldul tau este 500 RON.")]),
        build_banking_tools(FakeSupabase()),
    )
    orchestrator = Orchestrator([banking], default="banking")

    turn = await orchestrator.dispatch(
        [Message(role="user", content="care este soldul meu")], "care este soldul meu", context
    )

    assert turn.routing_chain[0].agent_name == "banking"
    assert turn.final_reply == "Soldul tau este 500 RON."


async def test_ambiguous_in_domain_message_is_not_blocked(context):
    """Task example: 'what's going on with my money' must still reach an
    agent, not get bounced as off-topic."""
    banking = BankingAgent(
        MockProvider([ModelResponse(text="Iti arat imediat conturile.")]),
        build_banking_tools(FakeSupabase()),
    )
    orchestrator = Orchestrator([banking], default="banking")

    turn = await orchestrator.dispatch(
        [Message(role="user", content="ce se intampla cu banii mei")],
        "ce se intampla cu banii mei",
        context,
    )

    assert turn.routing_chain[0].agent_name == "banking"
    assert turn.routing_chain[0].agent_name != SCOPE_GUARDRAIL_AGENT_NAME


# ---------------------------------------------------------------------------
# 3. The guardrail steps aside for an active document/statement conversation
# ---------------------------------------------------------------------------


async def test_active_document_bypasses_the_guardrail_even_for_off_topic_phrasing():
    """DocumentAgent already enforces its own tighter, document-scoped
    refusal (see document_agent.py's SYSTEM_PROMPT) - the centralized check
    must not preempt it while a document is active."""
    documents = DocumentAgent(
        MockProvider(
            [ModelResponse(text="Nu te pot ajuta cu asta - doar cu documentul atasat.")]
        ),
        build_document_tools(FakeSupabase()),
    )
    orchestrator = Orchestrator([documents])
    doc_context = Context(
        user_id=TEST_USER_ID, account_ids=OWNED_ACCOUNT_IDS, active_document_id="doc-1111"
    )

    turn = await orchestrator.dispatch(
        [Message(role="user", content="spune-mi o gluma")], "spune-mi o gluma", doc_context
    )

    assert turn.routing_chain[0].agent_name == "documents"
    assert turn.routing_chain[0].agent_name != SCOPE_GUARDRAIL_AGENT_NAME


async def test_active_statement_also_bypasses_the_guardrail():
    documents = DocumentAgent(
        MockProvider([ModelResponse(text="Doar despre extrasul atasat pot discuta.")]),
        build_document_tools(FakeSupabase()),
    )
    orchestrator = Orchestrator([documents])
    statement_context = Context(
        user_id=TEST_USER_ID, account_ids=OWNED_ACCOUNT_IDS, statement_id="stmt-1111"
    )

    turn = await orchestrator.dispatch(
        [Message(role="user", content="ce vreme e afara")], "ce vreme e afara", statement_context
    )

    assert turn.routing_chain[0].agent_name == "documents"


# ---------------------------------------------------------------------------
# 4. Every general-purpose agent still carries the backup prompt layer
# ---------------------------------------------------------------------------


def test_every_general_purpose_agent_still_carries_the_backup_guardrail_text():
    """Defense in depth (requirement 2): even though the centralized check is
    now primary, each of Banking/Insights/Planning/Docs keeps its own
    OFF_TOPIC_GUARDRAIL restatement, verbatim, in its system prompt."""
    from app.ai.agents.banking_agent import SYSTEM_PROMPT as banking_prompt
    from app.ai.agents.docs_agent import SYSTEM_PROMPT as docs_prompt
    from app.ai.agents.insights_agent import SYSTEM_PROMPT as insights_prompt
    from app.ai.agents.planning_agent import SYSTEM_PROMPT as planning_prompt
    from app.ai.agents.scope_guardrail import OFF_TOPIC_GUARDRAIL

    for prompt in (banking_prompt, insights_prompt, planning_prompt, docs_prompt):
        assert OFF_TOPIC_GUARDRAIL in prompt


def test_document_agent_keeps_its_own_tighter_refusal_wording():
    """DocumentAgent deliberately does NOT share OFF_TOPIC_GUARDRAIL (see
    scope_guardrail.py's docstring) - it has its own document-scoped one."""
    from app.ai.agents.document_agent import SYSTEM_PROMPT as document_prompt

    assert "nu ține de documentul" in document_prompt or "documentul sau extrasul atașat" in (
        document_prompt
    )
    assert "DOUĂ părți" in document_prompt
