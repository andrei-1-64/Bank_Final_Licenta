"""Orchestrates GET /accounts/{id}/statement/pdf: ownership check,
opening/closing balance, transaction fetch, PDF render. See
statement_pdf.py for the actual rendering - this module is the only one
that touches the database.

Deliberately its own file rather than folded into accounts/service.py:
that module's functions all return the plain dicts the rest of the app
expects (AccountRead, etc.), whereas this one's whole job is producing PDF
bytes for one specific endpoint.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time

from supabase import AsyncClient

from app.core.exceptions import ValidationError
from app.modules.accounts import service as accounts_service
from app.modules.accounts.statement_pdf import StatementRow, render_statement_pdf
from app.modules.ledger.models import LedgerDirection
from app.modules.users.schemas import UserRead

#: Bounds how many rows one statement can cover - generous for any
#: realistic personal-account period, and keeps the PDF's page count
#: bounded (see statement_pdf.py's per-page row budget).
MAX_ROWS = 2000


def _signed_amount(amount_minor: int, direction: str) -> int:
    return amount_minor if direction == LedgerDirection.CREDIT else -amount_minor


async def _balance_before(supabase: AsyncClient, account_id: uuid.UUID, before: datetime) -> int:
    """The account's balance AS OF the instant just before `before` -
    summed here rather than via ledger_service.get_balance's RPC, which
    only computes the CURRENT total with no date cutoff."""
    resp = (
        await supabase.table("ledger_entries")
        .select("amount_minor, direction")
        .eq("account_id", str(account_id))
        .lt("created_at", before.isoformat())
        .execute()
    )
    return sum(_signed_amount(row["amount_minor"], row["direction"]) for row in (resp.data or []))


async def generate_statement_pdf(
    supabase: AsyncClient,
    user: UserRead,
    account_id: uuid.UUID,
    *,
    period_start: date,
    period_end: date,
) -> bytes:
    if period_start > period_end:
        raise ValidationError("Data de început trebuie să fie înainte de data de sfârșit.")

    # Ownership-checked - raises AccountNotFoundError (404) for an account
    # that isn't this user's, without leaking whether it exists at all.
    account = await accounts_service.get_account_for_owner(supabase, user.id, account_id)

    period_start_dt = datetime.combine(period_start, time.min, tzinfo=UTC)
    period_end_dt = datetime.combine(period_end, time.max, tzinfo=UTC)  # inclusive of the whole day

    opening_balance_minor = await _balance_before(supabase, account_id, period_start_dt)

    resp = (
        await supabase.table("ledger_entries")
        .select("*, journal:journal_transactions(description, reference)")
        .eq("account_id", str(account_id))
        .gte("created_at", period_start_dt.isoformat())
        .lte("created_at", period_end_dt.isoformat())
        .order("created_at")
        .limit(MAX_ROWS)
        .execute()
    )

    rows = [
        StatementRow(
            created_at=row["created_at"],
            description=(row.get("journal") or {}).get("description") or "-",
            amount_minor=_signed_amount(row["amount_minor"], row["direction"]),
            currency=row["currency"],
        )
        for row in (resp.data or [])
    ]
    closing_balance_minor = opening_balance_minor + sum(row.amount_minor for row in rows)

    return render_statement_pdf(
        holder_name=f"{user.first_name} {user.last_name}",
        national_id=user.national_id,
        account_name=account["name"],
        iban=account.get("iban"),
        currency=account["currency"],
        period_start=period_start,
        period_end=period_end,
        opening_balance_minor=opening_balance_minor,
        closing_balance_minor=closing_balance_minor,
        rows=rows,
    )
