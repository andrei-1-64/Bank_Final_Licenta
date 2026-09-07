"""AI-proposed write actions: create, confirm (with step-up auth), reject.

The AI layer never executes a write directly (see app/ai/tools/propose_tools.py
and the `read_only` flag on app/ai/tools/base.py::Tool) - a propose_* tool only
ever inserts a `pending` row here. Only `confirm_proposal`, after verifying the
caller's step-up credential SERVER-SIDE, calls the real service function
(create_transfer, create_payment, open_account, close_account, cancel_card,
sign_document).

THE RULE, same as everywhere else identity/auth is involved: never trust the
frontend's "the user proved who they are" - the password is checked against
the bcrypt hash here, and the face token was only ever issued by a prior
server-side 1:1 face match (see face_auth_service.create_face_confirmation).
`credential` (whichever kind it is) must never be logged, and must never end
up in the `proposals` row - it is used once, in memory, and discarded.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.exceptions import (
    CurrencyMismatchError,
    FaceAuthMethodRequiredError,
    InvalidFaceConfirmationError,
    NotFoundError,
    ProposalExpiredError,
    ProposalNotPendingError,
    ProposalRateLimitedError,
    UnauthorizedError,
    ValidationError,
)
from app.core.security import verify_password
from app.modules.accounts.service import close_account, get_account, open_account
from app.modules.auth import service as auth_service
from app.modules.cards.service import cancel_card
from app.modules.face_auth import service as face_auth_service
from app.modules.payments.schemas import PaymentCreate
from app.modules.payments.service import create_payment, is_first_payment_to_person
from app.modules.transfers.schemas import TransferCreate
from app.modules.transfers.service import create_fx_transfer, create_transfer
from app.modules.users.schemas import UserRead
from supabase import AsyncClient

#: A pending proposal past this age is treated as expired at the next
#: confirm/reject attempt (lazy expiry - no background job, same pattern as
#: auth/service.py's password-reset codes).
PROPOSAL_EXPIRY_MINUTES = 10

#: Confirm-attempt rate limiting (Step 16, item 5). Same threshold and
#: window as auth/service.py's login lockout, deliberately - "matching
#: lockout duration to the existing login lockout" was the brief, and there
#: is no reason for a different number here.
CONFIRM_MAX_FAILED_ATTEMPTS = auth_service.LOGIN_MAX_FAILED_ATTEMPTS
CONFIRM_LOCKOUT_WINDOW_MINUTES = auth_service.LOGIN_LOCKOUT_WINDOW_MINUTES

#: A face-required proposal (large transfer, first payment to someone new)
#: still can't be confirmed with a password out of the gate - see
#: _proposal_requires_face below - but stops refusing it once this many
#: failed attempts are already on record for THIS proposal, exactly
#: mirroring face_auth/service.py::enforce_face_confirmation's own password
#: fallback for the direct (non-AI) path. Reuses the same counter
#: CONFIRM_MAX_FAILED_ATTEMPTS rate-limits with below - the only way to
#: accumulate failures under this key before password is allowed at all is
#: by failing face, so this number is really "how many failed face
#: attempts", even though the counter itself doesn't distinguish which
#: method failed.
FACE_FAILURES_BEFORE_PASSWORD_FALLBACK = 3


def _confirm_attempt_key(user_id: str, proposal_id: str) -> str:
    """Reuses `login_attempts.email` (VARCHAR(320), no format constraint) as
    a namespaced key instead of a new table - "do not introduce a new
    storage backend" ruled out a sibling table too, since that would need a
    migration. The "confirm:" prefix keeps a rate-limit row unmistakable
    from a real login email at a glance, and nothing in this codebase ever
    queries `login_attempts` for a bare, unprefixed email match against
    THIS key shape, so the two uses cannot collide."""
    return f"confirm:{user_id}:{proposal_id}"


async def _count_recent_failed_confirm_attempts(
    supabase: AsyncClient, user_id: str, proposal_id: str
) -> int:
    window_start = datetime.now(UTC) - timedelta(minutes=CONFIRM_LOCKOUT_WINDOW_MINUTES)
    resp = (
        await supabase.table("login_attempts")
        .select("id", count="exact")
        .eq("email", _confirm_attempt_key(user_id, proposal_id))
        .eq("success", False)
        .gte("created_at", window_start.isoformat())
        .execute()
    )
    return resp.count or 0


async def _record_failed_confirm_attempt(
    supabase: AsyncClient, user_id: str, proposal_id: str
) -> None:
    await (
        supabase.table("login_attempts")
        .insert({"email": _confirm_attempt_key(user_id, proposal_id), "success": False})
        .execute()
    )


async def _clear_confirm_attempts(supabase: AsyncClient, user_id: str, proposal_id: str) -> None:
    """A successful confirm resets the count. Unlike login (whose window just
    slides - there is no single "session" a success could close), a
    proposal is single-use: once confirmed it can never be confirmed again
    (see the status gate above), so a stale failure count sitting under this
    key afterwards protects nothing and would only need to age out on its
    own instead."""
    await (
        supabase.table("login_attempts")
        .delete()
        .eq("email", _confirm_attempt_key(user_id, proposal_id))
        .execute()
    )


async def create_proposal(
    supabase: AsyncClient,
    *,
    user_id: str,
    conversation_id: str,
    proposal_type: str,
    payload: dict[str, Any],
    summary: str,
    conversion: dict[str, Any] | None = None,
) -> dict:
    """`conversion` carries the six columns added by
    0023_proposal_currency_conversion.sql, and ONLY a cross-currency transfer
    passes it. When it is None the insert below is byte-identical to the one
    this function has always sent - so every existing proposal type, and every
    same-currency transfer, is unaffected and keeps working whether or not
    0023 has been applied yet.
    """
    # Supersede any other still-pending proposal in THIS conversation first.
    # A new propose_* call almost always means the user changed their mind
    # about a prior one in the same conversation ("de fapt, trimite 500 RON"
    # after a 50 RON proposal) - leaving the old one confirmable is a real
    # risk (a stray click, or the model re-surfacing it), not just stale UI.
    # Scoped to the conversation (not proposal_type), same status either way
    # would be misleading to leave "pending" once the user has moved on.
    await (
        supabase.table("proposals")
        .update({"status": "rejected", "rejected_at": datetime.now(UTC).isoformat()})
        .eq("conversation_id", conversation_id)
        .eq("status", "pending")
        .execute()
    )

    row: dict[str, Any] = {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "proposal_type": proposal_type,
        "payload": payload,
        "summary": summary,
    }
    if conversion is not None:
        row.update(conversion)

    resp = await supabase.table("proposals").insert(row).execute()
    return resp.data[0]


async def get_proposal(supabase: AsyncClient, user: UserRead, proposal_id: str) -> dict:
    """Ownership-checked read. A missing id and one owned by someone else look
    identical to the caller - NotFoundError either way, never 403, so a
    non-owner can't confirm a proposal exists by the error shape alone."""
    resp = (
        await supabase.table("proposals").select("*").eq("id", proposal_id).maybe_single().execute()
    )
    proposal = resp.data if resp is not None else None
    if proposal is None or proposal["user_id"] != str(user.id):
        raise NotFoundError("Proposal not found.")
    return proposal


