"""The provider contract.

Adding a provider means implementing `complete` — nothing above this line
changes. Implementations must not raise on model-authored nonsense; they
translate whatever came back into a `ModelResponse`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from app.ai.schemas import Message, ModelResponse

# A tool advertisement in the shape providers hand to the model.
ToolSpec = dict[str, Any]


class ProviderError(RuntimeError):
    """The provider could not produce a response (network, auth, quota, ...)."""


class ModelProvider(ABC):
    """Turns a conversation + the available tools into one model response."""

    @abstractmethod
    def complete(
        self,
        messages: Sequence[Message],
        tool_specs: Sequence[ToolSpec] | None = None,
    ) -> ModelResponse:
        """Produce the next model response. Must not mutate `messages`."""
        raise NotImplementedError
