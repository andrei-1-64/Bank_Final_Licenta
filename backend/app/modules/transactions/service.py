import uuid
from datetime import datetime
from typing import Any

from supabase import AsyncClient

from app.modules.accounts import service as accounts_service
from app.modules.transactions.schemas import TransactionEntryRead
from app.modules.users.schemas import UserRead

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# Analytical reads (the AI insights layer) legitimately need far more rows than
# a user-facing list: spotting a spending trend over a quarter means seeing the
# whole quarter, not its 50 most recent entries.
ANALYTICS_DEFAULT_LIMIT = 500
ANALYTICS_MAX_LIMIT = 2000


def _to_entry(row: Any) -> TransactionEntryRead:
    """Build one entry from a PostgREST row.

    `row` is `Any` rather than `dict` on purpose: supabase-py types `resp.data`
    as loose JSON, so a narrower annotation here would only move the type error
    to every call site (see the existing Supabase-typing noise in this module).
    """
    return TransactionEntryRead(
        id=row["id"],
        journal_id=row["journal_id"],
        account_id=row["account_id"],
        direction=row["direction"],
        amount_minor=row["amount_minor"],
        currency=row["currency"],
        description=row["journal"]["description"],
        reference=row["journal"]["reference"],
        created_at=row["created_at"],
    )


async def list_user_transactions_in_range_for_owner(
    supabase: AsyncClient,
    user_id: uuid.UUID | str,
    *,
    date_from: datetime,
    date_to: datetime,
    limit: int = ANALYTICS_DEFAULT_LIMIT,
) -> list[TransactionEntryRead]:
    """Every transaction the user has, across ALL their accounts, in a window.

    The cross-account counterpart to `list_account_transactions_for_owner`:
    analysis ("unde s-au dus banii?") is a question about a person, not about
    one account. Ordered oldest-first, because that is the direction a trend
    reads in.

    Ownership is enforced by deriving the account set from `user_id` rather
    than accepting one - there is no account parameter to abuse.
    """
    accounts = await accounts_service.list_accounts_for_owner(supabase, user_id)
    account_ids = [str(account["id"]) for account in accounts]
    if not account_ids:
        # A user with no accounts has no transactions. Legitimate, not an error.
        return []

    limit = max(1, min(limit, ANALYTICS_MAX_LIMIT))

    resp = (
        await supabase.table("ledger_entries")
        .select("*, journal:journal_transactions(description, reference)")
        .in_("account_id", account_ids)
        .gte("created_at", date_from.isoformat())
        .lte("created_at", date_to.isoformat())
        .order("created_at")
        .limit(limit)
        .execute()
    )
    return [_to_entry(row) for row in resp.data]


async def list_account_transactions_for_owner(
    supabase: AsyncClient,
    user_id: uuid.UUID | str,
    account_id: uuid.UUID,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> list[TransactionEntryRead]:
    """Transaction list for callers holding a bare user id (the AI layer's
    `Context`), mirroring `accounts_service.get_account_for_owner` - which is
    also what enforces ownership here. `list_account_transactions` below is the
    same read for the banking modules, which have a full `UserRead`."""
    # Ownership check - raises AccountNotFoundError (404) if this isn't the
    # caller's account, without leaking whether it exists at all.
    await accounts_service.get_account_for_owner(supabase, user_id, account_id)

    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)

    query = (
        supabase.table("ledger_entries")
        .select("*, journal:journal_transactions(description, reference)")
        .eq("account_id", str(account_id))
    )
    if date_from is not None:
        query = query.gte("created_at", date_from.isoformat())
    if date_to is not None:
        query = query.lte("created_at", date_to.isoformat())

    resp = await query.order("created_at", desc=True).limit(limit).offset(offset).execute()

    return [
        TransactionEntryRead(
            id=row["id"],
            journal_id=row["journal_id"],
            account_id=row["account_id"],
            direction=row["direction"],
            amount_minor=row["amount_minor"],
            currency=row["currency"],
            description=row["journal"]["description"],
            reference=row["journal"]["reference"],
            created_at=row["created_at"],
        )
        for row in resp.data
    ]


async def list_account_transactions(
    supabase: AsyncClient,
    user: UserRead,
    account_id: uuid.UUID,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> list[TransactionEntryRead]:
    return await list_account_transactions_for_owner(
        supabase,
        user.id,
        account_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
