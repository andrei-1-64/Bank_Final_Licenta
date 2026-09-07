"""Azure Document Intelligence (prebuilt-layout) extraction.

Turns a PDF into a flat, ordered list of blocks - paragraphs and table rows -
each tagged with the section heading it fell under. Tables are extracted as
rows, never flattened into prose: a fee schedule's "name -> value" pairing
survives this step, which is the entire reason the source documents are
built with real <table> markup (see backend/knowledge_base/*.html's header
comment).

NOT smoke-tested against a live Document Intelligence resource - there was
no endpoint/key available while writing this. Before relying on it, run
`python -m scripts.ingest_knowledge_base` against a real resource and check
the extracted blocks look right; the SDK's exact response shape can drift
across azure-ai-documentintelligence versions.
"""

from __future__ import annotations

from dataclasses import dataclass

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
from azure.core.credentials import AzureKeyCredential

from app.config import DocumentIntelligenceConfig

_MODEL_ID = "prebuilt-layout"
_HEADING_ROLES = {"title", "sectionHeading"}


@dataclass(frozen=True)
class ExtractedBlock:
    """One retrievable unit: a paragraph, or one row of a table."""

    section_title: str | None
    content: str


def extract_layout(pdf_bytes: bytes, config: DocumentIntelligenceConfig) -> list[ExtractedBlock]:
    client = DocumentIntelligenceClient(
        endpoint=config.endpoint, credential=AzureKeyCredential(config.key)
    )
    poller = client.begin_analyze_document(
        _MODEL_ID, AnalyzeDocumentRequest(bytes_source=pdf_bytes)
    )
    result = poller.result()

    headings_by_page = _headings_by_page(result.paragraphs or [])

    blocks: list[ExtractedBlock] = []
    current_section: str | None = None
    for paragraph in result.paragraphs or []:
        text = (paragraph.content or "").strip()
        if not text:
            continue
        if getattr(paragraph, "role", None) in _HEADING_ROLES:
            current_section = text
            continue
        blocks.append(ExtractedBlock(section_title=current_section, content=text))

    for table in result.tables or []:
        blocks.extend(_table_rows(table, headings_by_page))

    return blocks


def _page_of(item: object) -> int | None:
    regions = getattr(item, "bounding_regions", None) or []
    return regions[0].page_number if regions else None


def _headings_by_page(paragraphs: list) -> list[tuple[int, str]]:
    """(page_number, heading_text) pairs, in page order, for tables to look up."""
    headings = []
    for paragraph in paragraphs:
        if getattr(paragraph, "role", None) not in _HEADING_ROLES:
            continue
        text = (paragraph.content or "").strip()
        page = _page_of(paragraph)
        if text and page is not None:
            headings.append((page, text))
    return sorted(headings, key=lambda item: item[0])


def _heading_for_page(headings_by_page: list[tuple[int, str]], page: int | None) -> str | None:
    """The last heading at or before `page` - None if the table precedes every heading."""
    if page is None:
        return None
    candidate: str | None = None
    for heading_page, text in headings_by_page:
        if heading_page > page:
            break
        candidate = text
    return candidate


def _table_rows(table: object, headings_by_page: list[tuple[int, str]]) -> list[ExtractedBlock]:
    """One block per data row: "header: value; header: value", not one block
    per table - so a single fee/rate row can be retrieved and cited on its
    own instead of forcing the whole table into one oversized chunk."""
    section = _heading_for_page(headings_by_page, _page_of(table))

    header_cells: dict[int, str] = {}
    rows: dict[int, dict[int, str]] = {}
    for cell in table.cells:
        content = (cell.content or "").strip()
        if getattr(cell, "kind", None) == "columnHeader":
            header_cells[cell.column_index] = content
        else:
            rows.setdefault(cell.row_index, {})[cell.column_index] = content

    blocks = []
    for row_index in sorted(rows):
        pairs = [
            f"{header_cells.get(col, f'col{col}')}: {value}"
            for col, value in sorted(rows[row_index].items())
            if value
        ]
        if pairs:
            blocks.append(ExtractedBlock(section_title=section, content="; ".join(pairs)))
    return blocks
