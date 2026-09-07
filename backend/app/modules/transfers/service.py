"""Transfers are strictly between two accounts owned by the SAME user (e.g.
Checking -> Savings). Sending money to someone else's account is the
beneficiaries/payments flow (a separate module) - the spec lists "Transfer
money between accounts" and "Send money to saved payees" as two distinct
capabilities, and this module only implements the former.

create_transfer calls the `create_transfer` Postgres RPC directly (see
backend/supabase/migrations/0002_ledger_functions.sql) rather than calling
ledger.post_transaction() and then inserting a `transfers` row as two
separate steps - that composition needs to be atomic, and the RPC already
does both inside one transaction.
"""

import uuid
from datetime import date
from decimal import Decimal

from postgrest.exceptions import APIError

from app.core.exceptions import (
    CurrencyMismatchError,
    IdempotencyKeyConflictError,
    NotFoundError,
    ValidationError,
)
from app.db.supabase_client import map_postgrest_error
from app.modules.accounts import service as accounts_service
from app.modules.face_auth import service as face_auth_service
from app.modules.notifications import service as notifications_service
from app.modules.transfers.schemas import TransferCreate
from app.modules.users.schemas import UserRead
from supabase import AsyncClient


async def _find_by_idempotency_key(supabase: AsyncClient, idempotency_key: str) -> dict | None:
    resp = (
        await supabase.table("transfers")
        .select("*")
        .eq("idempotency_key", idempotency_key)
        .maybe_single()
        .execute()
    )
    return resp.data if resp is not None else None


