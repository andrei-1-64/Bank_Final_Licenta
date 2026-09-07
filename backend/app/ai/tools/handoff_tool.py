"""The one tool that can move a turn between agents.

Unlike every other tool, this one does no work: it returns a SENTINEL that
`ToolLoopAgent.run` recognises and turns into a `HandoffRequest`. The agent
loop stops there — the handoff itself is executed by `Orchestrator.dispatch`,
which is the only place that knows what other agents exist.

That split is the security design, not an implementation detail. The tool's
arguments are model-authored: `target_agent` is a name the model made up, and
if this tool could route on its own, a well-phrased document could talk any
agent holding it into reaching any other agent. It cannot. All it can do is
*ask*. `dispatch` then checks the hop cap, the cycle set, the per-source
allow-list, the DocumentAgent quarantine and the statement-mode gate before a
single one of those requests is honoured (see orchestrator.py).

Which registries this tool belongs in is likewise not the tool's business —
see `app/ai/service.py`. It is on InsightsAgent and PlanningAgent, and
deliberately absent from BankingAgent (terminal: it produces the proposal that
ends a chain) and from DocumentAgent (isolated: see build_document_tools).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool

logger = logging.getLogger(__name__)

#: The key `ToolLoopAgent.run` looks for in a ToolResult's `data` to recognise
#: a handoff. Dunder-ish on purpose: it must not collide with any real tool
#: payload key, and it must be obvious in a diff that it is a protocol marker
#: rather than data anyone should read.
HANDOFF_SENTINEL_KEY = "__handoff__"

#: What replaces the sentinel result in the trace. The raw sentinel never
#: reaches a prompt (see ToolLoopAgent.run): it is protocol plumbing, and
#: replaying it to a model in a later turn would invite the model to imitate
#: the shape rather than call the tool.
HANDOFF_TRACE_MARKER = "handed off to another agent"


class HandoffToAgentInput(BaseModel):
    target_agent: str = Field(
        max_length=64,
        description=(
            "Numele agentului care trebuie să continue: 'banking' pentru "
            "acțiuni bancare (propuneri de transfer, plată, anulare card), "
            "'planning' pentru planificare financiară."
        ),
    )
    reason: str = Field(
        max_length=500,
        description="De ce este nevoie de celălalt agent. Apare în jurnalul de audit.",
    )
    context_hint: str = Field(
        max_length=1000,
        description=(
            "Instrucțiunea scurtă pe care o primește celălalt agent, ca și cum "
            "ar fi mesajul utilizatorului. Numește concret despre ce este vorba "
            "(ex. cardul și plata recurentă în cauză)."
        ),
    )
    answer_so_far: str = Field(
        default="",
        max_length=2000,
        description=(
            "Răspunsul TĂU pentru partea din întrebare pe care ai acoperit-o "
            "deja, exact așa cum vrei să îl citească utilizatorul. Lasă-l gol "
            "dacă nu ai răspuns la nimic. Folosește-l pentru întrebări "
            "compuse: aici pui partea ta, iar în context_hint pui partea "
            "rămasă, pentru celălalt agent."
        ),
    )


class HandoffToAgentTool(Tool):
    """Ask for the rest of this turn to continue on another agent."""

    name = "handoff_to_agent"
    description = (
        "Predă restul acestei conversații unui alt agent, care continuă în "
        "ACEEAȘI tură și îi răspunde utilizatorului. Folosește-l când ai "
        "identificat ceva ce tu nu poți duce la capăt (de ex. o acțiune "
        "bancară). Nu garantează nimic: cererea poate fi refuzată, caz în "
        "care răspunsul tău rămâne singurul."
    )
    input_schema = HandoffToAgentInput
    #: No side effect: it writes nothing and reads nothing. The turn changing
    #: agent is `dispatch`'s doing, not this call's.
    read_only = True

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, HandoffToAgentInput)

        # UNTRUSTED CONTENT BOUNDARY, for whoever extends this next:
        #
        # `context_hint` is model-authored, and `dispatch` hands it to the
        # target agent as a synthetic USER-role message. That is safe as it
        # stands, because the target already treats every user message as
        # untrusted input - no extra wrapping buys anything at this boundary.
        #
        # It stops being safe the moment a handoff carries text lifted from a
        # DOCUMENT or a STATEMENT. Those are attacker-controlled in a way a
        # user's own typing is not, and a target agent reading them as a
        # plain user instruction is precisely the prompt-injection path Step
        # 12 closed. If you ever pass such content through here, wrap it in
        # <untrusted_document> / <untrusted_statement> first, the same way
        # app/ai/tools/document_tools.py and statement_tools.py already do.
        # Step 15 does not: the only agents holding this tool (Insights,
        # Planning) are also the ones dispatch refuses to let hand off while
        # a statement is active.
        #
        # `context` is not read here at all, deliberately: a handoff must not
        # be able to influence identity, account access or authorisation. The
        # target runs on the SAME frozen Context instance the source did.
        # The requested target is NOT logged here: it is a model-authored
        # string that could contain anything, same reasoning as
        # Orchestrator._classify_with_model's unregistered-name branch.
        # dispatch logs it once it has been matched against a real agent.
        logger.info("handoff requested by an agent")

        # `answer_so_far` exists because of a hard constraint one layer down:
        # `ModelResponse` carries EITHER text OR tool_calls, never both (see
        # app/ai/schemas.py), and `ToolLoopAgent.run` ends the turn the moment
        # a model emits text. A source agent therefore cannot say something
        # AND hand off in the same turn - the natural way to answer half a
        # compound question and pass the other half on. Carrying that half
        # here, as an argument, is the way to do it without relaxing an
        # invariant every provider adapter and every agent depends on.
        #
        # It is model-authored text shown to the user, which is exactly what
        # an agent's ordinary `reply` already is - the same trust level, not a
        # new one. It contributes nothing to identity, routing or
        # authorisation; `dispatch`'s gates do not read it.
        payload: dict[str, Any] = {
            HANDOFF_SENTINEL_KEY: {
                "target": validated_input.target_agent,
                "reason": validated_input.reason,
                "context_hint": validated_input.context_hint,
                "answer_so_far": validated_input.answer_so_far,
            }
        }
        return ToolResult(name=self.name, data=payload)