async def ensure_pending_and_not_expired(supabase: AsyncClient, proposal: dict) -> None:
    """The status + expiry gate, shared by every way a proposal can be
    confirmed - the ordinary single-credential path below, and esign's
    OTP+Face path for admin-issued documents (confirm_admin_document).

    Status is checked BEFORE expiry so an already-decided proposal reports
    its real state rather than "expired" if it happens to also be old."""
    if proposal["status"] != "pending":
        raise ProposalNotPendingError()

    created_at = datetime.fromisoformat(proposal["created_at"])
    if datetime.now(UTC) - created_at > timedelta(minutes=PROPOSAL_EXPIRY_MINUTES):
        await (
            supabase.table("proposals")
            .update({"status": "expired", "rejected_at": datetime.now(UTC).isoformat()})
            .eq("id", proposal["id"])
            .execute()
        )
        raise ProposalExpiredError()


async def mark_confirmed(supabase: AsyncClient, proposal: dict, result: dict) -> dict:
    """The final state transition, shared for the same reason as
    ensure_pending_and_not_expired above - only ever called after whichever
    step-up check applies has already succeeded."""
    updated = (
        await supabase.table("proposals")
        .update(
            {
                "status": "confirmed",
                "confirmed_at": datetime.now(UTC).isoformat(),
                "result": result,
            }
        )
        .eq("id", proposal["id"])
        .execute()
    )
    return updated.data[0]


