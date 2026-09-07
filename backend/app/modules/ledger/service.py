"""ledger.post_transaction() - the ONLY money-writer in the system. Every
money movement (transfers here; payments/cards/scheduled_transfers/
round_ups elsewhere) must go through this function and nothing else.

The locking/idempotency/atomicity logic now lives in Postgres, not here -
see backend/supabase/migrations/0002_ledger_functions.sql::post_transaction.
PostgREST runs each RPC call inside one real transaction, which is the only
way to keep SELECT...FOR UPDATE row locks held across the
lock -> check -> insert sequence this needs; that can't be done from
separate REST calls. This module is a thin wrapper: build the legs JSON,
call the RPC, map Postgres errors back to the same typed exceptions callers
already expect.
"""

import uuid
from collections.abc import Sequence

from postgrest.exceptions import APIError
from supabase import AsyncClient

from app.core.exceptions import CurrencyMismatchError, InvalidLedgerLegsError
from app.core.money import ensure_positive_minor
from app.db.supabase_client import map_postgrest_error
from app.modules.ledger.models import LedgerDirection
from app.modules.ledger.schemas import LedgerLeg


def _validate_legs(legs: Sequence[LedgerLeg]) -> None:
    """Fail-fast client-side pre-check - cheap, avoids a network round trip
    for obviously-bad input. The RPC function re-validates all of this
    server-side too; that's the real enforcement boundary now."""
    if len(legs) < 2:
        raise InvalidLedgerLegsError("A journal needs at least two legs.")

    currencies = {leg.currency.upper() for leg in legs}
    if len(currencies) > 1:
        raise CurrencyMismatchError(
            "All legs of one journal must share a currency; cross-currency "
            "movements are out of scope."
        )

    for leg in legs:
        ensure_positive_minor(leg.amount_minor, field="leg.amount_minor")

    debits = sum(leg.amount_minor for leg in legs if leg.direction == LedgerDirection.DEBIT)
    credits = sum(leg.amount_minor for leg in legs if leg.direction == LedgerDirection.CREDIT)
    if debits != credits:
        raise InvalidLedgerLegsError(f"Unbalanced journal: debits={debits} != credits={credits}.")


async def get_balance(supabase: AsyncClient, account_id: uuid.UUID) -> int:
    """Balance = SUM(credits) - SUM(debits) for this account, computed
    server-side (see get_account_balance in the RPC migration)."""
    resp = await supabase.rpc("get_account_balance", {"p_account_id": str(account_id)}).execute()
    return resp.data


async def grant_opening_balance(
    supabase: AsyncClient,
    account_id: uuid.UUID,
    amount_minor: int,
    currency: str,
    idempotency_key: str,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> dict:
    """The one deliberate exception to post_transaction() being the only
    money-writer: a new account's opening balance doesn't come from another
    account, it comes "from the bank" - there is no funding/deposit concept
    anywhere else in this app (see design_decisions), so a single-sided
    credit is the only way money enters the system at all. Same
    locking/idempotency pattern as post_transaction, via its own RPC
    function (see backend/supabase/migrations/0004_...sql)."""
    params = {
        "p_account_id": str(account_id),
        "p_amount_minor": amount_minor,
        "p_currency": currency.upper(),
        "p_idempotency_key": idempotency_key,
        "p_actor_user_id": str(actor_user_id) if actor_user_id else None,
    }

    try:
        resp = await supabase.rpc("grant_opening_balance", params).execute()
    except APIError as exc:
        mapped = map_postgrest_error(exc)
        if mapped is not None:
            raise mapped from exc
        raise

    return resp.data


async def grant_interest(
    supabase: AsyncClient,
    account_id: uuid.UUID,
    amount_minor: int,
    currency: str,
    idempotency_key: str,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> dict:
    """The other deliberate exception alongside grant_opening_balance: a
    savings/term-deposit account's interest doesn't come from another
    account either. Same shape, own RPC (backend/supabase/migrations/
    0012_...sql) so the ledger/audit trail can tell interest credits apart
    from the opening-balance grant - see accounts/service.py for when this
    gets called (lazily, on every account read, not on a schedule)."""
    params = {
        "p_account_id": str(account_id),
        "p_amount_minor": amount_minor,
        "p_currency": currency.upper(),
        "p_idempotency_key": idempotency_key,
        "p_actor_user_id": str(actor_user_id) if actor_user_id else None,
    }

    try:
        resp = await supabase.rpc("grant_interest", params).execute()
    except APIError as exc:
        mapped = map_postgrest_error(exc)
        if mapped is not None:
            raise mapped from exc
        raise

    return resp.data


async def post_transaction(
    supabase: AsyncClient,
    legs: Sequence[LedgerLeg],
    idempotency_key: str,
    description: str,
    *,
    reference: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> dict:
    _validate_legs(legs)

    params = {
        "p_idempotency_key": idempotency_key,
        "p_description": description,
        "p_legs": [
            {
                "account_id": str(leg.account_id),
                "direction": leg.direction.value,
                "amount_minor": leg.amount_minor,
                "currency": leg.currency.upper(),
            }
            for leg in legs
        ],
        "p_reference": reference,
        "p_actor_user_id": str(actor_user_id) if actor_user_id else None,
    }

    try:
        resp = await supabase.rpc("post_transaction", params).execute()
    except APIError as exc:
        mapped = map_postgrest_error(exc)
        if mapped is not None:
            raise mapped from exc
        raise

    return resp.data
