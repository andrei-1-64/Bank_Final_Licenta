"""Shared plumbing for the planning tools: current balance + a recent
income/spending baseline, both scoped by account the same way insights'
tools are ("no account_id" means all of the user's accounts, not a single
default one).

Duplicated in shape from `app.ai.tools.insights._shared` rather than shared
with it - the two tool packages are kept apart deliberately (see
`tools/insights/__init__.py`), and this module's `months_ago` in particular
has different callers than insights' (a fixed 3-month baseline here, vs a
model-supplied window there).
"""

from __future__ import annotations

import calendar
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.ai.context import Context

if TYPE_CHECKING:
    from supabase import AsyncClient

#: The user's primary currency when they have no accounts to read one from -
#: this is a Romanian bank, and every seeded/demo account defaults to it too.
FALLBACK_CURRENCY = "RON"

#: How far back "recent" spending/income is averaged over, for every planning
#: tool that needs a baseline rate.
BASELINE_MONTHS = 3


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


async def current_balance(
    supabase: AsyncClient, context: Context, *, account_id: str | None = None
) -> tuple[int, str]:
    """(balance_minor, currency) - one account if named, else summed across
    all of the user's accounts.

    Summing assumes a single currency per user, the same simplification the
    rest of the AI layer makes (no tool anywhere converts between
    currencies); accounts in a different currency than the first one found
    are excluded from the sum rather than silently mixed into it.
    """
    from app.modules.accounts import service as accounts_service
    from app.modules.ledger import service as ledger_service

    if account_id is not None:
        resolved = context.resolve_account(account_id)
        account = await accounts_service.get_account_for_owner(
            supabase, context.user_id, resolved
        )
        balance_minor = await ledger_service.get_balance(supabase, uuid.UUID(str(account["id"])))
        return balance_minor, account["currency"]

    accounts = await accounts_service.list_accounts_for_owner(supabase, context.user_id)
    if not accounts:
        return 0, FALLBACK_CURRENCY

    primary_currency = accounts[0]["currency"]
    total = 0
    for account in accounts:
        if account["currency"] != primary_currency:
            continue
        total += await ledger_service.get_balance(supabase, uuid.UUID(str(account["id"])))
    return total, primary_currency


async def recent_monthly_averages(
    supabase: AsyncClient,
    context: Context,
    *,
    account_id: str | None = None,
    months: int = BASELINE_MONTHS,
) -> tuple[int, int, bool]:
    """(avg_monthly_income_minor, avg_monthly_spending_minor, has_history)
    over the last `months` calendar months, across one account or all of
    them. The averages are zero (not an error) when there is no transaction
    history; `has_history` is what callers check to tell that apart from a
    genuinely-zero income/spending month."""
    from app.modules.transactions import service as transactions_service

    now = datetime.now(UTC)
    date_from = months_ago(now, months)

    if account_id is not None:
        resolved = context.resolve_account(account_id)
        entries = await transactions_service.list_account_transactions_for_owner(
            supabase,
            context.user_id,
            uuid.UUID(resolved),
            date_from=date_from,
            date_to=now,
            limit=transactions_service.ANALYTICS_MAX_LIMIT,
        )
    else:
        entries = await transactions_service.list_user_transactions_in_range_for_owner(
            supabase,
            context.user_id,
            date_from=date_from,
            date_to=now,
            limit=transactions_service.ANALYTICS_MAX_LIMIT,
        )

    income = sum(e.amount_minor for e in entries if e.direction.value == "credit")
    spending = sum(e.amount_minor for e in entries if e.direction.value == "debit")
    return round(income / months), round(spending / months), bool(entries)