async def _proposal_requires_face(supabase: AsyncClient, user: UserRead, proposal: dict) -> bool:
    """Whether THIS transfer/payment proposal would have required Face ID
    specifically (not password) on the direct, non-AI path - see
    face_auth_service.enforce_face_confirmation, which create_transfer/
    create_payment skip entirely when called with proposal_pre_authorized=
    True (see _execute below). Without this check, confirm_proposal's own
    Face-OR-password gate would let a large transfer or a first payment to
    a new recipient go through on a password alone, silently defeating the
    mandatory-Face-ID policy for exactly the AI-chat path most people would
    actually use it from.

    Recomputed from the proposal's own payload (the real amount/recipient,
    already resolved and stored when the proposal was created - see
    propose_tools.py), not from anything the caller supplies now."""
    payload = proposal["payload"]
    proposal_type = proposal["proposal_type"]

    if proposal_type not in ("transfer", "payment"):
        return False
    if face_auth_service.requires_face_confirmation(payload["amount_minor"]):
        return True
    if proposal_type != "payment":
        return False

    # Mirrors payments/service.py::create_payment's own to_account lookup -
    # is_first_payment_to_person needs the recipient's user_id, which the
    # proposal payload only carries as an IBAN.
    resp = (
        await supabase.table("accounts")
        .select("user_id")
        .eq("iban", payload["to_iban"])
        .maybe_single()
        .execute()
    )
    to_account = resp.data if resp is not None else None
    if to_account is None:
        # An invalid/vanished IBAN is a real failure, but not THIS function's
        # to raise - _execute's own create_payment call will hit the same
        # lookup and surface IbanNotFoundError properly.
        return False
    return await is_first_payment_to_person(supabase, user.id, to_account["user_id"])


async def confirm_proposal(
    supabase: AsyncClient,
    user: UserRead,
    proposal_id: str,
    auth_method: str,
    credential: str,
) -> dict:
    proposal = await get_proposal(supabase, user, proposal_id)
    await ensure_pending_and_not_expired(supabase, proposal)

    # BEFORE the credential is looked at: a proposal that cannot possibly
    # execute should not cost the user a Face ID scan first. See
    # `_assert_still_executable`.
    await _assert_still_executable(supabase, user, proposal)

    if auth_method == "password" and await _proposal_requires_face(supabase, user, proposal):
        failed_so_far = await _count_recent_failed_confirm_attempts(
            supabase, str(user.id), proposal["id"]
        )
        if failed_so_far < FACE_FAILURES_BEFORE_PASSWORD_FALLBACK:
            raise FaceAuthMethodRequiredError()

    # THE critical security gate. Only reached with a still-pending, not-yet-
    # expired proposal; only past this point does anything real execute.
    #
    # Rate-limited BEFORE the credential is looked at, same as login - the
    # 10-minute proposal expiry above bounds the window on its own, but
    # without this a single pending proposal could still absorb hundreds of
    # wrong-password guesses per second within it.
    if (
        await _count_recent_failed_confirm_attempts(supabase, str(user.id), proposal["id"])
        >= CONFIRM_MAX_FAILED_ATTEMPTS
    ):
        raise ProposalRateLimitedError()

    if auth_method == "face":
        if not await face_auth_service.has_face_enrolled(supabase, user):
            raise ValidationError("Autentificarea facială nu este activată pe acest cont.")
        try:
            await face_auth_service.consume_face_confirmation_token(supabase, user, credential)
        except InvalidFaceConfirmationError:
            await _record_failed_confirm_attempt(supabase, str(user.id), proposal["id"])
            raise
    elif auth_method == "password":
        resp = (
            await supabase.table("users")
            .select("password_hash")
            .eq("id", str(user.id))
            .maybe_single()
            .execute()
        )
        password_hash = resp.data["password_hash"] if resp is not None and resp.data else None
        if password_hash is None or not verify_password(credential, password_hash):
            await _record_failed_confirm_attempt(supabase, str(user.id), proposal["id"])
            raise UnauthorizedError("Parolă incorectă.")
    else:
        raise ValidationError("Metodă de autentificare necunoscută.")

    await _clear_confirm_attempts(supabase, str(user.id), proposal["id"])
    result = await _execute(supabase, user, proposal, auth_method)
    return await mark_confirmed(supabase, proposal, result)


async def reject_proposal(supabase: AsyncClient, user: UserRead, proposal_id: str) -> dict:
    proposal = await get_proposal(supabase, user, proposal_id)
    if proposal["status"] != "pending":
        raise ProposalNotPendingError()

    updated = (
        await supabase.table("proposals")
        .update({"status": "rejected", "rejected_at": datetime.now(UTC).isoformat()})
        .eq("id", proposal["id"])
        .execute()
    )
    return updated.data[0]


