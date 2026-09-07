"""Context-first document routing (Step 12): an active document forces
DocumentAgent regardless of what the message says - see
Orchestrator.route()'s docstring for why keyword rules must not run first
while a document is attached.

Offline, same as tests/ai/test_routing.py.
"""

from __future__ import annotations

from app.ai.agents.banking_agent import BankingAgent
from app.ai.agents.document_agent import DocumentAgent
from app.ai.agents.insights_agent import InsightsAgent
from app.ai.agents.planning_agent import PlanningAgent
from app.ai.context import Context
from app.ai.orchestrator import Orchestrator
from app.ai.providers.mock_provider import MockProvider
from app.ai.schemas import ModelResponse
from app.ai.service import (
    build_banking_tools,
    build_document_tools,
    build_insights_tools,
    build_planning_tools,
)
from tests.ai.conftest import OWNED_ACCOUNT_IDS, TEST_USER_ID, FakeSupabase


def _orchestrator() -> Orchestrator:
    supabase = FakeSupabase()
    banking = BankingAgent(
        MockProvider([ModelResponse(text="ok")], repeat_last=True),
        build_banking_tools(supabase),
    )
    documents = DocumentAgent(
        MockProvider([ModelResponse(text="ok")], repeat_last=True),
        build_document_tools(supabase),
    )
    return Orchestrator([banking, documents])


def test_active_document_claims_a_message_about_the_document():
    """The attachment still wins when the message NAMES it - even against
    another agent's keywords in the same sentence ("transferuri" is
    BankingAgent's stem; "document" keeps this with DocumentAgent)."""
    orchestrator = _orchestrator()
    context = Context(
        user_id=TEST_USER_ID,
        account_ids=OWNED_ACCOUNT_IDS,
        active_document_id="doc-1111",
    )

    decision = orchestrator.route("ce scrie in document despre transferuri?", context)

    assert decision.agent_name == "documents"
    assert decision.matched_rule == "context_override"
    assert decision.confidence == 1.0


def test_active_document_does_not_capture_a_live_account_question():
    """THE ROUTING FIX. `document_id` is resent with every message until the
    user detaches (see wireDocumentAttach in frontend/app.js), so before this
    an attached PDF meant no banking question worked for the rest of the
    conversation - "cât am acum în cont?" reached DocumentAgent, which cannot
    see accounts, and said so."""
    orchestrator = _orchestrator()
    context = Context(
        user_id=TEST_USER_ID,
        account_ids=OWNED_ACCOUNT_IDS,
        active_document_id="doc-1111",
    )

    decision = orchestrator.route("cat am acum in cont?", context)

    assert decision.agent_name == "banking"
    assert decision.matched_rule == "banking_keywords"


def test_a_vague_followup_still_belongs_to_the_attached_document():
    """No agent's rules claim it, so with something attached it means the
    attachment - the third branch of route()'s attachment order."""
    orchestrator = _orchestrator()
    context = Context(
        user_id=TEST_USER_ID,
        account_ids=OWNED_ACCOUNT_IDS,
        active_document_id="doc-1111",
    )

    decision = orchestrator.route("si mai departe?", context)

    assert decision.agent_name == "documents"
    assert decision.matched_rule == "context_override"


def test_no_active_document_routes_normally():
    """Regression check: without an active document, banking keywords still
    win exactly as they did before Step 12 introduced the context check."""
    orchestrator = _orchestrator()
    context = Context(user_id=TEST_USER_ID, account_ids=OWNED_ACCOUNT_IDS)

    decision = orchestrator.route("care este soldul meu?", context)

    assert decision.agent_name == "banking"
    assert decision.matched_rule == "banking_keywords"


def test_active_statement_claims_a_message_about_the_statement():
    """Step 13's equivalent of the document case above. `extras` reaches
    DocumentAgent only because a statement is attached - see
    DOCUMENT_FOLLOWUP_RULE, which is deliberately not a general routing rule
    (BankingAgent owns `extras` for "generate me a statement")."""
    orchestrator = _orchestrator()
    context = Context(
        user_id=TEST_USER_ID,
        account_ids=OWNED_ACCOUNT_IDS,
        statement_id="stmt-1111",
    )

    decision = orchestrator.route("ce contine extrasul?", context)

    assert decision.agent_name == "documents"
    assert decision.matched_rule == "context_override"
    assert decision.reason == "active_statement_in_context"