async def _is_owned_by(supabase: AsyncClient, account_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    resp = (
        await supabase.table("accounts")
        .select("id")
        .eq("id", str(account_id))
        .eq("user_id", str(user_id))
        .maybe_single()
        .execute()
    )
    return resp is not None and resp.data is not None


async def create_transfer(
    supabase: AsyncClient,
    user: UserRead,
    payload: TransferCreate,
    idempotency_key: str,
    face_token: str | None = None,
    proposal_pre_authorized: bool = False,
    *,
    password: str | None = None,
) -> dict:
    existing = await _find_by_idempotency_key(supabase, idempotency_key)
    if existing is not None:
        if not await _is_owned_by(supabase, uuid.UUID(existing["from_account_id"]), user.id):
            raise IdempotencyKeyConflictError()
        return existing

    if payload.from_account_id == payload.to_account_id:
        raise ValidationError("from_account_id and to_account_id must differ.")

    from_account = await accounts_service.get_account(supabase, user, payload.from_account_id)
    to_account = await accounts_service.get_account(supabase, user, payload.to_account_id)
    accounts_service.assert_not_locked_for_debit(from_account)

    currency = payload.currency.upper()
    if from_account["currency"] != currency or to_account["currency"] != currency:
        raise CurrencyMismatchError("Transfer currency must match both accounts' currency.")

    # proposal_pre_authorized=True: identity already verified by the proposal
    # confirmation flow (face token or password). Only set by proposals_service.
    # Existing callers (transfers/router.py) never set this flag.
    if not proposal_pre_authorized:
        await face_auth_service.enforce_face_confirmation(
            supabase,
            user,
            required=face_auth_service.requires_face_confirmation(payload.amount_minor),
            token=face_token,
            password=password,
        )

    try:
        resp = await supabase.rpc(
            "create_transfer",
            {
                "p_from_account_id": str(from_account["id"]),
                "p_to_account_id": str(to_account["id"]),
                "p_amount_minor": payload.amount_minor,
                "p_currency": currency,
                "p_description": payload.description
                or f"Transfer: {from_account['name']} → {to_account['name']}",
                "p_idempotency_key": idempotency_key,
                "p_actor_user_id": str(user.id),
            },
        ).execute()
    except APIError as exc:
        mapped = map_postgrest_error(exc)
        if mapped is not None:
            raise mapped from exc
        raise

    # Same category payments/service.py uses for "you got paid" - a
    # transfer moves money into an account just as much as a payment does,
    # so it gets the same real-time pop-up/animation on the frontend (see
    # notifications/bus.py). Always this user's own notification (transfers
    # are strictly own-account-to-own-account - see this module's docstring),
    # not a cross-user event like the payments one.
    await notifications_service.create_notification(
        supabase,
        user.id,
        title="Ai primit bani",
        body=(
            f"{payload.amount_minor / 100:.2f} {currency} au fost transferați din "
            f"\"{from_account['name']}\" în \"{to_account['name']}\"."
        ),
        category="money_received",
    )

    return resp.data


async def create_fx_transfer(
    supabase: AsyncClient,
    user: UserRead,
    *,
    from_account_id: uuid.UUID,
    to_account_id: uuid.UUID,
    from_amount_minor: int,
    to_amount_minor: int,
    exchange_rate: Decimal,
    exchange_rate_date: date,
    description: str | None,
    idempotency_key: str,
    proposal_pre_authorized: bool = False,
) -> dict:
    """A transfer between two accounts in DIFFERENT currencies.

    A sibling of `create_transfer` above rather than a widening of it: an
    ordinary transfer keeps its exact existing code path, and neither
    function's reader has to hold the other's case in their head. Both end up
    in `post_transaction`, which remains the only thing that writes
    ledger_entries.

    The mechanics live in the `create_fx_transfer` RPC (0024_fx_desk.sql):
    two balanced single-currency journals through the bank's FX desk, in one
    transaction. Nothing about the ledger's invariants is relaxed to make
    this work - see that file's header.

    THE RATE IS PASSED IN, NOT LOOKED UP. It was locked when the proposal was
    built and shown to the user; re-fetching here would mean executing a
    number they never saw. BNR publishes once a business day so the two would
    almost always agree - "almost always" is not the standard for money.

    Currencies are read from the two accounts rather than taken as arguments,
    for the same reason `propose_transfer` stopped taking one: there is
    exactly one correct value per account and it is not the caller's to
    state.
    """
    existing = await _find_by_idempotency_key(supabase, idempotency_key)
    if existing is not None:
        if not await _is_owned_by(supabase, uuid.UUID(existing["from_account_id"]), user.id):
            raise IdempotencyKeyConflictError()
        return existing

    if from_account_id == to_account_id:
        raise ValidationError("from_account_id and to_account_id must differ.")

    from_account = await accounts_service.get_account(supabase, user, from_account_id)
    to_account = await accounts_service.get_account(supabase, user, to_account_id)
    accounts_service.assert_not_locked_for_debit(from_account)

    if from_account["currency"] == to_account["currency"]:
        # Not an FX transfer. Refused rather than quietly forwarded, so the
        # ordinary path cannot drift into this one unnoticed.
        raise ValidationError("Same-currency transfers must use create_transfer.")

    # Step-up auth is judged on what LEAVES the user's account, which is the
    # source amount in the source currency - the same figure `create_transfer`
    # passes, and the same one the user was asked to confirm.
    #
    # `token=None`: every caller today arrives through the proposal flow,
    # which has already proved identity and passes
    # proposal_pre_authorized=True, so this branch is unreachable in
    # practice. It is kept rather than asserted away so that adding a direct
    # REST route for FX transfers later cannot accidentally skip step-up.
    if not proposal_pre_authorized:
        await face_auth_service.enforce_face_confirmation(
            supabase,
            user,
            required=face_auth_service.requires_face_confirmation(from_amount_minor),
            token=None,
        )

    try:
        resp = await supabase.rpc(
            "create_fx_transfer",
            {
                "p_from_account_id": str(from_account["id"]),
                "p_to_account_id": str(to_account["id"]),
                "p_from_amount_minor": from_amount_minor,
                "p_from_currency": from_account["currency"],
                "p_to_amount_minor": to_amount_minor,
                "p_to_currency": to_account["currency"],
                "p_exchange_rate": str(exchange_rate),
                "p_exchange_rate_date": exchange_rate_date.isoformat(),
                "p_description": description
                or f"Schimb valutar: {from_account['name']} → {to_account['name']}",
                "p_idempotency_key": idempotency_key,
                "p_actor_user_id": str(user.id),
            },
        ).execute()
    except APIError as exc:
        mapped = map_postgrest_error(exc)
        if mapped is not None:
            raise mapped from exc
        raise

    return resp.data


async def get_transfer(supabase: AsyncClient, user: UserRead, transfer_id: uuid.UUID) -> dict:
    resp = (
        await supabase.table("transfers")
        .select("*")
        .eq("id", str(transfer_id))
        .maybe_single()
        .execute()
    )
    transfer = resp.data if resp is not None else None
    if transfer is None or not await _is_owned_by(
        supabase, uuid.UUID(transfer["from_account_id"]), user.id
    ):
        raise NotFoundError("Transfer not found.")
    return transfer


async def list_transfers_for_owner(
    supabase: AsyncClient, user_id: uuid.UUID | str, *, limit: int | None = None
) -> list[dict]:
    """Transfer list for callers holding a bare user id (the AI layer's
    `Context`), mirroring `accounts_service.get_account_for_owner`.

    `limit` is optional so `list_transfers` below - the banking modules' entry
    point - keeps returning the full history unchanged.
    """
    # Safe two-call fallback instead of relying on PostgREST's embedded-
    # filter syntax (unstable across versions) - not a hot/concurrent path.
    accounts_resp = (
        await supabase.table("accounts").select("id").eq("user_id", str(user_id)).execute()
    )
    account_ids = [row["id"] for row in accounts_resp.data]
    if not account_ids:
        return []

    query = (
        supabase.table("transfers")
        .select("*")
        .in_("from_account_id", account_ids)
        .order("created_at", desc=True)
    )
    if limit is not None:
        query = query.limit(limit)

    resp = await query.execute()
    return resp.data


async def list_transfers(supabase: AsyncClient, user: UserRead) -> list[dict]:
    return await list_transfers_for_owner(supabase, user.id)