def _locked_conversion(proposal: dict) -> dict[str, Any] | None:
    """The FX numbers locked onto this proposal when it was created, or None.

    None for every same-currency transfer, for every other proposal type, and
    for any row predating 0023_proposal_currency_conversion.sql - `.get`
    rather than `[...]` so a proposal read back before that migration is
    applied behaves exactly as it did before, instead of raising KeyError.

    `converted_amount_minor` is the single flag: the migration's
    `proposals_currency_conversion_complete` CHECK guarantees the other five
    are present whenever it is. The values are re-read here, never
    recomputed - see `_execute`.
    """
    amount = proposal.get("converted_amount_minor")
    if amount is None:
        return None
    return {
        "original_currency": proposal["original_currency"],
        "original_amount_minor": int(proposal["original_amount_minor"]),
        "converted_currency": proposal["converted_currency"],
        "converted_amount_minor": int(amount),
        # NUMERIC arrives from PostgREST as a string (or, on some driver
        # versions, a float). Decimal(str(...)) is exact for the former and
        # the closest available reading of the latter; never Decimal(float).
        "exchange_rate": Decimal(str(proposal["exchange_rate"])),
        "exchange_rate_date": date.fromisoformat(str(proposal["exchange_rate_date"])),
    }


async def _assert_still_executable(
    supabase: AsyncClient, user: UserRead, proposal: dict
) -> None:
    """Re-check a pending proposal against the accounts as they are NOW.

    A proposal's payload is a snapshot taken when the AI built it, and
    `_execute` below hands that snapshot to the real service functions. Those
    functions validate - which is right - but they validate at EXECUTION
    time, i.e. after the user has read the proposal, tapped confirm and
    proved their identity. A snapshot that fails that check surfaced as a raw
    English "Transfer currency must match both accounts' currency." inside a
    Romanian confirmation dialog, at the worst possible moment, with nothing
    the user could do about it.

    A currency MISMATCH between the two accounts is no longer a reason to
    refuse - `propose_transfer` now converts at the BNR rate and locks the
    result onto the proposal. What this function checks is that those locked
    numbers still describe THESE two accounts. If they do not, the figure the
    user read and approved is not the figure that would move, and no reading
    of "confirm" covers that.

    Nothing here weakens a check downstream: every service function still
    validates its own inputs exactly as before. This only moves the FIRST
    honest "no" earlier, and says it in Romanian.

    Only `transfer` is covered - it is the one whose payload carries values
    that can contradict the accounts it names. Payments take their currency
    from the source account at execution, and the remaining types name no
    second account to disagree with.
    """
    if proposal["proposal_type"] != "transfer":
        return

    payload = proposal["payload"]
    try:
        from_id = uuid.UUID(str(payload["from_account_id"]))
        to_id = uuid.UUID(str(payload["to_account_id"]))
    except (KeyError, ValueError):
        # Not something this check can reason about. Leave it to `_execute`
        # and the service functions, which reject it exactly as they did
        # before this function existed - an early check must not become a new
        # way for a request to fail.
        return

    from_account = await get_account(supabase, user, from_id)
    to_account = await get_account(supabase, user, to_id)
    conversion = _locked_conversion(proposal)

    if from_account["currency"] != to_account["currency"]:
        if conversion is None:
            # A proposal built before cross-currency transfers were supported,
            # still pending. There is no locked rate to execute against, and
            # inventing one now would execute a number the user never saw.
            raise CurrencyMismatchError(
                f"Această propunere a fost pregătită înainte ca transferurile "
                f"valutare să fie posibile, așa că nu conține un curs de "
                f"schimb. Respinge-o și cere-mi din nou transferul din "
                f"{from_account['name']} în {to_account['name']} - îl voi "
                "recalcula la cursul BNR de azi."
            )
        if (
            conversion["original_currency"] != from_account["currency"]
            or conversion["converted_currency"] != to_account["currency"]
        ):
            raise CurrencyMismatchError(
                f"Conturile s-au schimbat de când am pregătit propunerea: "
                f"conversia a fost calculată din {conversion['original_currency']} "
                f"în {conversion['converted_currency']}, dar acum "
                f"{from_account['name']} este în {from_account['currency']}, iar "
                f"{to_account['name']} este în {to_account['currency']}. "
                "Respinge propunerea și cere-mi transferul din nou."
            )
        # The locked numbers still describe these accounts. The payload's
        # advisory `currency` is not consulted: for an FX transfer the two
        # authoritative currencies are the ones above.
        return

    if conversion is not None:
        # Both accounts read the same currency now, but the proposal carries a
        # conversion - so one of them changed after it was built. The
        # converted figure the user approved no longer means anything here.
        raise CurrencyMismatchError(
            f"Am pregătit această propunere ca schimb valutar din "
            f"{conversion['original_currency']} în {conversion['converted_currency']}, "
            f"dar ambele conturi sunt acum în {from_account['currency']}. "
            "Respinge propunerea și cere-mi transferul din nou."
        )

    # The accounts agree with each other but not with what the proposal says.
    # This is a proposal built before propose_transfer started reading the
    # currency off the account (it used to take the model's word for it), so
    # the amount shown to the user was labelled with the wrong currency. It
    # must NOT be quietly executed in the right one: 500 EUR is not the 500
    # RON they read and approved.
    stated_currency = payload.get("currency")
    if stated_currency is not None and stated_currency != from_account["currency"]:
        raise CurrencyMismatchError(
            f"Această propunere a fost pregătită în {stated_currency}, dar "
            f"{from_account['name']} este în {from_account['currency']}. Suma "
            "afișată nu corespunde monedei contului, așa că nu o pot executa. "
            "Respinge propunerea și cere-mi transferul din nou."
        )


