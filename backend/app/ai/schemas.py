"""Provider-agnostic conversation types.

These types are the boundary between the agent loop and any concrete model
provider. Nothing here may import a vendor SDK — that keeps swapping providers
(or adding a second one) additive.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["system", "user", "assistant", "tool"]


class ToolCall(BaseModel):
    """A model's request to invoke one tool.

    ``arguments`` is whatever the model produced, already JSON-decoded but NOT
    yet validated — the owning tool's Pydantic schema is the only thing allowed
    to decide whether it is acceptable.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """The outcome of executing one tool call, ready to feed back to the model."""

    tool_call_id: str = ""
    name: str
    ok: bool = True
    data: dict[str, Any] | None = None
    error: str | None = None

    def to_content(self) -> str:
        """Serialise for the model. Errors are reported, never raised at it."""
        if self.ok:
            return json.dumps({"ok": True, "result": self.data or {}})
        return json.dumps({"ok": False, "error": self.error or "tool failed"})

    def to_message(self) -> Message:
        return Message(
            role="tool",
            content=self.to_content(),
            tool_call_id=self.tool_call_id,
            name=self.name,
        )

    @classmethod
    def failure(cls, name: str, error: str, tool_call_id: str = "") -> ToolResult:
        return cls(tool_call_id=tool_call_id, name=name, ok=False, error=error)


class Message(BaseModel):
    """One turn of conversation.

    ``tool_calls`` is set only on assistant turns; ``tool_call_id`` / ``name``
    only on tool turns.
    """

    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


class ModelResponse(BaseModel):
    """One provider reply: EITHER final text OR a batch of tool calls."""

    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exactly_one_kind(self) -> ModelResponse:
        if self.text is not None and self.tool_calls:
            raise ValueError("ModelResponse carries either text or tool_calls, not both")
        if self.text is None and not self.tool_calls:
            raise ValueError("ModelResponse must carry either text or tool_calls")
        return self

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def to_assistant_message(self) -> Message:
        return Message(role="assistant", content=self.text, tool_calls=list(self.tool_calls))
