"""Payments move money between accounts owned by DIFFERENT users, found by
IBAN - the counterpart to transfers.py's same-user-only movement (see that
module's docstring). create_payment calls the `create_payment` Postgres RPC
(backend/supabase/migrations/0005_...sql), which wraps post_transaction()
the same way create_transfer does. A successful payment also upserts a
beneficiary/contact for the sender (see beneficiaries/service.py) - not
atomic with the money movement (a separate REST call), same accepted
limitation as audit_log writes post-Supabase-migration.
"""

import uuid

from postgrest.exceptions import APIError
from supabase import AsyncClient

from app.core.exceptions import (
    CurrencyMismatchError,
    IbanNotFoundError,
    IdempotencyKeyConflictError,
    SubscriptionPriceIncreaseError,
    ValidationError,
)
from app.db.supabase_client import map_postgrest_error
from app.modules.accounts import service as accounts_service
from app.modules.auth.validation import validate_iban
from app.modules.beneficiaries import service as beneficiaries_service
from app.modules.face_auth import service as face_auth_service
from app.modules.notifications import service as notifications_service
from app.modules.payments.known_subscriptions import match_known_subscription_business
from app.modules.payments.schemas import PaymentCreate
from app.modules.users.schemas import UserRead


async def is_first_payment_to_person(
    supabase: AsyncClient, from_user_id: uuid.UUID, to_user_id: str
) -> bool:
    """"Per person", not per IBAN: someone can own several accounts/IBANs, so
    this checks every account the recipient owns against the sender's saved
    beneficiaries, not just the one IBAN being paid right now. Relies on
    beneficiaries actually being saved (see PaymentCreate.save_beneficiary) -
    a payment sent with that unset never registers as "known", so the next
    one to the same person is treated as new again."""
    if str(from_user_id) == to_user_id:
        return False

    recipient_accounts_resp = (
        await supabase.table("accounts").select("iban").eq("user_id", to_user_id).execute()
    )
    recipient_ibans = [row["iban"] for row in recipient_accounts_resp.data if row["iban"]]
    if not recipient_ibans:
        return True

    resp = (
        await supabase.table("beneficiaries")
        .select("id")
        .eq("user_id", str(from_user_id))
        .in_("iban", recipient_ibans)
        .limit(1)
        .execute()
    )
    return not resp.data


async def _detect_subscription_price_increase(
    supabase: AsyncClient,
    user_id: uuid.UUID,
    from_account_id: uuid.UUID | str,
    to_iban: str,
    new_amount_minor: int,
    beneficiary_name: str,
) -> dict | None:
    """A "this subscription raised its price" signal: the recipient counts
    as a subscription, this exact account has paid it at least twice before
    (a recurring pattern - same 2-occurrence threshold
    app/ai/tools/insights/detect_recurring_payments.py uses to call
    something "recurring"), and the new amount is higher than the most
    recent of those. Returns None when any of that isn't true.

    "Counts as a subscription" is either/or:
    - the recipient IBAN is saved as a contact explicitly marked
      is_subscription (see beneficiaries/service.py) - a friend paid a
      recurring amount twice is NOT a subscription just because the pattern
      looks similar; only a contact the user deliberately classified this
      way triggers on the flag alone; OR
    - the beneficiary_name the sender typed on THIS payment matches a
      hardcoded list of well-known subscription businesses (see
      known_subscriptions.py) - added because in practice almost nothing
      sets the flag above: it only exists on the standalone "add
      beneficiary" form, so a payment made from the Payments form or by the
      AI agent never qualified on the flag alone.

    Deliberately only checks THIS sender account, not every account the user
    owns - one account per subscription is the common real-world case, and
    checking all of them would need a cross-account join for a demo-grade
    heuristic. See design_decisions for the general "good enough" bar."""
    beneficiary = await beneficiaries_service.get_beneficiary_by_iban(supabase, user_id, to_iban)
    known_website = match_known_subscription_business(beneficiary_name)
    is_subscription = (beneficiary is not None and beneficiary["is_subscription"]) or (
        known_website is not None
    )
    if not is_subscription:
        return None

    resp = (
        await supabase.table("payments")
        .select("amount_minor")
        .eq("from_account_id", str(from_account_id))
        .eq("to_iban", to_iban)
        .eq("status", "completed")
        .order("created_at", desc=True)
        .limit(2)
        .execute()
    )
    history = resp.data
    if len(history) < 2:
        return None
    previous_amount_minor = history[0]["amount_minor"]
    if new_amount_minor <= previous_amount_minor:
        return None
    # A real saved website always wins over the hardcoded default.
    website = (beneficiary["website"] if beneficiary else None) or known_website
    return {
        "previous_amount_minor": previous_amount_minor,
        "new_amount_minor": new_amount_minor,
        "website": website,
    }


