"""Search the ingested product/fees documentation - semantic, not keyword.

Read-only, no identity to scope: unlike the banking tools, this reads shared
product documentation (same content for every user), not anything owned by
the caller. `context` is accepted only because `Tool.run`'s signature
requires it, and is otherwise unused here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.ai.context import Context
from app.ai.knowledge import service as knowledge_service
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool

if TYPE_CHECKING:
    from supabase import AsyncClient

    from app.ai.providers.embedding_base import EmbeddingProvider


class SearchKnowledgeBaseInput(BaseModel):
    query: str = Field(
        min_length=1,
        description=(
            "What to search for, in the user's own words (e.g. 'comision retragere "
            "numerar din strainatate'). Search is semantic, not exact keyword "
            "matching, so a natural-language question works better than a single word."
        ),
    )


class SearchKnowledgeBaseTool(Tool):
    name = "search_knowledge_base"
    description = (
        "Search the bank's product and fee documentation for passages relevant to "
        "the user's question. Returns the most relevant excerpts, each tagged with "
        "the document and section they came from. Returns an empty list when nothing "
        "relevant is found - that means the documentation doesn't cover it, not that "
        "the search failed."
    )
    input_schema = SearchKnowledgeBaseInput
    read_only = True

    def __init__(self, supabase: AsyncClient, embedding_provider: EmbeddingProvider) -> None:
        self._supabase = supabase
        self._embedding_provider = embedding_provider

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, SearchKnowledgeBaseInput)
        del context  # shared documentation, not user-owned - nothing to scope

        matches = await knowledge_service.search(
            self._supabase, self._embedding_provider, validated_input.query
        )

        return ToolResult(
            name=self.name,
            data={
                "results": [
                    {
                        "document": match["document_title"],
                        "section": match["section_title"],
                        "content": match["content"],
                    }
                    for match in matches
                ]
            },
        )
