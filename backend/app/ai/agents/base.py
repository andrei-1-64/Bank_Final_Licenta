"""The agent contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import ClassVar

from app.ai.context import Context
from app.ai.routing import RoutingRule
from app.ai.schemas import Message
from app.ai.turn import TurnResult


class Agent(ABC):
    """Turns a conversation into one final assistant reply."""

    #: Stable identifier the orchestrator routes to.
    name: str

    #: What this agent claims. Rules live with the agent that owns them, so
    #: adding an agent means writing its rules in its own file and calling
    #: `orchestrator.register()` — the orchestrator itself never changes.
    #: Empty means "no rule ever matches me"; such an agent is only reachable
    #: via the LLM fallback or by being the default.
    routing_rules: ClassVar[tuple[RoutingRule, ...]] = ()

    @abstractmethod
    async def run(self, messages: Sequence[Message], context: Context) -> TurnResult:
        """Produce this agent's contribution to the turn. Must not mutate `messages`.

        `TurnResult.reply` is the final text and `TurnResult.trace` is every
        message generated while producing it - assistant tool-call turns and
        their tool-result turns, in order, but never the agent's own system
        prompt. `trace` is empty when the model answers directly with no tool
        calls. Callers persist `trace` alongside `reply` so a replayed
        conversation looks the same as it did live.

        `TurnResult.routing` is left None here: an agent knows what it did, not
        why it was picked. `Orchestrator.dispatch` stamps that on.

        `TurnResult.handoff`, when set, means this agent asked for the rest of
        the turn to continue on another one and stopped early - `reply` may
        then be empty (Step 15). Setting it only ASKS; `dispatch` decides
        whether the handoff is permitted at all. An agent that was never handed
        the handoff tool can never set it.

        `context` is the trusted identity the agent acts for; it is passed to
        every tool and is never derived from the conversation. A handoff does
        NOT rebuild it - the target agent runs on the same frozen instance.
        """
        raise NotImplementedError
