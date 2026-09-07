"""Supabase (PostgREST) client + Postgres-error-to-AppError mapping.

The process-wide client is a plain HTTP client (no connection/session to
open or close per request, unlike the SQLAlchemy AsyncSession this replaces)
so it's safely shareable across requests. `get_supabase()` exists purely as
a FastAPI dependency seam - tests use `dependency_overrides[get_supabase]`
the same way the old code used `dependency_overrides[get_db]`.
"""

from collections.abc import AsyncIterator

from postgrest.exceptions import APIError
from supabase import AsyncClient, acreate_client

from app.config import settings
from app.core.exceptions import (
    AccountClosedError,
    AccountNotFoundError,
    AppError,
    CurrencyMismatchError,
    InsufficientFundsError,
    InvalidLedgerLegsError,
)

# Custom SQLSTATEs raised by the RPC functions in
# backend/supabase/migrations/0002_ledger_functions.sql - kept in sync with
# that file's header comment.
_LEDGER_ERROR_CODES: dict[str, type[AppError]] = {
    "BK001": InsufficientFundsError,
    "BK002": AccountNotFoundError,
    "BK003": AccountClosedError,
    "BK004": CurrencyMismatchError,
    "BK005": InvalidLedgerLegsError,
}

UNIQUE_VIOLATION = "23505"


def map_postgrest_error(exc: APIError) -> AppError | None:
    """Translate a ledger RPC's custom SQLSTATE into the matching AppError
    subclass. Returns None for anything else (unique_violation and generic
    errors), which callers handle themselves - unique_violation is caught
    and turned into an idempotent replay *inside* the RPC functions, so a
    caller only ever sees it for constraints the RPC doesn't already own
    (e.g. register_user's email/national_id uniqueness)."""
    error_cls = _LEDGER_ERROR_CODES.get(exc.code)
    return error_cls(exc.message) if error_cls else None


_client: AsyncClient | None = None


async def get_client() -> AsyncClient:
    global _client
    if _client is None:
        _client = await acreate_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
    return _client


async def get_supabase() -> AsyncIterator[AsyncClient]:
    yield await get_client()
