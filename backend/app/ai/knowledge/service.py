"""Retrieval side of the knowledge base - the only part of app/ai/knowledge
reachable from the request path. Ingestion (ingest.py) writes; this reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from supabase import AsyncClient

    from app.ai.providers.embedding_base import EmbeddingProvider

DEFAULT_LIMIT = 5


class KnowledgeMatch(TypedDict):
    document_title: str
    section_title: str | None
    content: str


async def search(
    supabase: AsyncClient,
    embedding_provider: EmbeddingProvider,
    query: str,
    limit: int = DEFAULT_LIMIT,
) -> list[KnowledgeMatch]:
    """Embed `query` and return the `limit` closest chunks by cosine similarity.

    Similarity scores are deliberately not returned to the caller: they're a
    ranking signal for the SQL `ORDER BY`, not a calibrated confidence a
    model should reason about numerically.
    """
    [query_embedding] = embedding_provider.embed([query])

    response = await supabase.rpc(
        "match_knowledge_chunks",
        {"query_embedding": query_embedding, "match_count": limit},
    ).execute()

    return [
        KnowledgeMatch(
            document_title=row["document_title"],
            section_title=row.get("section_title"),
            content=row["content"],
        )
        for row in (response.data or [])
    ]