def test_extras_still_belongs_to_banking_when_nothing_is_attached():
    """The other half of the rule above: DOCUMENT_FOLLOWUP_RULE must not leak
    into ordinary routing, or asking the bank to produce a statement would
    reach the agent that only reads attached ones."""
    orchestrator = _orchestrator()
    context = Context(user_id=TEST_USER_ID, account_ids=OWNED_ACCOUNT_IDS)

    decision = orchestrator.route("vreau extrasul de cont pe luna trecuta", context)

    assert decision.agent_name == "banking"


def test_active_statement_does_not_capture_a_live_account_question():
    """The statement half of the routing fix. This one bit harder than the
    document half: `statement_id` re-resolves to the conversation's latest
    upload with nothing sent by the client at all (see Context.statement_id),
    so it went sticky on its own."""
    orchestrator = _orchestrator()
    context = Context(
        user_id=TEST_USER_ID,
        account_ids=OWNED_ACCOUNT_IDS,
        statement_id="stmt-1111",
    )

    decision = orchestrator.route("cat am acum in cont?", context)

    assert decision.agent_name == "banking"


# ---------------------------------------------------------------------------
# `econom` / `cheltui` two-token collision rules (Step 16 Priority 2, item 7)
#
# Registered in the same relative order as AIService.__init__ - insights,
# then banking, then planning - since that order is what decides which of
# two agents claiming the same stem wins.
# ---------------------------------------------------------------------------


def _collision_orchestrator() -> Orchestrator:
    supabase = FakeSupabase()
    provider = MockProvider([ModelResponse(text="ok")], repeat_last=True)
    insights = InsightsAgent(provider, build_insights_tools(supabase, provider))
    banking = BankingAgent(provider, build_banking_tools(supabase))
    planning = PlanningAgent(provider, build_planning_tools(supabase))
    return Orchestrator([insights, banking, planning])


def _collision_context() -> Context:
    return Context(user_id=TEST_USER_ID, account_ids=OWNED_ACCOUNT_IDS)


def test_econom_with_a_forward_marker_routes_to_planning():
    orchestrator = _collision_orchestrator()

    decision = orchestrator.route(
        "cat ar trebui sa economisesc pentru vacanta", _collision_context()
    )

    assert decision.agent_name == "planning"
    assert decision.matched_rule == "planning_savings_goal"


def test_econom_with_no_forward_marker_still_falls_through_to_banking():
    """Past tense, no forward-looking marker - not a goal, so this must keep
    landing on Banking exactly as it did before the collision rules split."""
    orchestrator = _collision_orchestrator()

    decision = orchestrator.route("am economisit 500 lei", _collision_context())

    assert decision.agent_name == "banking"
    assert decision.matched_rule == "banking_savings_default"


def test_cheltui_with_an_analytical_marker_routes_to_insights():
    orchestrator = _collision_orchestrator()

    decision = orchestrator.route(
        "unde am cheltuit cei mai multi bani", _collision_context()
    )

    assert decision.agent_name == "insights"
    assert decision.matched_rule == "insights_spending_analysis"


def test_cheltui_with_no_analytical_marker_falls_through_to_banking():
    """A plain statement of fact, not an analytical question - falls through
    to Banking's unconditional `cheltui`, unchanged from before this rule."""
    orchestrator = _collision_orchestrator()

    decision = orchestrator.route("am cheltuit 50 lei pe cafea", _collision_context())

    assert decision.agent_name == "banking"
    assert decision.matched_rule == "banking_keywords"


def test_sold_routes_to_banking_unchanged():
    orchestrator = _collision_orchestrator()

    decision = orchestrator.route("care e soldul meu", _collision_context())

    assert decision.agent_name == "banking"
    assert decision.matched_rule == "banking_keywords"


def test_bani_routes_to_banking_unchanged():
    orchestrator = _collision_orchestrator()

    decision = orchestrator.route("cati bani am", _collision_context())

    assert decision.agent_name == "banking"
    assert decision.matched_rule == "banking_keywords"
