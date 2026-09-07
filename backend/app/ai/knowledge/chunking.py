"""Groups `ExtractedBlock`s into chunks sized for embedding + retrieval.

Packs consecutive blocks that share a section together, greedily, up to
MAX_CHUNK_CHARS. A block never splits mid-block - a table row stays intact
(it's already small and self-contained: "header: value; header: value"), and
a chunk starts fresh whenever the section changes, so a chunk never mixes
content from two different headings.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ai.knowledge.document_intelligence import ExtractedBlock

MAX_CHUNK_CHARS = 1200


@dataclass(frozen=True)
class Chunk:
    section_title: str | None
    content: str


def chunk_blocks(blocks: list[ExtractedBlock]) -> list[Chunk]:
    chunks: list[Chunk] = []
    current_section: str | None = None
    current_parts: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current_parts, current_len
        if current_parts:
            chunks.append(Chunk(section_title=current_section, content="\n".join(current_parts)))
        current_parts = []
        current_len = 0

    for block in blocks:
        section_changed = block.section_title != current_section
        would_overflow = current_len + len(block.content) + 1 > MAX_CHUNK_CHARS
        if current_parts and (section_changed or would_overflow):
            flush()

        current_section = block.section_title
        current_parts.append(block.content)
        current_len += len(block.content) + 1

    flush()
    return chunks
