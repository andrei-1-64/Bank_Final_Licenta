"""Ingest backend/knowledge_base/*.html into Supabase for the docs agent.

    python -m scripts.ingest_knowledge_base

For each source file: export/refresh its PDF (Microsoft Edge headless - see
app/ai/knowledge/pdf_export.py; install Edge or export by hand via Ctrl+P if
that fails), extract paragraphs and tables via Azure Document Intelligence's
prebuilt-layout model, chunk them, and embed + store any chunk whose content
actually changed since the last run.

Needs AI_PROVIDER=azure (real embeddings) plus AZURE_OPENAI_EMBEDDING_DEPLOYMENT
and AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT/KEY in backend/.env - see
.env.example. Run whenever a file under knowledge_base/ changes; safe to
re-run any time, unchanged documents are a no-op.
"""

import asyncio
from pathlib import Path

from app.ai.knowledge.ingest import ingest_directory
from app.ai.providers.azure_embedding_provider import AzureEmbeddingProvider
from app.config import ConfigurationError, settings
from app.db.supabase_client import get_client

_KNOWLEDGE_BASE_DIR = Path(__file__).resolve().parent.parent / "knowledge_base"


async def run() -> list[str]:
    supabase = await get_client()
    embedding_provider = AzureEmbeddingProvider(settings)
    di_config = settings.require_document_intelligence()
    return await ingest_directory(supabase, embedding_provider, di_config, _KNOWLEDGE_BASE_DIR)


def main() -> int:
    if settings.AI_PROVIDER.strip().lower() != "azure":
        print(
            "AI_PROVIDER is not 'azure' - set it in backend/.env along with "
            "AZURE_OPENAI_EMBEDDING_DEPLOYMENT and "
            "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT/KEY before ingesting "
            "(real embeddings and Document Intelligence are required; the "
            "mock provider only exists for chat)."
        )
        return 2

    try:
        statuses = asyncio.run(run())
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        return 2

    print(f"Ingested {_KNOWLEDGE_BASE_DIR}:")
    for status in statuses:
        print(f"  {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
