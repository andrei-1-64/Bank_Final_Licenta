"""Azure OpenAI embeddings (text-embedding-3-small via a deployment name).

Mirrors azure_provider.py's shape: config comes from `Settings`, nothing
Azure-specific leaks past this module.
"""

from __future__ import annotations

from collections.abc import Sequence

from openai import AzureOpenAI, OpenAIError

from app.ai.providers.embedding_base import EmbeddingProvider, EmbeddingProviderError
from app.config import Settings, get_settings


class AzureEmbeddingProvider(EmbeddingProvider):
    def __init__(
        self,
        settings: Settings | None = None,
        client: AzureOpenAI | None = None,
    ) -> None:
        self._config = (settings or get_settings()).require_azure_embedding()
        self._client = client or AzureOpenAI(
            azure_endpoint=self._config.endpoint,
            api_key=self._config.api_key,
            api_version=self._config.api_version,
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = self._client.embeddings.create(
                model=self._config.deployment,
                input=list(texts),
            )
        except OpenAIError as exc:  # network, auth, quota, bad deployment, ...
            raise EmbeddingProviderError(f"Azure OpenAI embeddings call failed: {exc}") from exc

        # The API returns items in request order, but `index` is authoritative
        # rather than assumed - sorting by it is cheap insurance against a
        # provider that ever reorders.
        by_index = sorted(response.data, key=lambda item: item.index)
        return [item.embedding for item in by_index]
