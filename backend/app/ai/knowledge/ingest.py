"""Ingest every source document in backend/knowledge_base/ into Supabase.

For each `.html` file: export/refresh its PDF (pdf_export), extract
paragraphs+tables (document_intelligence), chunk them (chunking), and embed
only if the extracted content actually changed since last time (content_hash
comparison) - re-running this script after an unrelated code change is a
fast no-op, not a full re-embed.

Entry point is scripts/ingest_knowledge_base.py; this module holds no CLI
concerns of its own so it stays testable without one.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from app.ai.knowledge.chunking import Chunk, chunk_blocks
from app.ai.knowledge.document_intelligence import extract_layout
from app.ai.knowledge.pdf_export import export_html_to_pdf
from app.config import DocumentIntelligenceConfig

if TYPE_CHECKING:
    from supabase import AsyncClient

    from app.ai.providers.embedding_base import EmbeddingProvider

logger = logging.getLogger(__name__)

#: How many chunks go into one embeddings API call. Azure OpenAI's embeddings
#: endpoint accepts a batch of inputs; this just keeps any single request
#: reasonably sized rather than sending hundreds of chunks at once.
EMBED_BATCH_SIZE = 64


def _content_hash(chunks: list[Chunk]) -> str:
    joined = "\x00".join(chunk.content for chunk in chunks)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _title_from_filename(source_path: str) -> str:
    return Path(source_path).stem.replace("-", " ").replace("_", " ").strip().capitalize()


async def ingest_document(
    supabase: AsyncClient,
    embedding_provider: EmbeddingProvider,
    di_config: DocumentIntelligenceConfig,
    html_path: Path,
) -> str:
    """Ingest one HTML source file. Returns a one-line status for the CLI to print."""
    pdf_path = html_path.with_suffix(".pdf")
    if not pdf_path.is_file() or pdf_path.stat().st_mtime < html_path.stat().st_mtime:
        export_html_to_pdf(html_path, pdf_path)

    blocks = extract_layout(pdf_path.read_bytes(), di_config)
    chunks = chunk_blocks(blocks)
    if not chunks:
        return f"{html_path.name}: no content extracted, skipped"

    content_hash = _content_hash(chunks)
    source_path = html_path.name

    existing = (
        await supabase.table("knowledge_documents")
        .select("id, content_hash")
        .eq("source_path", source_path)
        .maybe_single()
        .execute()
    )
    existing_row = existing.data if existing is not None else None
    if existing_row is not None and existing_row["content_hash"] == content_hash:
        return f"{source_path}: unchanged, skipped ({len(chunks)} chunks)"

    document = (
        await supabase.table("knowledge_documents")
        .upsert(
            {
                "source_path": source_path,
                "title": _title_from_filename(source_path),
                "content_hash": content_hash,
            },
            on_conflict="source_path",
        )
        .execute()
    ).data[0]
    document_id = document["id"]

    # Re-embedding replaces every chunk for this document rather than trying
    # to diff old vs new chunk boundaries - simpler, and correct even when
    # chunking itself changes (chunk count/content shifting is expected).
    await supabase.table("knowledge_chunks").delete().eq("document_id", document_id).execute()

    embeddings: list[list[float]] = []
    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start : start + EMBED_BATCH_SIZE]
        embeddings.extend(embedding_provider.embed([chunk.content for chunk in batch]))

    rows = [
        {
            "document_id": document_id,
            "chunk_index": index,
            "section_title": chunk.section_title,
            "content": chunk.content,
            "embedding": embedding,
        }
        for index, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=True))
    ]
    await supabase.table("knowledge_chunks").insert(rows).execute()

    return f"{source_path}: ingested ({len(chunks)} chunks)"


async def ingest_directory(
    supabase: AsyncClient,
    embedding_provider: EmbeddingProvider,
    di_config: DocumentIntelligenceConfig,
    directory: Path,
) -> list[str]:
    html_files = sorted(directory.glob("*.html"))
    if not html_files:
        return [f"no .html files found in {directory}"]

    statuses = []
    for html_path in html_files:
        try:
            statuses.append(
                await ingest_document(supabase, embedding_provider, di_config, html_path)
            )
        except Exception as exc:  # one bad document must not abort the whole run
            logger.exception("Failed to ingest %s", html_path.name)
            statuses.append(f"{html_path.name}: FAILED ({type(exc).__name__}: {exc})")
    return statuses