async def _find_by_idempotency_key(supabase: AsyncClient, idempotency_key: str) -> dict | None:
    resp = (
        await supabase.table("payments")
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


async def _get_account_by_iban(supabase: AsyncClient, iban: str) -> dict:
    resp = await supabase.table("accounts").select("*").eq("iban", iban).maybe_single().execute()
    account = resp.data if resp is not None else None
    if account is None:
        raise IbanNotFoundError()
    return account


async def create_payment(
    supabase: AsyncClient,
    user: UserRead,
    payload: PaymentCreate,
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

    to_iban = payload.to_iban.replace(" ", "").upper()
    if not validate_iban(to_iban):
        raise ValidationError("Invalid IBAN.")

    from_account = await accounts_service.get_account(supabase, user, payload.from_account_id)
    accounts_service.assert_not_locked_for_debit(from_account)
    to_account = await _get_account_by_iban(supabase, to_iban)

    if to_account["id"] == str(from_account["id"]):
        raise ValidationError("Cannot send a payment to the same account.")

    currency = from_account["currency"]
    if to_account["currency"] != currency:
        raise CurrencyMismatchError(
            "Payments can't cross currencies - sender and recipient accounts must "
            "share one."
        )

    if not payload.confirm_price_increase:
        price_increase = await _detect_subscription_price_increase(
            supabase,
            user.id,
            from_account["id"],
            to_iban,
            payload.amount_minor,
            payload.beneficiary_name,
        )
        if price_increase is not None:
            raise SubscriptionPriceIncreaseError(
                details={
                    **price_increase,
                    "currency": currency,
                    "beneficiary_name": payload.beneficiary_name,
                }
            )

    is_new_person = await is_first_payment_to_person(supabase, user.id, to_account["user_id"])
    # proposal_pre_authorized=True: identity already verified by the proposal
    # confirmation flow (face token or password). Only set by proposals_service.
    # Existing callers (payments/router.py) never set this flag.
    if not proposal_pre_authorized:
        await face_auth_service.enforce_face_confirmation(
            supabase,
            user,
            required=face_auth_service.requires_face_confirmation(payload.amount_minor)
            or is_new_person,
            token=face_token,
            password=password,
        )

    try:
        resp = await supabase.rpc(
            "create_payment",
            {
                "p_from_account_id": str(from_account["id"]),
                "p_to_account_id": str(to_account["id"]),
                "p_to_iban": to_iban,
                "p_amount_minor": payload.amount_minor,
                "p_currency": currency,
                "p_description": payload.description
                or f"Plată către {payload.beneficiary_name}",
                "p_idempotency_key": idempotency_key,
                "p_actor_user_id": str(user.id),
            },
        ).execute()
    except APIError as exc:
        mapped = map_postgrest_error(exc)
        if mapped is not None:
            raise mapped from exc
        raise

    payment = resp.data

    # Not atomic with the money movement, same accepted limitation as the
    # beneficiary upsert below - a failure here never blocks the payment
    # itself, it just means the recipient finds out from their statement
    # instead of the bell icon.
    await notifications_service.create_notification(
        supabase,
        to_account["user_id"],
        title="Ai primit bani",
        body=(
            f"Ai primit {payload.amount_minor / 100:.2f} {currency} de la "
            f"{user.first_name} {user.last_name} în contul \"{to_account['name']}\"."
        ),
        category="money_received",
    )

    if payload.save_beneficiary:
        await beneficiaries_service.upsert_beneficiary(
            supabase, user_id=user.id, iban=to_iban, display_name=payload.beneficiary_name
        )

    return payment


async def list_payments(supabase: AsyncClient, user: UserRead) -> list[dict]:
    # Safe two-call fallback instead of relying on PostgREST's embedded-
    # filter syntax (unstable across versions) - not a hot/concurrent path.
    accounts_resp = (
        await supabase.table("accounts").select("id").eq("user_id", str(user.id)).execute()
    )
    account_ids = [row["id"] for row in accounts_resp.data]
    if not account_ids:
        return []

    resp = (
        await supabase.table("payments")
        .select("*")
        .in_("from_account_id", account_ids)
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data
