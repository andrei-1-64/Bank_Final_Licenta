"""The rule-hybrid router: rules, the single-agent shortcut, and LLM fallback.

Offline: every provider here is a scripted `MockProvider`, so the classifier's
behaviour - including what it does with a model that answers nonsense - is
fully deterministic.
"""

from __future__ import annotations

import re

import pytest
from pydantic import BaseModel, ValidationError

from app.ai.agents.banking_agent import BankingAgent
from app.ai.agents.base import Agent
from app.ai.context import Context
from app.ai.orchestrator import LLM_FALLBACK_CONFIDENCE, Orchestrator
from app.ai.providers.base import ProviderError
from app.ai.providers.mock_provider import MockProvider
from app.ai.routing import RoutingDecision, RoutingRule, normalise
from app.ai.schemas import Message, ModelResponse, ToolResult
from app.ai.service import build_banking_tools
from tests.ai.conftest import FakeSupabase


def _banking_agent() -> BankingAgent:
    return BankingAgent(
        MockProvider([ModelResponse(text="ok")], repeat_last=True),
        build_banking_tools(FakeSupabase()),
    )


class _StubAgent(Agent):
    """A second agent, so the multi-agent paths are reachable."""

    name = "insights"
    routing_rules = (
        RoutingRule(name="insights_keywords", keywords=frozenset({"grafic", "raport"})),
    )

    async def run(
        self, messages: list[Message], context: Context
    ) -> tuple[str, list[Message]]:
        return "insights reply", []


class _RulelessAgent(Agent):
    """No rules at all - only reachable via the fallback or as default."""

    name = "ruleless"

    async def run(
        self, messages: list[Message], context: Context
    ) -> tuple[str, list[Message]]:
        return "ruleless reply", []


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def test_router_matches_banking_rule_for_balance_keyword_romanian(context):
    orchestrator = Orchestrator([_banking_agent(), _StubAgent()])

    decision = orchestrator.route("care este soldul meu?", context)

    assert decision.agent_name == "banking"
    assert decision.matched_rule == "banking_keywords"
    assert decision.confidence == 1.0
    assert "sold" in decision.reason


def test_router_matches_banking_rule_for_card_keyword_english(context):
    orchestrator = Orchestrator([_banking_agent(), _StubAgent()])

    decision = orchestrator.route("show me my card", context)

    assert decision.agent_name == "banking"
    assert decision.matched_rule == "banking_keywords"
    assert "'card'" in decision.reason


@pytest.mark.parametrize(
    "message",
    [
        "Care este SOLDUL meu?",
        "care este soldul meu?",
        "ce TRANZACȚII am?",
        "ce tranzactii am?",
        "Ce CARDURI am?",
    ],
)
def test_route_is_case_and_diacritic_insensitive(context, message):
    """Romanian is routinely typed without diacritics; both spellings route the
    same way, and so does any capitalisation."""
    orchestrator = Orchestrator([_banking_agent(), _StubAgent()])

    decision = orchestrator.route(message, context)

    assert decision.agent_name == "banking"
    assert decision.matched_rule == "banking_keywords"


def test_rules_match_inflected_romanian_words():
    """Prefix matching is what makes `card` claim `cardurile`."""
    rule = RoutingRule(name="r", keywords=frozenset({"card", "sold", "tranzac"}))

    assert rule.matched(normalise("Ce carduri am?")) == {"card"}
    assert rule.matched(normalise("soldurile mele")) == {"sold"}
    assert rule.matched(normalise("tranzacțiile mele")) == {"tranzac"}
    assert rule.matched(normalise("vremea de maine")) == frozenset()


# ---------------------------------------------------------------------------
# Agent-count paths
# ---------------------------------------------------------------------------


def test_router_returns_only_registered_agent_when_one_agent(context):
    """Today's common path: nothing to decide, so an unmatched message still
    goes to the single agent - without spending a model call on it."""
    orchestrator = Orchestrator([_banking_agent()])

    decision = orchestrator.route("vremea de maine", context)

    assert decision.agent_name == "banking"
    assert decision.matched_rule is None
    assert decision.confidence == 1.0
    assert decision.reason == "Only one agent registered"


def test_router_llm_fallback_when_multiple_agents_and_no_rule_match(context):
    """Several agents, no rule matched -> the model classifies."""
    classifier = MockProvider([ModelResponse(text="insights")])
    orchestrator = Orchestrator(
        [_banking_agent(), _StubAgent()], provider=classifier
    )

    decision = orchestrator.route("vremea de maine", context)

    assert decision.agent_name == "insights"
    assert decision.matched_rule is None
    assert decision.confidence == LLM_FALLBACK_CONFIDENCE
    assert decision.reason == "Classified by model"

    # The classifier saw a system prompt naming the choices, and the message.
    seen = classifier.calls[0]
    assert seen[0].role == "system"
    assert "banking" in (seen[0].content or "")
    assert "insights" in (seen[0].content or "")
    assert seen[1].content == "vremea de maine"


@pytest.mark.parametrize(
    "reply",
    ["definitely-not-an-agent", "", "   ", "banking, insights", "I think banking!"],
    ids=["unknown", "empty", "whitespace", "list", "prose"],
)
def test_router_llm_fallback_falls_back_to_default_agent_on_invalid_model_response(
    context, reply
):
    """A model answer that does not name a registered agent is discarded."""
    orchestrator = Orchestrator(
        [_banking_agent(), _StubAgent()],
        provider=MockProvider([ModelResponse(text=reply)]),
    )

    decision = orchestrator.route("vremea de maine", context)

    # banking is the default (registered first).
    assert decision.agent_name == "banking"
    assert decision.matched_rule is None
    assert "unknown agent" in decision.reason


