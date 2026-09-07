"""Shared plumbing for the insights tools that analyze transaction history.

Every tool in this package follows the same shape: resolve a date window,
fetch the relevant rows (all of the user's accounts, or one if `account_id`
narrows it), then compute something over them. Keeping the fetch here means
the account-scoping / ownership rule only has to be gotten right once,
instead of once per tool.

Since Step 13, `load_rows` (formerly `fetch_entries`) is also the seam that
decides WHICH source those rows come from: the real ledger by default, or
one uploaded statement's extracted rows when `context.statement_id` is set
(see app.ai.context.Context.statement_id). Every tool in this package calls
`load_rows` (or, for get_transactions_in_range, its own equivalent branch)
instead of the transactions service directly, so this one function is the
only place that decision is made.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING

from app.ai.context import Context

if TYPE_CHECKING:
    from app.modules.transactions.schemas import TransactionEntryRead
    from supabase import AsyncClient


def day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """Inclusive whole-day UTC bounds - same convention as get_transactions_in_range."""
    return (
        datetime.combine(start, time.min, tzinfo=UTC),
        datetime.combine(end, time.max, tzinfo=UTC),
    )


def months_ago(reference: datetime, months: int) -> datetime:
    """Calendar-correct "N months before `reference`", not a 30-day approximation.

    Clamps the day of month so e.g. Mar 31 minus 1 month lands on Feb 28/29
    instead of overflowing into March.
    """
    month_index = reference.month - 1 - months
    year = reference.year + month_index // 12
    month = month_index % 12 + 1
    day = min(reference.day, calendar.monthrange(year, month)[1])
    return reference.replace(year=year, month=month, day=day)


async def load_rows(
    supabase: AsyncClient,
    context: Context,
    *,
    date_from: datetime,
    date_to: datetime,
    account_id: str | None = None,
    limit: int | None = None,
) -> list[TransactionEntryRead]:
    """Every row in the window, from whichever source is active.

    Two sources, chosen by `context.statement_id` alone (never by anything
    model-authored): the active statement's extracted rows when it is set,
    else the real ledger - one account if `account_id` narrows it, else all
    of them. `account_id` is untrusted model input: `context.resolve_account`
    is the only thing allowed to accept or refuse it, and it is ignored
    entirely in statement mode (a statement has no per-account concept - see
    backend/supabase/migrations/0018_statements.sql). Unlike
    `Context.resolve_account` used elsewhere, `None` here means "every
    account", not "the default account" - these are cross-account analytical
    reads by default.

    `limit` defaults to the ledger's own analytical cap
    (transactions_service.ANALYTICS_MAX_LIMIT) when reading the ledger; in
    statement mode it simply truncates the (already date-filtered, always
    much smaller) row list.
    """
    if context.statement_id is not None:
        return await _load_statement_rows(
            supabase, context, date_from=date_from, date_to=date_to, limit=limit
        )

    from app.modules.transactions import service as transactions_service

    effective_limit = limit or transactions_service.ANALYTICS_MAX_LIMIT

    if account_id is not None:
        resolved = context.resolve_account(account_id)
        return await transactions_service.list_account_transactions_for_owner(
            supabase,
            context.user_id,
            uuid.UUID(resolved),
            date_from=date_from,
            date_to=date_to,
            limit=effective_limit,
        )

    return await transactions_service.list_user_transactions_in_range_for_owner(
        supabase,
        context.user_id,
        date_from=date_from,
        date_to=date_to,
        limit=effective_limit,
    )


async def _load_statement_rows(
    supabase: AsyncClient,
    context: Context,
    *,
    date_from: datetime,
    date_to: datetime,
    limit: int | None,
) -> list[TransactionEntryRead]:
    """Adapts statement_rows into TransactionEntryRead-shaped objects so
    every existing tool keeps working unchanged over either source.
    `journal_id`/`account_id` have no real statement equivalent - both are
    set to the statement's own id, a placeholder never read as a real
    account/journal reference from this branch (no tool in this package
    resolves an account from an entry it already has). Ownership of
    `context.statement_id` was already verified before Context was built
    (see chat/router.py) - statements_service.get_statement_rows re-checks
    it anyway, the same defense-in-depth every other tool's ownership check
    gets.
    """
    from app.modules.ledger.models import LedgerDirection
    from app.modules.statements import service as statements_service
    from app.modules.transactions.schemas import TransactionEntryRead

    assert context.statement_id is not None
    rows = await statements_service.get_statement_rows(
        supabase, context.user_id, context.statement_id
    )

    entries: list[TransactionEntryRead] = []
    for row in rows:
        posted = row.get("posted_date")
        if posted is None:
            continue
        posted_date = date.fromisoformat(posted) if isinstance(posted, str) else posted
        posted_at = datetime.combine(posted_date, time.min, tzinfo=UTC)
        if not (date_from <= posted_at <= date_to):
            continue

        amount = float(row["amount"])
        direction = LedgerDirection.CREDIT if amount >= 0 else LedgerDirection.DEBIT
        description = row.get("description") or ""

        entries.append(
            TransactionEntryRead(
                id=row["id"],
                journal_id=row["statement_id"],
                account_id=row["statement_id"],
                direction=direction,
                amount_minor=round(abs(amount) * 100),
                currency=row["currency"],
                description=description,
                reference=description,
                created_at=posted_at,
            )
        )

    entries.sort(key=lambda e: e.created_at)
    return entries[:limit] if limit is not None else entries
