"""Per-user bank statement storage - extracted, unverified rows from an
uploaded statement PDF.

NEVER written to the ledger: this module's tables (statements,
statement_rows - see backend/supabase/migrations/0018_statements.sql) are a
completely separate read/write surface from app.modules.transactions, which
stays the sole ledger read path, and from post_transaction, the sole ledger
WRITE path. Nothing here ever touches accounts, journal_transactions, or
ledger_entries.

Functions here take `user_id: str` rather than a `UserRead`, matching
`app.modules.documents.service`'s convention - the AI tool layer only ever
has a `Context` (which carries `user_id`, not a full `UserRead`), so this
keeps one ownership-check shape usable from both the router and AI tools.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.exceptions import NotFoundError
from supabase import AsyncClient

_STATEMENT_COLUMNS = (
    "id, created_at, user_id, conversation_id, document_id, bank_name, "
    "period_start, period_end, currency, opening_balance, closing_balance, "
    "row_count"
)


async def create_statement(
    supabase: AsyncClient,
    *,
    user_id: str,
    conversation_id: str | None,
    document_id: str | None,
    bank_name: str | None,
    period_start: str | None,
    period_end: str | None,
    currency: str,
    opening_balance: float | None,
    closing_balance: float | None,
    rows: list[dict],
) -> dict:
    """Insert the statement, then its extracted rows.

    Not atomic across the two inserts - PostgREST has no cross-table
    transaction. An interrupted upload can leave a statement with zero
    rows; that is a safe, retriable state (row_count reports it honestly),
    never a partially-wrong ledger, since the ledger is never touched here
    at all.
    """
    inserted = (
        await supabase.table("statements")
        .insert(
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "document_id": document_id,
                "bank_name": bank_name,
                "period_start": period_start,
                "period_end": period_end,
                "currency": currency,
                "opening_balance": opening_balance,
                "closing_balance": closing_balance,
                "row_count": len(rows),
            }
        )
        .execute()
    )
    statement_id = inserted.data[0]["id"]

    if rows:
        await (
            supabase.table("statement_rows")
            .insert([{**row, "statement_id": statement_id} for row in rows])
            .execute()
        )

    resp = (
        await supabase.table("statements")
        .select(_STATEMENT_COLUMNS)
        .eq("id", statement_id)
        .maybe_single()
        .execute()
    )
    return resp.data


async def list_statements(supabase: AsyncClient, user_id: str) -> list[dict]:
    resp = (
        await supabase.table("statements")
        .select(_STATEMENT_COLUMNS)
        .eq("user_id", user_id)
        .is_("deleted_at", "null")
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data


async def get_statement(supabase: AsyncClient, user_id: str, statement_id: str) -> dict:
    """Ownership-checked read, without rows. A missing id, one owned by
    someone else, or a soft-deleted one all look identical to the caller -
    NotFoundError either way, same as documents_service.get_document."""
    resp = (
        await supabase.table("statements")
        .select(_STATEMENT_COLUMNS)
        .eq("id", statement_id)
        .is_("deleted_at", "null")
        .maybe_single()
        .execute()
    )
    statement = resp.data if resp is not None else None
    if statement is None or statement["user_id"] != user_id:
        raise NotFoundError("Statement not found.")
    return statement


async def get_statement_with_rows(
    supabase: AsyncClient, user_id: str, statement_id: str
) -> dict:
    statement = await get_statement(supabase, user_id, statement_id)
    rows_resp = (
        await supabase.table("statement_rows")
        .select("*")
        .eq("statement_id", statement_id)
        .order("row_index")
        .execute()
    )
    statement["rows"] = rows_resp.data
    return statement


async def get_statement_rows(
    supabase: AsyncClient, user_id: str, statement_id: str
) -> list[dict]:
    """Ownership-checked row read - the seam InsightsAgent's `load_rows`
    uses (see app/ai/tools/insights/_shared.py). Raises NotFoundError for a
    foreign/nonexistent/deleted statement, never silently returning someone
    else's rows."""
    await get_statement(supabase, user_id, statement_id)
    resp = (
        await supabase.table("statement_rows")
        .select("*")
        .eq("statement_id", statement_id)
        .order("row_index")
        .execute()
    )
    return resp.data


async def soft_delete_statement(supabase: AsyncClient, user_id: str, statement_id: str) -> None:
    await get_statement(supabase, user_id, statement_id)
    await (
        supabase.table("statements")
        .update({"deleted_at": datetime.now(UTC).isoformat()})
        .eq("id", statement_id)
        .execute()
    )


async def get_latest_statement_for_conversation(
    supabase: AsyncClient, user_id: str, conversation_id: str
) -> dict | None:
    """The implicit "active statement" lookup for a chat turn.

    Mirrors `active_document_id`'s explicit-per-turn mechanism with one
    addition: when the incoming ChatRequest names no statement_id, the most
    recently uploaded (non-deleted) statement in THIS conversation becomes
    active automatically - see chat/router.py, which calls this only after
    the explicit path found nothing. This is what lets "last statement
    uploaded stays active until another is uploaded" work without a
    frontend picker UI (see app.ai.context.Context.statement_id's
    docstring).
    """
    resp = (
        await supabase.table("statements")
        .select(_STATEMENT_COLUMNS)
        .eq("user_id", user_id)
        .eq("conversation_id", conversation_id)
        .is_("deleted_at", "null")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = resp.data
    return rows[0] if rows else None


async def set_row_category(
    supabase: AsyncClient, user_id: str, statement_id: str, row_id: str, category: str
) -> None:
    """Persists categorize_transactions's result onto the extracted row -
    NEVER onto the ledger (statement_rows is a completely separate table,
    see this module's docstring). Ownership-checked via get_statement
    before the row update, same as every other write here."""
    await get_statement(supabase, user_id, statement_id)
    await (
        supabase.table("statement_rows")
        .update({"extracted_category": category})
        .eq("id", row_id)
        .eq("statement_id", statement_id)
        .execute()
    )
