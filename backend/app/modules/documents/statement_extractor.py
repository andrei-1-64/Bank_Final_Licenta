"""Bank statement extraction via Azure Document Intelligence (Step 13).

A sibling to extractor.py's plain-text PDF extraction, for the one document
type (bank statements) where the AI layer needs STRUCTURED rows, not prose.
Mirrors app/ai/knowledge/document_intelligence.py's client-call shape
(prebuilt-layout, a fresh sync client per call, wrapped for async callers)
but parses `result.tables` into transaction rows instead of flattening
paragraphs/tables into retrievable text blocks.

KNOWN LIMITATION: like document_intelligence.py, this has NOT been
smoke-tested against a live Document Intelligence resource or a real bank
statement PDF - `parse_layout_result` is exercised only against a
hand-built fake `AnalyzeResult` in tests (test_statement_extractor.py). The
SDK's exact response shape, and real banks' table layouts/column headers,
may differ from what is assumed here. Every per-row parse failure is caught
and the row is SKIPPED, never raised - a statement with 40 good rows and 2
malformed ones should still come back with 40 rows, not fail the whole
upload.

Column header matching is diacritic-folded substring matching against a
small set of expected Romanian (and English) headers - see
_COLUMN_ALIASES. A table whose headers don't match enough of these is
skipped entirely (it is probably not the transactions table - some
statements have a summary table above it).

SIGN CONVENTION: `ExtractedRow.amount` is POSITIVE for a credit (money in)
and NEGATIVE for a debit (money out) - this is what
backend/supabase/migrations/0018_statements.sql's statement_rows.amount
carries, and what statements/service.py and the InsightsAgent tools'
statement branch (app/ai/tools/insights/_shared.py's load_rows) assume
without re-checking.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from app.core.exceptions import ValidationError

if TYPE_CHECKING:
    from app.config import DocumentIntelligenceConfig

_MODEL_ID = "prebuilt-layout"

_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("data", "date", "data tranzactiei", "data operatiunii"),
    "description": ("descriere", "detalii", "description", "explicatie", "detalii tranzactie"),
    "debit": ("debit", "iesiri", "suma debit"),
    "credit": ("credit", "intrari", "suma credit"),
    "balance": ("sold", "balance", "sold disponibil", "sold final"),
    "amount": ("suma", "amount", "valoare"),
}

_DATE_FORMATS = ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y")


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(c for c in normalized if not unicodedata.combining(c)).lower().strip()


def _match_column(header_text: str) -> str | None:
    folded = _fold(header_text)
    for column, aliases in _COLUMN_ALIASES.items():
        if any(alias in folded for alias in aliases):
            return column
    return None


class ExtractedRow(BaseModel):
    """One parsed statement line - see the module docstring for the sign
    convention."""

    posted_date: date | None
    description: str
    amount: float
    balance_after: float | None
    row_index: int


class ExtractedStatement(BaseModel):
    """Everything statements/service.create_statement needs to persist an
    upload. `bank_name`/`period_start`/`period_end`/balances are
    best-effort - None when nothing in the document let the parser infer
    them."""

    bank_name: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    currency: str = "RON"
    opening_balance: float | None = None
    closing_balance: float | None = None
    rows: list[ExtractedRow] = []


def _parse_amount(text: str) -> float | None:
    """Romanian/EU-formatted numbers (1.234,56) as well as plain (1234.56)."""
    cleaned = re.sub(r"[^\d,.\-]", "", text.strip())
    if not cleaned or cleaned == "-":
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_date(text: str) -> date | None:
    text = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _cell_text(table: Any, row_index: int, col_index: int) -> str:
    for cell in table.cells:
        if cell.row_index == row_index and cell.column_index == col_index:
            return (cell.content or "").strip()
    return ""


def _header_columns(table: Any) -> tuple[int | None, dict[int, str]]:
    header_row_index: int | None = None
    columns: dict[int, str] = {}
    for cell in table.cells:
        if getattr(cell, "kind", None) != "columnHeader":
            continue
        column = _match_column(cell.content or "")
        if column is None:
            continue
        columns[cell.column_index] = column
        if header_row_index is None or cell.row_index < header_row_index:
            header_row_index = cell.row_index
    return header_row_index, columns


def _parse_table_row(table: Any, row: int, columns: dict[int, str]) -> ExtractedRow | None:
    """Best-effort parse of one data row. Returns None (SKIP, never raise)
    for anything that doesn't look like a real transaction line - a
    subtotal row, a blank spacer row, a row with an unparseable date, etc.
    `row_index` is left at 0 here; parse_layout_result re-numbers globally.
    """
    try:
        by_column = {column: _cell_text(table, row, col) for col, column in columns.items()}

        posted_date = _parse_date(by_column.get("date", ""))
        if posted_date is None:
            return None

        if "amount" in by_column:
            amount = _parse_amount(by_column["amount"])
        else:
            debit = _parse_amount(by_column.get("debit", "")) or 0.0
            credit = _parse_amount(by_column.get("credit", "")) or 0.0
            if debit and credit:
                # Both columns populated is not a normal transaction line -
                # likely a header/subtotal artifact the header-row scan let
                # through.
                return None
            amount = credit if credit else (-debit if debit else None)

        if amount is None:
            return None

        balance_after = (
            _parse_amount(by_column.get("balance", "")) if "balance" in by_column else None
        )

        return ExtractedRow(
            posted_date=posted_date,
            description=by_column.get("description", ""),
            amount=amount,
            balance_after=balance_after,
            row_index=0,
        )
    except Exception:
        return None


def _extract_table_rows(table: Any) -> list[ExtractedRow]:
    header_row_index, columns = _header_columns(table)

    # Need at minimum a date column and either a signed "amount" column or
    # a debit/credit pair - anything less isn't a transactions table.
    has_amount_shape = "amount" in columns.values() or (
        "debit" in columns.values() and "credit" in columns.values()
    )
    if header_row_index is None or "date" not in columns.values() or not has_amount_shape:
        return []

    rows: list[ExtractedRow] = []
    for r in range(header_row_index + 1, table.row_count):
        parsed = _parse_table_row(table, r, columns)
        if parsed is not None:
            rows.append(parsed)
    return rows


def _guess_bank_name(result: Any) -> str | None:
    """First non-empty title/sectionHeading paragraph, if any - the same
    heuristic app/ai/knowledge/document_intelligence.py uses to find
    headings. Best-effort only; None is a normal, expected outcome."""
    for paragraph in getattr(result, "paragraphs", None) or []:
        if getattr(paragraph, "role", None) in ("title", "sectionHeading"):
            content = (paragraph.content or "").strip()
            if content:
                return content
    return None


def parse_layout_result(result: Any) -> ExtractedStatement:
    """Pure function: AzDI's AnalyzeResult -> ExtractedStatement.

    Kept separate from extract_statement so tests can exercise it against a
    hand-built fake result without any network/SDK dependency. `result` is
    typed `Any` rather than `AnalyzeResult` so this module (and its tests)
    don't need azure-ai-documentintelligence importable just to construct a
    fake - the same reasoning transactions/service.py gives for typing a
    PostgREST row `Any`.
    """
    rows: list[ExtractedRow] = []
    for table in getattr(result, "tables", None) or []:
        rows.extend(_extract_table_rows(table))

    # Re-index globally across (possibly multiple) tables, in document order.
    for i, row in enumerate(rows):
        row.row_index = i

    dated = [r.posted_date for r in rows if r.posted_date is not None]

    return ExtractedStatement(
        bank_name=_guess_bank_name(result),
        period_start=min(dated) if dated else None,
        period_end=max(dated) if dated else None,
        rows=rows,
    )


def _extract_statement_sync(
    pdf_bytes: bytes, config: DocumentIntelligenceConfig
) -> ExtractedStatement:
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
    from azure.core.credentials import AzureKeyCredential

    client = DocumentIntelligenceClient(
        endpoint=config.endpoint, credential=AzureKeyCredential(config.key)
    )
    try:
        poller = client.begin_analyze_document(
            _MODEL_ID, AnalyzeDocumentRequest(bytes_source=pdf_bytes)
        )
        result = poller.result()
    except Exception as exc:
        raise ValidationError("Nu am putut analiza extrasul de cont.") from exc

    return parse_layout_result(result)


async def extract_statement(
    pdf_bytes: bytes, config: DocumentIntelligenceConfig
) -> ExtractedStatement:
    """Async wrapper: runs AzDI's blocking client in a worker thread (see
    knowledge/document_intelligence.py, which this mirrors) so the request
    loop is never blocked on the poller."""
    return await asyncio.to_thread(_extract_statement_sync, pdf_bytes, config)
