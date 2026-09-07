"""Device trust: after registering, a user can verify a Teams-delivered OTP
once to enroll the current browser, then log in from that same browser with
just the account password afterwards - no email retyping, no OTP each time.

Deliberately NOT WebAuthn/passkeys: a website cannot read or verify a
user's actual OS/Windows password, so this only ever checks the BanK
account password again, behind a long-lived signed cookie that just proves
"this browser already completed OTP enrollment." See core/security.py's
header for why a hash (not the raw token) is what gets persisted - same
pattern as sessions.
"""

from datetime import UTC, datetime, timedelta

from supabase import AsyncClient

from app.core.audit import record_audit_event
from app.core.exceptions import (
    DeviceNotTrustedError,
    InvalidEnrollmentCodeError,
    LoginRateLimitedError,
    UnauthorizedError,
)
from app.core.security import (
    generate_otp_code,
    generate_session_token,
    hash_otp_code,
    hash_session_token,
    verify_password,
)
from app.core.teams import send_teams_message
from app.modules.auth import service as auth_service
from app.modules.face_auth import service as face_auth_service
from app.modules.users.schemas import UserRead

ENROLLMENT_CODE_TTL_MINUTES = 10
ENROLLMENT_CODE_MAX_ATTEMPTS = 5

LOGIN_MAX_FAILED_ATTEMPTS = 5


async def request_device_enrollment(supabase: AsyncClient, user: UserRead) -> None:
    """Sends a fresh OTP to enroll the current browser. Unlike password
    reset there's no enumeration concern - the caller is already
    authenticated (this runs right after registration), so we know exactly
    who they are."""
    user_id = str(user.id)

    # At most one live code per user, same reasoning as password reset.
    await (
        supabase.table("device_enrollment_codes")
        .update({"consumed_at": datetime.now(UTC).isoformat()})
        .eq("user_id", user_id)
        .is_("consumed_at", "null")
        .execute()
    )

    code = generate_otp_code()
    expires_at = datetime.now(UTC) + timedelta(minutes=ENROLLMENT_CODE_TTL_MINUTES)
    await supabase.table("device_enrollment_codes").insert(
        {
            "user_id": user_id,
            "code_hash": hash_otp_code(code),
            "expires_at": expires_at.isoformat(),
        }
    ).execute()

    await send_teams_message(
        f"🔐 Cod verificare dispozitiv BanK pentru {user.email}: **{code}**  \n"
        f"Valabil {ENROLLMENT_CODE_TTL_MINUTES} minute."
    )

    await record_audit_event(
        supabase,
        user_id=user.id,
        action="trusted_devices.request_enrollment",
        entity=f"users:{user_id}",
    )


async def verify_and_enroll_device(
    supabase: AsyncClient, user: UserRead, code: str, label: str | None
) -> str:
    """Verifies the OTP and, if valid, enrolls the current browser. Returns
    the raw device token - the router sets it as a cookie; only its hash is
    ever persisted."""
    user_id = str(user.id)
    resp = (
        await supabase.table("device_enrollment_codes")
        .select("*")
        .eq("user_id", user_id)
        .is_("consumed_at", "null")
        .order("created_at", desc=True)
        .limit(1)
        .maybe_single()
        .execute()
    )
    code_row = resp.data if resp is not None else None
    if code_row is None or code_row["attempts"] >= ENROLLMENT_CODE_MAX_ATTEMPTS:
        raise InvalidEnrollmentCodeError()

    expires_at = datetime.fromisoformat(code_row["expires_at"])
    if datetime.now(UTC) >= expires_at or hash_otp_code(code) != code_row["code_hash"]:
        await (
            supabase.table("device_enrollment_codes")
            .update({"attempts": code_row["attempts"] + 1})
            .eq("id", code_row["id"])
            .execute()
        )
        raise InvalidEnrollmentCodeError()

    await (
        supabase.table("device_enrollment_codes")
        .update({"consumed_at": datetime.now(UTC).isoformat()})
        .eq("id", code_row["id"])
        .execute()
    )

    # Same generation/hashing primitives as sessions - a device token is
    # exactly the same shape of secret (opaque, high-entropy, hash-only
    # persisted), just with a much longer lifetime and a different purpose.
    token = generate_session_token()
    await supabase.table("trusted_devices").insert(
        {
            "user_id": user_id,
            "device_token_hash": hash_session_token(token),
            "label": label,
        }
    ).execute()

    await record_audit_event(
        supabase, user_id=user.id, action="trusted_devices.enroll", entity=f"users:{user_id}"
    )
    return token


async def check_trusted_device(supabase: AsyncClient, raw_token: str | None) -> dict | None:
    """Read-only probe for the login page: is this browser already
    enrolled, and if so, whose account? Never touches last_used_at - only
    an actual login attempt does that.

    Also reports face_enrolled so the login page can try Face ID first on a
    recognized browser (the account is already pinned down by the device
    cookie, so there's no enumeration concern in revealing this, unlike the
    unauthenticated normal-login form)."""
    if not raw_token:
        return None
    resp = (
        await supabase.table("trusted_devices")
        .select("user:users(id, email)")
        .eq("device_token_hash", hash_session_token(raw_token))
        .maybe_single()
        .execute()
    )
    row = resp.data if resp is not None else None
    if row is None or row.get("user") is None:
        return None
    user_id = row["user"]["id"]
    face_enrolled = await face_auth_service.has_face_enrolled_by_id(supabase, user_id)
    return {"email": row["user"]["email"], "face_enrolled": face_enrolled}


async def login_with_trusted_device(
    supabase: AsyncClient, raw_token: str | None, password: str
) -> tuple[UserRead, str]:
    """The password-only login path for an already-enrolled browser. Rate-
    limited the same way as normal login (reuses auth.service's helper -
    same login_attempts table, same brute-force protection) since a leaked
    device cookie should not turn into "only the password stands between an
    attacker and this account" with no throttling at all."""
    if not raw_token:
        raise DeviceNotTrustedError()

    resp = (
        await supabase.table("trusted_devices")
        .select("id, user:users(*)")
        .eq("device_token_hash", hash_session_token(raw_token))
        .maybe_single()
        .execute()
    )
    row = resp.data if resp is not None else None
    if row is None or row.get("user") is None:
        raise DeviceNotTrustedError()

    user_row = row["user"]
    email = user_row["email"]

    recent_failures = await auth_service._count_recent_failed_attempts(supabase, email)
    if recent_failures >= LOGIN_MAX_FAILED_ATTEMPTS:
        raise LoginRateLimitedError()

    if not verify_password(password, user_row["password_hash"]):
        await supabase.table("login_attempts").insert({"email": email, "success": False}).execute()
        raise UnauthorizedError("Invalid password.")

    await supabase.table("login_attempts").insert({"email": email, "success": True}).execute()
    await (
        supabase.table("trusted_devices")
        .update({"last_used_at": datetime.now(UTC).isoformat()})
        .eq("id", row["id"])
        .execute()
    )

    user = UserRead.model_validate(user_row)
    await record_audit_event(
        supabase, user_id=user.id, action="trusted_devices.login", entity=f"users:{user.id}"
    )
    token = await auth_service.start_session(supabase, user)
    return user, token
