"""The embedding-provider contract.

Separate from `ModelProvider` (base.py) on purpose: embeddings are a
different model family from chat completions, and `ModelProvider`'s
docstring already closes its contract at `complete`. A RAG-only capability
gets its own small interface instead of widening that one.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence


class EmbeddingProviderError(RuntimeError):
    """The provider could not produce embeddings (network, auth, quota, ...)."""


class EmbeddingProvider(ABC):
    """Turns a batch of texts into one embedding vector each, same order in as out."""

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed `texts`. Returns one vector per input, in the same order."""
        raise NotImplementedError
