"""The tool contract.

`execute()` is the only entry point the agent uses. It enforces two rules:

1. Model-authored arguments are never trusted — they pass through the tool's own
   Pydantic schema first, and a rejection becomes a normal failed `ToolResult`
   rather than an exception that kills the loop.
2. Identity is never taken from those arguments. Every tool receives the trusted
   `Context` alongside them and must resolve who it is acting for from there.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from app.ai.context import Context, IdentityError
from app.ai.schemas import ToolCall, ToolResult


class Tool(ABC):
    """A single capability exposed to the model."""

    name: ClassVar[str]
    description: ClassVar[str]
    input_schema: ClassVar[type[BaseModel]]

    #: Read-only tools run freely. Write tools come in two shapes: low-stakes,
    #: reversible ones execute directly (see e.g. app/ai/tools/banking/
    #: freeze_card.py - the system prompt asks the model to get a "yes" from
    #: the user in conversation first, but nothing enforces that here), while
    #: a higher-stakes one (propose_card_order.py) stays read_only and only
    #: ever proposes - the actual write happens through a normal authenticated
    #: REST call the user's own UI click triggers, never from inside the tool.
    read_only: ClassVar[bool] = True

    @abstractmethod
    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        """Do the work.

        `validated_input` has passed `input_schema` but is still model-authored
        and UNTRUSTED. `context` is the trusted identity — resolve every user or
        account reference through it.
        """
        raise NotImplementedError

    async def execute(self, call: ToolCall, context: Context) -> ToolResult:
        """Validate the model's arguments, then run. Never raises."""
        try:
            validated = self.input_schema.model_validate(call.arguments)
        except ValidationError as exc:
            return ToolResult.failure(
                name=self.name,
                error=f"invalid input: {_summarise(exc)}",
                tool_call_id=call.id,
            )

        try:
            result = await self.run(validated, context)
        except IdentityError as exc:
            # Caught before the generic handler below so the model gets a clear,
            # actionable refusal instead of "tool execution failed". The message
            # is safe by construction — it never names the refused resource.
            return ToolResult.failure(
                name=self.name,
                error=f"access denied: {exc}",
                tool_call_id=call.id,
            )
        except Exception as exc:  # a broken tool must not take the loop down
            return ToolResult.failure(
                name=self.name,
                error=f"tool execution failed: {type(exc).__name__}",
                tool_call_id=call.id,
            )

        # `run` implementations don't know the call id; stamp it here.
        return result.model_copy(update={"tool_call_id": call.id, "name": self.name})

    def spec(self) -> dict[str, Any]:
        """Advertisement in the shape providers hand to the model."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema.model_json_schema(),
            },
        }


def _summarise(exc: ValidationError) -> str:
    """Compact, PII-free description of what was wrong with the arguments."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '(root)'}: {error['msg']}"
        for error in exc.errors()
    )
