"""Electronic signatures over uploaded documents.

`sign_document` is called from ONE place: proposals_service._execute, for
proposal_type == "sign_document" - after confirm_proposal has already
verified step-up auth (face token or password). Nothing here re-checks
identity; ownership of the document is the only thing re-verified, via
documents_service.get_document_with_content (NotFoundError for a foreign or
nonexistent id, same as every other ownership check in this codebase).

THE GUARDRAIL: the document is re-hashed HERE, from the live `content`
column, not copied from whatever hash the proposal was created with. If the
document changed between "user asked to sign" and "user completed step-up
auth", the two hashes disagree and this raises - the user never ends up
having cryptographically signed different bytes than the ones they
consented to.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta

from supabase import AsyncClient

from app.core.exceptions import InvalidSigningCodeError, NotFoundError, ValidationError
from app.core.security import generate_otp_code, hash_otp_code
from app.core.teams import send_teams_message
from app.modules.chat import proposals_service
from app.modules.documents import service as documents_service
from app.modules.esign import keys
from app.modules.esign.canonical import build_canonical_payload
from app.modules.face_auth import service as face_auth_service
from app.modules.users.schemas import UserRead

#: Same TTL/attempt-limit shape as auth/service.py's password-reset codes -
#: see 0019_admin_documents.sql's docstring for why this is its own table
#: rather than reusing that one.
SIGNING_CODE_TTL_MINUTES = 10
SIGNING_CODE_MAX_ATTEMPTS = 5

#: Includes canonical_payload for verify_signature's internal use - harmless
#: to select (it's derived from the other columns, not a secret), and
#: SignatureRead simply doesn't declare that field, so it never appears in
#: an API response.
_SIGNATURE_COLUMNS = (
    "id, created_at, document_id, proposal_id, key_id, algorithm, "
    "document_sha256, signed_at, auth_method, intent, signature_b64, "
    "canonical_payload, user_id"
)


async def sign_document(
    supabase: AsyncClient,
    user: UserRead,
    proposal: dict,
    *,
    document_id: str,
    intent: str,
    auth_method: str,
    expected_document_sha256: str,
) -> dict:
    document = await documents_service.get_document_with_content(
        supabase, str(user.id), document_id
    )
    document_sha256 = hashlib.sha256(document["content"]).hexdigest()
    if document_sha256 != expected_document_sha256:
        raise ValidationError(
            "Documentul s-a modificat de la crearea cererii de semnătură - "
            "semnarea a fost anulată."
        )

    await keys.ensure_key_registered(supabase)

    signed_at = datetime.now(UTC)
    payload = build_canonical_payload(
        proposal_id=str(proposal["id"]),
        document_sha256=document_sha256,
        user_id=str(user.id),
        signed_at_iso=signed_at.isoformat(),
        auth_method=auth_method,
        intent=intent,
    )
    signature_bytes = keys.sign(payload)

    inserted = (
        await supabase.table("signatures")
        .insert(
            {
                "proposal_id": proposal["id"],
                "document_id": document_id,
                "user_id": str(user.id),
                "key_id": keys.key_id(),
                "algorithm": "ed25519",
                "document_sha256": document_sha256,
                "signed_at": signed_at.isoformat(),
                "auth_method": auth_method,
                "intent": intent,
                "canonical_payload": payload.decode("utf-8"),
                "signature_b64": base64.b64encode(signature_bytes).decode(),
            }
        )
        .execute()
    )
    return inserted.data[0]


async def create_sign_request(
    supabase: AsyncClient, user: UserRead, document_id: str, intent: str
) -> dict:
    """Creates a `pending` proposal for signing `document_id` - the same
    pending -> step-up-confirm flow as a transfer or payment (see
    app/modules/chat/proposals_service.py), just triggered directly from a
    "Sign this document" action rather than from the AI tool loop.

    ONLY admin-issued documents are signable. A document a user uploaded to
    the chat themselves is reference material for the AI to answer questions
    about - there is no counterparty, nothing was agreed, and a signature
    over it would attest to nothing. Signing is reserved for what the bank
    formally issues TO the user (see admin/service.py::
    generate_and_send_document), which is also why the signature itself goes
    through the stronger OTP+Face path rather than the ordinary
    Face-or-password one (see confirm_admin_document below).

    NOT an AI tool on purpose: DocumentAgent's tool registry is deliberately
    limited to `read_document` alone (see app/ai/tools/document_tools.py's
    module docstring) so that a document's own text - untrusted input - can
    never talk its way into a write. This stays a direct, non-AI action the
    frontend calls from the "Documente de semnat" list, exactly the way a
    real e-sign flow presents a specific document with a specific button
    rather than routing the decision through a chatbot.

    The document's own `sha256` is computed HERE, at request time, and
    carried in the proposal payload - `sign_document` (called only after
    step-up auth succeeds) re-hashes and compares before signing, so a
    document edited between these two moments can never be signed silently.
    """
    document = await documents_service.get_document_with_content(
        supabase, str(user.id), document_id
    )
    if document.get("issued_by_admin_id") is None:
        raise ValidationError(
            "Doar documentele emise de bancă pot fi semnate electronic."
        )
    if document["conversation_id"] is None:
        raise ValidationError(
            "Acest document nu mai are o conversație asociată și nu poate fi semnat."
        )

    document_sha256 = hashlib.sha256(document["content"]).hexdigest()
    summary = f"Semnare electronică a documentului «{document['filename']}»"

    from app.modules.chat.proposals_service import create_proposal

    return await create_proposal(
        supabase,
        user_id=str(user.id),
        conversation_id=document["conversation_id"],
        proposal_type="sign_document",
        payload={
            "document_id": document_id,
            "intent": intent,
            "document_sha256": document_sha256,
        },
        summary=summary,
    )


async def get_signature(supabase: AsyncClient, user: UserRead, signature_id: str) -> dict:
    """Ownership-checked read - a missing id and one owned by someone else
    look identical to the caller, same pattern as get_document/get_proposal."""
    resp = (
        await supabase.table("signatures")
        .select(_SIGNATURE_COLUMNS)
        .eq("id", signature_id)
        .maybe_single()
        .execute()
    )
    signature = resp.data if resp is not None else None
    if signature is None or signature["user_id"] != str(user.id):
        raise NotFoundError("Signature not found.")
    return signature


async def list_signatures_for_document(
    supabase: AsyncClient, user: UserRead, document_id: str
) -> list[dict]:
    # Confirms ownership of the document itself - a document with zero
    # signatures still 404s if it isn't the caller's.
    await documents_service.get_document(supabase, str(user.id), document_id)

    resp = (
        await supabase.table("signatures")
        .select(_SIGNATURE_COLUMNS)
        .eq("document_id", document_id)
        .eq("user_id", str(user.id))
        .order("created_at")
        .execute()
    )
    return resp.data


async def verify_signature(supabase: AsyncClient, user: UserRead, signature_id: str) -> dict:
    """Independent re-check: verifies the exact bytes recorded at signing
    time (`signatures.canonical_payload` - not reconstructed from the other
    columns, which would risk a false mismatch from datetime
    round-tripping through Postgres with different precision) against the
    public key recorded for `signature.key_id`, whether or not that key is
    still the active one. Also re-hashes the document's CURRENT content, so
    a caller can tell "signed correctly" apart from "still matches what's
    on file now"."""
    signature = await get_signature(supabase, user, signature_id)

    key_resp = (
        await supabase.table("signing_keys")
        .select("public_key_b64")
        .eq("key_id", signature["key_id"])
        .maybe_single()
        .execute()
    )
    key_row = key_resp.data if key_resp is not None else None
    if key_row is None:
        # Should be unreachable - signing_keys rows are never deleted - but
        # a missing key must fail verification, not raise past the caller.
        return {"signature": signature, "signature_valid": False, "document_unchanged": False}

    payload = signature["canonical_payload"].encode("utf-8")
    signature_valid = keys.verify(
        key_row["public_key_b64"], payload, base64.b64decode(signature["signature_b64"])
    )

    document = await documents_service.get_document_with_content(
        supabase, str(user.id), signature["document_id"]
    )
    current_sha256 = hashlib.sha256(document["content"]).hexdigest()
    document_unchanged = current_sha256 == signature["document_sha256"]

    return {
        "signature": signature,
        "signature_valid": signature_valid,
        "document_unchanged": document_unchanged,
    }