def test_router_llm_fallback_survives_a_provider_failure(context):
    """Routing must not be able to fail a request the default agent could have
    answered."""

    class _BrokenProvider(MockProvider):
        def complete(self, messages, tool_specs=None):
            raise ProviderError("upstream is down")

    orchestrator = Orchestrator(
        [_banking_agent(), _StubAgent()],
        provider=_BrokenProvider([ModelResponse(text="unused")]),
    )

    decision = orchestrator.route("vremea de maine", context)

    assert decision.agent_name == "banking"
    assert "unavailable" in decision.reason


def test_router_without_a_provider_degrades_to_the_default_agent(context):
    """Multiple agents but no classifier wired: still answer, still say why."""
    orchestrator = Orchestrator([_banking_agent(), _RulelessAgent()])

    decision = orchestrator.route("vremea de maine", context)

    assert decision.agent_name == "banking"
    assert "No classifier provider" in decision.reason


# ---------------------------------------------------------------------------
# The decision object itself
# ---------------------------------------------------------------------------


def test_routing_decision_is_immutable():
    """A decision records something that already happened; nothing downstream
    may rewrite it."""
    decision = RoutingDecision(agent_name="banking", reason="because", confidence=1.0)

    with pytest.raises(ValidationError):
        decision.agent_name = "insights"
    with pytest.raises(ValidationError):
        decision.confidence = 0.1


def test_routing_decision_rejects_out_of_range_confidence():
    with pytest.raises(ValidationError):
        RoutingDecision(agent_name="a", reason="r", confidence=1.5)
    with pytest.raises(ValidationError):
        RoutingDecision(agent_name="a", reason="r", confidence=-0.1)


@pytest.mark.parametrize(
    ("message", "must_not_appear"),
    [
        (
            "care este soldul meu, ma numesc Ion Popescu",
            ["Ion", "Popescu", "numesc"],
        ),
        (
            "transfer catre IBAN RO49AAAA1B31007593840000",
            ["RO49AAAA1B31007593840000", "catre"],
        ),
        ("cat am in contul meu secret 12345", ["12345", "secret"]),
    ],
)
def test_routing_decision_reason_never_leaks_sensitive_data(
    context, message, must_not_appear
):
    """The reason explains WHY we routed, not WHAT the user said. The
    transcript already holds their message; duplicating it into an audit field
    nobody expects to be sensitive is how leaks happen.

    The precise property is that the reason is assembled ONLY from the fixed
    keyword vocabulary. A vocabulary word like `transfer` may therefore appear
    even though the user also typed it - what must never appear is anything
    user-authored: names, IBANs, account numbers, free text.
    """
    orchestrator = Orchestrator([_banking_agent(), _StubAgent()])

    decision = orchestrator.route(message, context)

    assert message not in decision.reason
    for fragment in must_not_appear:
        assert fragment not in decision.reason

    # Structural version of the same claim: every quoted token in the reason is
    # a keyword some registered agent declared, not something the user supplied.
    vocabulary = {
        keyword
        for agent_name in orchestrator.names()
        for rule in orchestrator.get(agent_name).routing_rules
        for keyword in rule.keywords
    }
    quoted = re.findall(r"'([^']*)'", decision.reason)
    assert quoted, "a rule-matched reason should name the keywords that fired"
    assert set(quoted) <= vocabulary


# ---------------------------------------------------------------------------
# Registration stays the extension point
# ---------------------------------------------------------------------------


def test_registering_a_second_agent_makes_its_rules_routable(context):
    """The whole point of the mechanism: adding an agent is register() plus
    rules on the agent class. The orchestrator is not edited."""
    orchestrator = Orchestrator([_banking_agent()])
    assert orchestrator.route("arata-mi un grafic", context).agent_name == "banking"

    orchestrator.register(_StubAgent())

    decision = orchestrator.route("arata-mi un grafic", context)
    assert decision.agent_name == "insights"
    assert decision.matched_rule == "insights_keywords"


def test_agents_without_rules_default_to_an_empty_tuple():
    assert _RulelessAgent.routing_rules == ()
    assert isinstance(BankingAgent.routing_rules, tuple)
    assert BankingAgent.routing_rules


def test_route_raises_when_no_agents_are_registered(context):
    with pytest.raises(RuntimeError):
        Orchestrator().route("anything", context)


# ---------------------------------------------------------------------------
# The prompt change that ships with this step
# ---------------------------------------------------------------------------


def test_system_prompt_directs_generic_balance_to_list_accounts():
    """A scripted MockProvider cannot prove this - the test would be asserting
    its own script. What is checkable offline is that the instruction reaches
    the model at all; that it is OBEYED is verified against the real model in
    the manual smoke test."""
    from app.ai.agents.banking_agent import SYSTEM_PROMPT

    generic_rule = SYSTEM_PROMPT.split("Întrebare despre UN ANUME cont")[0]
    assert "GENERALĂ" in generic_rule
    assert "list_accounts" in generic_rule
    assert "get_balance" not in generic_rule

    specific_rule = SYSTEM_PROMPT.split("Întrebare despre UN ANUME cont")[1]
    assert "get_balance" in specific_rule.split("\n")[0] + specific_rule.split("\n")[1]


def test_tool_result_and_message_types_are_untouched_by_routing():
    """Regression guard: routing rides alongside the transcript, it does not
    change what a turn is."""
    assert issubclass(ToolResult, BaseModel)
    assert set(Message.model_fields) == {
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "name",
    }
