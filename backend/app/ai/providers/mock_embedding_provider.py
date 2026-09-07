"""Deterministic embedding provider for tests and AI_PROVIDER=mock. Never touches the network."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from app.ai.providers.embedding_base import EmbeddingProvider

#: Matches text-embedding-3-small's dimension, so a mock vector is
#: interchangeable with a real one anywhere the schema (VECTOR(1536)) cares.
DIMENSION = 1536


class MockEmbeddingProvider(EmbeddingProvider):
    """Hashes each text into a fixed-length vector.

    Not semantically meaningful - two similar sentences do not get similar
    vectors - but stable (same text always yields the same vector) and
    network-free, which is all a test needs.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [_hash_to_vector(text) for text in texts]


def _hash_to_vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    # Repeat the 32-byte digest to fill DIMENSION floats in [-1, 1).
    raw = (digest * (DIMENSION // len(digest) + 1))[:DIMENSION]
    return [(byte / 127.5) - 1.0 for byte in raw]
