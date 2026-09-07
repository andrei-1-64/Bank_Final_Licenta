"""The provider/tool loop every agent runs.

Extracted when the second agent arrived. The loop is where two security
properties live — identity is threaded to tools and never rendered into the
prompt, and a failing tool degrades to a reported error instead of killing the
request — and a copy-pasted second version of that is a copy that eventually
drifts. Agents differ in their prompt, their tools and their voice; none of
them differ in how the loop works.

Subclasses supply `name`, `system_prompt`, `fallback_reply`, and (optionally)
`routing_rules`. They do not override `run`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import ClassVar

from app.ai.agents.base import Agent
from app.ai.context import Context
from app.ai.language_directive import language_directive
from app.ai.providers.base import ModelProvider
from app.ai.schemas import Message, ToolCall, ToolResult
from app.ai.tools.handoff_tool import HANDOFF_SENTINEL_KEY, HANDOFF_TRACE_MARKER
from app.ai.tools.registry import ToolRegistry
from app.ai.turn import HandoffRequest, TurnResult

logger = logging.getLogger(__name__)

#: Cap on provider round-trips per user message. Prevents an infinite tool loop.
MAX_ITERATIONS = 5


class ToolLoopAgent(Agent):
    """Runs a read-only tool loop against a provider."""

    #: What the model is told it is. Every subclass overrides this.
    system_prompt: ClassVar[str] = ""
    #: Returned when the loop hits `max_iterations` without a final answer.
    fallback_reply: ClassVar[str] = (
        "I wasn't able to finish that request — I kept needing more information "
        "and stopped to avoid looping. Please try rephrasing it."
    )

    def __init__(
        self,
        provider: ModelProvider,
        tools: ToolRegistry,
        *,
        system_prompt: str | None = None,
        max_iterations: int = MAX_ITERATIONS,
    ) -> None:
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        self._provider = provider
        self._tools = tools
        self._system_prompt = system_prompt if system_prompt is not None else self.system_prompt
        self._max_iterations = max_iterations

    async def run(self, messages: Sequence[Message], context: Context) -> TurnResult:
        # `context` is threaded straight to the tools and is never rendered into
        # the prompt — the model must not be able to read or restate identity.
        #
        # Local working copy: the caller's history is never mutated. Everything
        # appended after the system prompt + caller's messages is the trace this
        # call hands back, so the caller can persist it.
        working: list[Message] = [
            Message(
                role="system",
                content=self._system_prompt + language_directive(context.language),
            ),
            *messages,
        ]
        trace_start = len(working)
        specs = self._tools.list_specs()

        for iteration in range(1, self._max_iterations + 1):
            response = self._provider.complete(working, specs)

            if not response.wants_tools:
                return TurnResult(reply=response.text or "", trace=working[trace_start:])

            assistant_turn = response.to_assistant_message()
            working.append(assistant_turn)
            for call in response.tool_calls:
                # Log the tool name only — arguments and results may carry PII.
                logger.info(
                    "agent=%s iteration=%d executing tool=%s",
                    self.name,
                    iteration,
                    call.name,
                )
                result = await self._execute(call, context)

                handoff = _handoff_from(result)
                if handoff is not None:
                    # STOP HERE. The rest of this turn belongs to another agent
                    # (if dispatch permits it), so there is nothing left for
                    # this model to say - anything it added now would be
                    # written before an answer it cannot see.
                    #
                    # The sentinel result is replaced rather than appended
                    # as-is: it is protocol plumbing, not data, and replaying
                    # it into a later prompt would teach the model to imitate
                    # the shape instead of calling the tool.
                    working.append(
                        ToolResult(
                            tool_call_id=result.tool_call_id,
                            name=result.name,
                            data={"status": HANDOFF_TRACE_MARKER},
                        ).to_message()
                    )
                    logger.info("agent=%s requested a handoff; stopping loop", self.name)
                    # `answer_so_far` first: for a compound question it is the
                    # source's own half of the answer, and it is the ONLY way
                    # it can have one. A model emits either text or tool calls
                    # (see ModelResponse), and this branch is on the tool-call
                    # side, so `assistant_turn.content` is None in practice -
                    # kept as the fallback for a provider that does populate
                    # both rather than silently preferring an empty string.
                    return TurnResult(
                        reply=handoff.answer_so_far or assistant_turn.content or "",
                        trace=working[trace_start:],
                        handoff=handoff,
                    )

                working.append(result.to_message())

        logger.warning(
            "agent=%s hit max_iterations=%d; returning fallback",
            self.name,
            self._max_iterations,
        )
        return TurnResult(reply=self.fallback_reply, trace=working[trace_start:])

    async def _execute(self, call: ToolCall, context: Context) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            # The model asked for something it was never offered.
            logger.warning("agent=%s requested unknown tool=%s", self.name, call.name)
            return ToolResult.failure(
                name=call.name,
                error=f"unknown tool: {call.name}",
                tool_call_id=call.id,
            )
        # `execute` validates arguments, enforces the context, and never raises.
        return await tool.execute(call, context)


def _handoff_from(result: ToolResult) -> HandoffRequest | None:
    """Recognise the handoff sentinel in a tool result, or None.

    Defensive about the payload's shape rather than trusting it: the sentinel
    only ever comes from `HandoffToAgentTool` today, but a malformed one must
    degrade to "no handoff" instead of raising inside the loop. Even a
    well-formed one is only a REQUEST - see orchestrator.dispatch.
    """
    if not result.ok or not result.data:
        return None
    payload = result.data.get(HANDOFF_SENTINEL_KEY)
    if not isinstance(payload, dict):
        return None
    try:
        return HandoffRequest(
            target_agent=str(payload["target"]),
            reason=str(payload["reason"]),
            context_hint=str(payload["context_hint"]),
            # `.get`, unlike the three above: a sentinel built before this
            # field existed is a well-formed handoff with nothing to say, not
            # a malformed one to discard.
            answer_so_far=str(payload.get("answer_so_far", "")),
        )
    except (KeyError, ValueError):
        logger.warning("malformed handoff sentinel ignored")
        return None
