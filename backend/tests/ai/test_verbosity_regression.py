"""Regression guard for the "too verbose, too many questions" hardening pass.

There is no real model in this offline suite (see conftest.py), so this file
cannot observe an actual reply being terse - what it CAN do, and what it
guards, is that the POLICY TEXT each agent is instructed with still says
"at most one clarifying question" and still asks for brevity. If a future
prompt edit silently drops one of those lines, one of these tests fails
instead of the regression only surfacing as a vague "the bot got chatty"
complaint later.

The MockProvider-driven tests below add the other half: given a scripted
reply, the agent returns it completely unchanged (no verbosity of its own is
injected by the loop) - so any wordiness a real model produces is entirely a
function of its prompt, which is exactly what the static checks above cover.
"""

from __future__ import annotations

import pytest

from app.ai.agents.banking_agent import SYSTEM_PROMPT as BANKING_PROMPT
from app.ai.agents.docs_agent import SYSTEM_PROMPT as DOCS_PROMPT
from app.ai.agents.document_agent import SYSTEM_PROMPT as DOCUMENT_PROMPT
from app.ai.agents.insights_agent import SYSTEM_PROMPT as INSIGHTS_PROMPT
from app.ai.agents.planning_agent import SYSTEM_PROMPT as PLANNING_PROMPT
from app.ai.schemas import Message, ModelResponse

#: The exact cap phrasing each of the 4 general-purpose agents' prompts
#: carries (word order/casing may differ slightly per agent's own voice, so
#: this checks for the load-bearing substring "CEL MULT" next to a
#: clarifying-question mention, not a byte-identical sentence).
_ONE_QUESTION_CAP_AGENTS = {
    "banking": BANKING_PROMPT,
    "insights": INSIGHTS_PROMPT,
    "planning": PLANNING_PROMPT,
    "docs": DOCS_PROMPT,
    "documents": DOCUMENT_PROMPT,
}


@pytest.mark.parametrize("agent_name", sorted(_ONE_QUESTION_CAP_AGENTS))
def test_every_agent_prompt_caps_clarifying_questions_at_one(agent_name: str) -> None:
    prompt = _ONE_QUESTION_CAP_AGENTS[agent_name]
    assert "CEL MULT" in prompt
    assert "ntrebare" in prompt  # "întrebare"/"întrebări" - diacritics vary by context


@pytest.mark.parametrize("agent_name", sorted(_ONE_QUESTION_CAP_AGENTS))
def test_every_agent_prompt_forbids_stacking_multiple_questions(agent_name: str) -> None:
    """The other half of the cap: not just "at most one", but an explicit
    "don't chain them" statement, so a future edit that keeps the cap but
    drops the anti-stacking clause still fails a test."""
    prompt = _ONE_QUESTION_CAP_AGENTS[agent_name]
    assert "mai multe întrebări" in prompt or "mai multor întrebări" in prompt


@pytest.mark.parametrize(
    ("agent_name", "prompt"),
    [
        ("banking", BANKING_PROMPT),
        ("insights", INSIGHTS_PROMPT),
        ("planning", PLANNING_PROMPT),
        ("docs", DOCS_PROMPT),
    ],
)
def test_every_general_purpose_agent_still_asks_for_brevity(
    agent_name: str, prompt: str
) -> None:
    """Regression guard on the pre-existing "Fii scurt" instructions - the
    verbosity pass tightens these, it must never remove them."""
    assert "scurt" in prompt.lower()


def test_insights_agent_defaults_an_unspecified_date_range_instead_of_asking() -> None:
    """Task requirement: a missing date range is NOT, by itself, blocking -
    InsightsAgent must default to the last 30 days rather than asking."""
    assert "ultimele 30 de zile" in INSIGHTS_PROMPT
    assert "fara sa intrebi" in INSIGHTS_PROMPT.replace("ă", "a").replace("î", "i")


# ---------------------------------------------------------------------------
# MockProvider-driven: the loop itself adds no verbosity of its own
# ---------------------------------------------------------------------------


async def test_agent_reply_is_exactly_what_the_model_produced_no_padding(
    make_agent, context
):
    """The loop must not append boilerplate, disclaimers, or repeated
    questions around a model's answer - whatever verbosity exists is 100%
    a function of the prompt (covered by the static tests above), not of
    ToolLoopAgent.run itself."""
    concise_reply = "Soldul contului curent este 500,00 RON."
    agent, _ = make_agent([ModelResponse(text=concise_reply)])

    result = await agent.run([Message(role="user", content="care e soldul meu?")], context)

    assert result.reply == concise_reply


async def test_a_single_scripted_question_passes_through_unchanged(make_agent, context):
    """A model asking exactly one clarifying question is relayed as-is - the
    loop does not need to (and must not) add a second one of its own."""
    one_question = "Pentru ce card vrei să schimb limita de cheltuieli?"
    agent, _ = make_agent([ModelResponse(text=one_question)])

    result = await agent.run(
        [Message(role="user", content="schimba-mi limita cardului")], context
    )

    assert result.reply == one_question
    assert result.reply.count("?") == 1