async def _require_admin_issued_sign_proposal(
    supabase: AsyncClient, user: UserRead, proposal_id: str
) -> tuple[dict, dict]:
    """Shared ownership/shape checks for both halves of the OTP+Face path
    below: a still-`get_proposal`-visible `sign_document` proposal, whose
    document is one an admin issued (not a self-upload - those stay on the
    ordinary Face-or-password confirm at POST /chat/proposals/{id}/confirm).
    Returns (proposal, document)."""
    proposal = await proposals_service.get_proposal(supabase, user, proposal_id)
    if proposal["proposal_type"] != "sign_document":
        raise ValidationError("This proposal is not a document signature request.")

    document = await documents_service.get_document(
        supabase, str(user.id), proposal["payload"]["document_id"]
    )
    if document.get("issued_by_admin_id") is None:
        raise ValidationError(
            "Acest document nu necesită cod OTP - folosește confirmarea obișnuită."
        )
    return proposal, document


async def request_signing_code(supabase: AsyncClient, user: UserRead, proposal_id: str) -> None:
    """Issues a fresh OTP for the OTP+Face confirm below and delivers it via
    the same Teams channel password-reset codes already use (see
    auth/service.py::request_password_reset) - no new delivery
    infrastructure, same "pretend this reaches the user" convention this
    demo already relies on.

    Scoped by `proposal_id`, not just the user: any prior unconsumed code
    for THIS proposal is invalidated first, but a code requested for a
    different pending signature is untouched - see 0019_admin_documents.sql.
    """
    proposal, _document = await _require_admin_issued_sign_proposal(supabase, user, proposal_id)
    await proposals_service.ensure_pending_and_not_expired(supabase, proposal)

    await (
        supabase.table("document_signing_codes")
        .update({"consumed_at": datetime.now(UTC).isoformat()})
        .eq("proposal_id", proposal_id)
        .is_("consumed_at", "null")
        .execute()
    )

    code = generate_otp_code()
    expires_at = datetime.now(UTC) + timedelta(minutes=SIGNING_CODE_TTL_MINUTES)
    await (
        supabase.table("document_signing_codes")
        .insert(
            {
                "user_id": str(user.id),
                "proposal_id": proposal_id,
                "code_hash": hash_otp_code(code),
                "expires_at": expires_at.isoformat(),
            }
        )
        .execute()
    )

    await send_teams_message(
        f"🔐 Cod semnare document BanK pentru {user.email}: **{code}**  \n"
        f"Valabil {SIGNING_CODE_TTL_MINUTES} minute."
    )


