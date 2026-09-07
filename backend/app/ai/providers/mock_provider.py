"""Deterministic provider for tests. Never touches the network."""

from __future__ import annotations

from collections.abc import Sequence

from app.ai.providers.base import ModelProvider, ProviderError, ToolSpec
from app.ai.schemas import Message, ModelResponse


class MockProvider(ModelProvider):
    """Replays a scripted list of `ModelResponse`, one per `complete()` call.

    Tests own the script, so they control exactly what the "model" does.
    Set `repeat_last=True` to keep returning the final entry forever — that is
    how the max-iterations guard is exercised.
    """

    def __init__(
        self,
        script: Sequence[ModelResponse],
        *,
        repeat_last: bool = False,
    ) -> None:
        if not script:
            raise ValueError("MockProvider needs at least one scripted response")
        self._script = list(script)
        self._repeat_last = repeat_last
        self.calls: list[list[Message]] = []
        self.tool_specs_seen: list[list[ToolSpec]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def complete(
        self,
        messages: Sequence[Message],
        tool_specs: Sequence[ToolSpec] | None = None,
    ) -> ModelResponse:
        index = len(self.calls)
        self.calls.append(list(messages))
        self.tool_specs_seen.append(list(tool_specs or []))

        if index < len(self._script):
            return self._script[index]
        if self._repeat_last:
            return self._script[-1]
        raise ProviderError(
            f"MockProvider script exhausted after {len(self._script)} response(s)"
        )
