"""DIY face login ("varianta DIY cu camera" - see the design discussion this
followed, not WebAuthn/passkeys). Enrollment stores one face_recognition
(dlib) embedding per user; login extracts a fresh embedding from a live
capture and compares it 1:1 against the enrolled one for the claimed email.

LIVENESS: every capture is a short BURST of frames (see `_extract_embedding`
below and vision/app/face.py), and vision-service refuses to return an
embedding at all unless the eyes actually blink across that burst - a
printed photo, or a photo of a phone screen held up to the camera, has
nothing moving in it and never produces the required open -> closed -> open
sequence. This is still demo-grade biometric auth, not a real security
boundary - it does not defend against a played VIDEO of the enrolled user
blinking, only against a still image - so never treat it as equivalent to
the password/OTP flows for anything that actually matters. See migration
0011's header for the same caveat.
"""

import hashlib
import math
import secrets
from datetime import UTC, datetime, timedelta

from postgrest.exceptions import APIError
from supabase import AsyncClient

from app.core import vision_client
from app.core.audit import record_audit_event
from app.core.exceptions import (
    FaceConfirmationRequiredError,
    FaceEnrollmentRequiredError,
    InvalidFaceConfirmationError,
    LoginRateLimitedError,
    UnauthorizedError,
    ValidationError,
)
from app.core.security import verify_password
from app.modules.auth import service as auth_service
from app.modules.users.schemas import UserRead

MATCH_THRESHOLD = 0.6
UNIQUE_VIOLATION = "23505"

# Step-up auth for large transfers/payments (see enforce_face_confirmation
# below, called from transfers/service.py and payments/service.py). Applied
# regardless of currency - a simplification, not "500000 of any currency"
# being treated as equally large, but good enough for a demo-grade check.
FACE_CONFIRMATION_THRESHOLD_MINOR = 500_000
FACE_CONFIRMATION_TOKEN_TTL_MINUTES = 3


#: Errors vision-service reports for a photo it could read but couldn't use,
#: mapped to the messages this module has always returned.
_FACE_EXTRACTION_MESSAGES = {
    "no_face_detected": "No face detected in the photo. Try a clearer, well-lit picture.",
    "multiple_faces_detected": (
        "Multiple faces detected - only one person can enroll at a time."
    ),
    "no_blink_detected": (
        "No blink detected - hold the camera steady, look at it, and blink naturally."
    ),
}


async def _extract_embedding(frames: list[bytes]) -> list[float]:
    """Turn a burst of frames into a 128-value embedding, via vision-service.

    `frames` (not a single photo) is what makes this a LIVENESS check rather
    than a plain photo match: vision-service requires the eyes to actually
    blink across the burst before it returns anything at all - see
    vision/app/face.py's module docstring for why one still photo can never
    prove that. Every real caller (enroll_face, login_with_face,
    create_face_confirmation below) captures and sends a burst; nothing here
    accepts a single frame anymore.

    THE SPLIT: only the CV step needs dlib, so only that step left the
    backend (see vision/app/face.py). Everything else about faces in this
    module - storing an embedding, comparing two of them, issuing and
    consuming confirmation tokens - is arithmetic and database work and
    stays here, which is why `enforce_face_confirmation` and friends can
    still be called in-process from transfers, payments and proposals.

    The frames go to a container that has no database and no session
    context; they come back as numbers and are never persisted there.
    """
    try:
        return await vision_client.extract_face_embedding(frames)
    except ValidationError as exc:
        # Re-raised with this module's own wording so the API's error text
        # is unchanged by the split.
        raise ValidationError(
            _FACE_EXTRACTION_MESSAGES.get(str(exc.message), str(exc.message))
        ) from exc


def _distance(known: list[float], candidate: list[float]) -> float:
    """Euclidean distance between two embeddings - exactly what
    face_recognition.face_distance computed before the split, without
    needing numpy or dlib in this image for two subtractions.

    A length mismatch means the stored embedding came from a different model
    and is not comparable; treated as "no match" rather than crashing.
    """
    if len(known) != len(candidate):
        return float("inf")
    return math.dist(known, candidate)


async def enroll_face(supabase: AsyncClient, user: UserRead, frames: list[bytes]) -> None:
    embedding = await _extract_embedding(frames)

    try:
        await (
            supabase.table("face_credentials")
            .insert({"user_id": str(user.id), "embedding": embedding})
            .execute()
        )
    except APIError as exc:
        if exc.code != UNIQUE_VIOLATION:
            raise
        # Already enrolled - re-enrolling replaces the old face, it doesn't
        # stack multiple faces per user.
        await (
            supabase.table("face_credentials")
            .update({"embedding": embedding})
            .eq("user_id", str(user.id))
            .execute()
        )

    await record_audit_event(
        supabase, user_id=user.id, action="auth.face_enroll", entity=f"users:{user.id}"
    )