async def _verify_and_consume_signing_code(
    supabase: AsyncClient, user: UserRead, proposal_id: str, code: str
) -> None:
    resp = (
        await supabase.table("document_signing_codes")
        .select("*")
        .eq("proposal_id", proposal_id)
        .eq("user_id", str(user.id))
        .is_("consumed_at", "null")
        .order("created_at", desc=True)
        .limit(1)
        .maybe_single()
        .execute()
    )
    code_row = resp.data if resp is not None else None
    if code_row is None or code_row["attempts"] >= SIGNING_CODE_MAX_ATTEMPTS:
        raise InvalidSigningCodeError()

    expires_at = datetime.fromisoformat(code_row["expires_at"])
    if datetime.now(UTC) >= expires_at or hash_otp_code(code) != code_row["code_hash"]:
        await (
            supabase.table("document_signing_codes")
            .update({"attempts": code_row["attempts"] + 1})
            .eq("id", code_row["id"])
            .execute()
        )
        raise InvalidSigningCodeError()

    await (
        supabase.table("document_signing_codes")
        .update({"consumed_at": datetime.now(UTC).isoformat()})
        .eq("id", code_row["id"])
        .execute()
    )


async def confirm_admin_document(
    supabase: AsyncClient,
    user: UserRead,
    proposal_id: str,
    *,
    otp_code: str,
    face_token: str,
) -> dict:
    """The stronger confirm path for an admin-issued document: BOTH an OTP
    (something the user has - Teams access) AND a live face match (something
    the user is) are required, unlike the ordinary Face-OR-password gate in
    proposals_service.confirm_proposal. Deliberately a separate function
    rather than a third `auth_method` branch there - that function's
    `credential: str` is a single value, and bolting a second one on for
    just this one case would make every OTHER caller's single-credential
    shape a lie.

    OTP is checked and consumed FIRST: it is the cheaper check (a DB lookup,
    no external service), so a mistyped code never costs the user a
    face-confirmation token, which is separately rate-limited and single-use
    on its own terms (see face_auth_service).
    """
    proposal, document = await _require_admin_issued_sign_proposal(supabase, user, proposal_id)
    await proposals_service.ensure_pending_and_not_expired(supabase, proposal)

    await _verify_and_consume_signing_code(supabase, user, proposal_id, otp_code)

    if not await face_auth_service.has_face_enrolled(supabase, user):
        raise ValidationError("Autentificarea facială nu este activată pe acest cont.")
    await face_auth_service.consume_face_confirmation_token(supabase, user, face_token)

    result = await sign_document(
        supabase,
        user,
        proposal,
        document_id=document["id"],
        intent=proposal["payload"]["intent"],
        auth_method="otp_face",
        expected_document_sha256=proposal["payload"]["document_sha256"],
    )
    return await proposals_service.mark_confirmed(supabase, proposal, result)