async def _execute(
    supabase: AsyncClient, user: UserRead, proposal: dict, auth_method: str
) -> dict:
    """Dispatch on proposal_type. Each branch calls the REAL service function,
    with `proposal_pre_authorized=True` where that parameter exists - the
    step-up check above already stood in for the amount/new-recipient face
    check those functions would otherwise perform themselves.

    The proposal's own id is the idempotency key: a proposal is confirmed at
    most once (the status gate in confirm_proposal enforces that), so it is
    already a natural, stable, unique key for the underlying write - no
    separate key generation needed.

    `auth_method` is only used by "sign_document" - it becomes part of the
    signature's canonical payload (which credential kind proved identity for
    THIS signature is part of what the signature attests to). No other
    branch needs it: a transfer/payment/etc. doesn't record how the user
    authenticated, only that confirm_proposal already required it."""
    proposal_type = proposal["proposal_type"]
    payload = proposal["payload"]
    idempotency_key = str(proposal["id"])

    if proposal_type == "transfer":
        conversion = _locked_conversion(proposal)
        if conversion is not None:
            # THE LOCKED RATE. Everything below comes off the proposal row as
            # it was written when the user was shown the figures; no BNR call
            # happens on this path. The user confirmed a specific converted
            # amount, and that exact integer is what gets credited.
            return await create_fx_transfer(
                supabase,
                user,
                from_account_id=uuid.UUID(str(payload["from_account_id"])),
                to_account_id=uuid.UUID(str(payload["to_account_id"])),
                from_amount_minor=conversion["original_amount_minor"],
                to_amount_minor=conversion["converted_amount_minor"],
                exchange_rate=conversion["exchange_rate"],
                exchange_rate_date=conversion["exchange_rate_date"],
                description=payload.get("description"),
                idempotency_key=idempotency_key,
                proposal_pre_authorized=True,
            )

        transfer_payload = TransferCreate(
            from_account_id=payload["from_account_id"],
            to_account_id=payload["to_account_id"],
            amount_minor=payload["amount_minor"],
            currency=payload["currency"],
            description=payload.get("description"),
        )
        return await create_transfer(
            supabase, user, transfer_payload, idempotency_key, proposal_pre_authorized=True
        )

    if proposal_type == "payment":
        payment_payload = PaymentCreate(
            from_account_id=payload["from_account_id"],
            to_iban=payload["to_iban"],
            beneficiary_name=payload["beneficiary_name"],
            amount_minor=payload["amount_minor"],
            description=payload.get("description"),
            save_beneficiary=payload.get("save_beneficiary", True),
        )
        return await create_payment(
            supabase, user, payment_payload, idempotency_key, proposal_pre_authorized=True
        )

    if proposal_type == "open_account":
        return await open_account(
            supabase,
            user,
            payload["name"],
            payload["currency"],
            product_type=payload.get("product_type", "checking"),
            term_months=payload.get("term_months"),
        )

    if proposal_type == "close_account":
        return await close_account(supabase, user, uuid.UUID(payload["account_id"]))

    if proposal_type == "cancel_card":
        return await cancel_card(supabase, user, uuid.UUID(payload["card_id"]))

    if proposal_type == "sign_document":
        from app.modules.esign.service import sign_document

        return await sign_document(
            supabase,
            user,
            proposal,
            document_id=payload["document_id"],
            intent=payload["intent"],
            auth_method=auth_method,
            expected_document_sha256=payload["document_sha256"],
        )

    raise ValueError(f"Unknown proposal_type: {proposal_type!r}")