async def remove_face(supabase: AsyncClient, user: UserRead) -> None:
    await supabase.table("face_credentials").delete().eq("user_id", str(user.id)).execute()
    await record_audit_event(
        supabase, user_id=user.id, action="auth.face_remove", entity=f"users:{user.id}"
    )


async def has_face_enrolled_by_id(supabase: AsyncClient, user_id: str) -> bool:
    resp = (
        await supabase.table("face_credentials")
        .select("id")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    return resp is not None and resp.data is not None


async def has_face_enrolled(supabase: AsyncClient, user: UserRead) -> bool:
    return await has_face_enrolled_by_id(supabase, str(user.id))


async def _count_recent_failed_attempts(supabase: AsyncClient, email: str) -> int:
    # Shares login_attempts and the lockout window/threshold with password
    # login (auth/service.py) - too many failed attempts of EITHER kind
    # locks the account the same way.
    window_start = datetime.now(UTC) - timedelta(minutes=auth_service.LOGIN_LOCKOUT_WINDOW_MINUTES)
    resp = (
        await supabase.table("login_attempts")
        .select("id", count="exact")
        .eq("email", email)
        .eq("success", False)
        .gte("created_at", window_start.isoformat())
        .execute()
    )
    return resp.count or 0


async def login_with_face(supabase: AsyncClient, email: str, frames: list[bytes]) -> tuple[UserRead, str]:
    email = email.lower()

    if await _count_recent_failed_attempts(supabase, email) >= auth_service.LOGIN_MAX_FAILED_ATTEMPTS:
        raise LoginRateLimitedError()

    resp = await supabase.table("users").select("*").eq("email", email).maybe_single().execute()
    user_row = resp.data if resp is not None else None

    credential_row = None
    if user_row is not None:
        cred_resp = (
            await supabase.table("face_credentials")
            .select("embedding")
            .eq("user_id", user_row["id"])
            .maybe_single()
            .execute()
        )
        credential_row = cred_resp.data if cred_resp is not None else None

    matched = False
    if user_row is not None and credential_row is not None:
        candidate = await _extract_embedding(frames)
        distance = _distance(credential_row["embedding"], candidate)
        matched = bool(distance <= MATCH_THRESHOLD)

    # Same shape as password login: unknown email, no face enrolled, and a
    # non-matching face all fail identically - don't leak which one it was.
    if not matched:
        await supabase.table("login_attempts").insert({"email": email, "success": False}).execute()
        raise UnauthorizedError("Face not recognized.")

    user = UserRead.model_validate(user_row)

    await supabase.table("login_attempts").insert({"email": email, "success": True}).execute()
    await record_audit_event(
        supabase, user_id=user.id, action="auth.face_login", entity=f"users:{user.id}"
    )

    token = await auth_service.start_session(supabase, user)
    return user, token


def _hash_confirmation_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_face_confirmation(supabase: AsyncClient, user: UserRead, frames: list[bytes]) -> str:
    """Re-verifies the already-logged-in `user`'s face (1:1, same comparison
    as login_with_face) and issues a short-lived, single-use token proving
    "yes, still you" - the step-up auth transfers/payments require above
    FACE_CONFIRMATION_THRESHOLD_MINOR. Unlike login, identity here comes from
    the session, not the photo - a mismatched face is a straight rejection,
    never "try another email"."""
    cred_resp = (
        await supabase.table("face_credentials")
        .select("embedding")
        .eq("user_id", str(user.id))
        .maybe_single()
        .execute()
    )
    credential_row = cred_resp.data if cred_resp is not None else None
    if credential_row is None:
        raise ValidationError("Face Login is not enabled on this account.")

    candidate = await _extract_embedding(frames)
    distance = _distance(credential_row["embedding"], candidate)
    if distance > MATCH_THRESHOLD:
        raise UnauthorizedError("Face not recognized.")

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(minutes=FACE_CONFIRMATION_TOKEN_TTL_MINUTES)
    await supabase.table("face_confirmations").insert(
        {
            "user_id": str(user.id),
            "token_hash": _hash_confirmation_token(token),
            "expires_at": expires_at.isoformat(),
        }
    ).execute()
    return token


async def _consume_face_confirmation(supabase: AsyncClient, user: UserRead, token: str) -> None:
    resp = (
        await supabase.table("face_confirmations")
        .select("*")
        .eq("token_hash", _hash_confirmation_token(token))
        .eq("user_id", str(user.id))
        .is_("consumed_at", "null")
        .maybe_single()
        .execute()
    )
    row = resp.data if resp is not None else None
    if row is None:
        raise InvalidFaceConfirmationError()

    expires_at = datetime.fromisoformat(row["expires_at"])
    if datetime.now(UTC) >= expires_at:
        raise InvalidFaceConfirmationError()

    await (
        supabase.table("face_confirmations")
        .update({"consumed_at": datetime.now(UTC).isoformat()})
        .eq("id", row["id"])
        .execute()
    )


async def consume_face_confirmation_token(supabase: AsyncClient, user: UserRead, token: str) -> None:
    """Public entry point for callers that need to consume a step-up token
    unconditionally - unlike `enforce_face_confirmation` below, this never
    no-ops for a user with no face enrolled (that no-op exists there because
    face is an *optional extra* on top of session auth for its callers;
    here, the caller - proposals_service.confirm_proposal - has the user's
    explicit choice of auth_method="face" and must not silently let an
    unenrolled user "succeed" a check that was never performed). Callers for
    whom that distinction matters should call `has_face_enrolled` first.

    Just exposes the existing `_consume_face_confirmation` publicly - same
    validation (ownership, expiry, single-use), nothing new."""
    await _consume_face_confirmation(supabase, user, token)


def requires_face_confirmation(amount_minor: int) -> bool:
    return amount_minor >= FACE_CONFIRMATION_THRESHOLD_MINOR


async def _verify_user_password(supabase: AsyncClient, user: UserRead, password: str) -> bool:
    """Same check confirm_proposal's auth_method="password" branch uses -
    the account's real login password, not a separate credential."""
    resp = (
        await supabase.table("users")
        .select("password_hash")
        .eq("id", str(user.id))
        .maybe_single()
        .execute()
    )
    password_hash = resp.data["password_hash"] if resp is not None and resp.data else None
    return password_hash is not None and verify_password(password, password_hash)


async def enforce_face_confirmation(
    supabase: AsyncClient,
    user: UserRead,
    *,
    required: bool,
    token: str | None,
    password: str | None = None,
    require_enrolled: bool = False,
) -> None:
    """Single call site transfers/service.py and payments/service.py use
    before executing a money movement. `required` is the caller's decision -
    transfers only ever pass `requires_face_confirmation(amount_minor)`;
    payments additionally OR it with "first payment to this person" (see
    payments/service.py). No-ops when `required` is False; otherwise Face
    ID is mandatory - raises FaceEnrollmentRequiredError when the user has
    never enrolled it at all (there is no token they could supply),
    FaceConfirmationRequiredError when they have but didn't supply a token
    or password for THIS request, or InvalidFaceConfirmationError/
    UnauthorizedError when the one supplied doesn't check out.

    This used to no-op for a user with no Face ID enrolled, treating it as
    an optional extra rather than a real requirement - changed so a large
    transfer or a first payment to someone new can never go through
    unverified just because the sender skipped enrolling.

    `password` is the account's real login password, accepted as an equal
    alternative to a face token - not a weaker check, just a different
    factor. The caller decides WHEN it's appropriate to offer it (the
    frontend only reveals a password option after several failed face
    captures in the same modal session - see requestFaceConfirmationToken
    in app.js); this function itself doesn't track attempt counts, it only
    verifies whichever credential it was actually given. `token` is tried
    first if both happen to be present (never expected from a real client,
    but token wins deterministically rather than depending on dict order).

    `require_enrolled` (default False, existing callers never pass it) is a
    SEPARATE gate from `required`: enrollment as a precondition for the
    action to be attempted at all, independent of whether *this particular*
    call also demands a fresh step-up token. `required=False` still means
    "no step-up needed for this amount", so it still no-ops for a caller
    that also leaves `require_enrolled` at its default - existing behaviour,
    unchanged. A future caller for which Face ID is the primary auth (not a
    bonus on top of session auth) passes `require_enrolled=True` to reject
    an unenrolled user up front, before any token/amount logic runs."""
    if require_enrolled and not await has_face_enrolled(supabase, user):
        raise FaceEnrollmentRequiredError()

    if not required:
        return
    if not await has_face_enrolled(supabase, user):
        raise FaceEnrollmentRequiredError()

    if token is not None:
        await _consume_face_confirmation(supabase, user, token)
        return

    if password is not None:
        if not await _verify_user_password(supabase, user, password):
            raise UnauthorizedError("Incorrect password.")
        return

    raise FaceConfirmationRequiredError()
