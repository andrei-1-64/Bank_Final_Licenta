"""Registration, login, logout. Session mechanics (the cookie, the token
hashing) are core/security.py + core/dependencies.py, documented in
docs/AUTH_HANDOFF.md - this module is the producer side: it's the only
code that ever writes to `sessions`.
"""

import uuid
from datetime import UTC, datetime, timedelta

from postgrest.exceptions import APIError
from supabase import AsyncClient

from app.config import settings
from app.core.audit import record_audit_event
from app.core.exceptions import (
    AccountBlockedError,
    EmailAlreadyRegisteredError,
    InvalidResetCodeError,
    LoginRateLimitedError,
    NationalIdAlreadyRegisteredError,
    UnauthorizedError,
    ValidationError,
)
from app.core.security import (
    generate_otp_code,
    generate_session_token,
    hash_otp_code,
    hash_password,
    hash_session_token,
    verify_password,
)
from app.core.teams import send_teams_message
from app.modules.auth.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
)
from app.modules.auth.validation import (
    extract_date_of_birth,
    extract_gender,
    validate_national_id,
)
from app.modules.notifications import service as notifications_service
from app.modules.users.schemas import UserRead

LOGIN_MAX_FAILED_ATTEMPTS = 5
LOGIN_LOCKOUT_WINDOW_MINUTES = 15

RESET_CODE_TTL_MINUTES = 10
RESET_CODE_MAX_ATTEMPTS = 5

UNIQUE_VIOLATION = "23505"

# Optional at registration - entering a valid one is what unlocks the
# welcome balance on accounts (see accounts/service.py::OPENING_BALANCE_MINOR).
# Two kinds of code count: any user's own per-user users.referral_code (see
# users/service.py::ensure_referral_code, added in 0008), OR this fixed
# fallback - the original single global code, kept working for anyone it
# was already shared with. Case-insensitive either way.
FALLBACK_REFERRAL_CODE = "BanKTHA"


async def _resolve_referral(supabase: AsyncClient, referral_code: str | None) -> tuple[bool, str | None]:
    """Returns (bonus_eligible, referrer_user_id). referrer_user_id is only
    ever set for a per-user code match - the fallback code makes the new
    user eligible for their own welcome balance, but has no specific referrer
    to reward (see accounts/service.py's referral_rewards handling)."""
    if referral_code is None:
        return False, None
    code = referral_code.strip()
    if not code:
        return False, None
    if code.lower() == FALLBACK_REFERRAL_CODE.lower():
        return True, None

    resp = (
        await supabase.table("users")
        .select("id")
        .eq("referral_code", code.upper())
        .maybe_single()
        .execute()
    )
    if resp is not None and resp.data is not None:
        return True, resp.data["id"]
    return False, None


async def start_session(supabase: AsyncClient, user: UserRead) -> str:
    """Creates a session row and returns the raw token to put in the
    cookie. Shared by register (auto-login) and login.

    Also THE login-side block check. Every way into a session goes through
    here - password login, register's auto-login, face login
    (face_auth/service.py) and trusted-device login
    (trusted_devices/service.py) - so a blocked user is refused a cookie
    once, here, instead of in four places that could drift apart.

    The state is re-read from the database rather than taken from `user`:
    callers build that object in different ways (some from `select("*")`,
    one from a fresh insert), and authorisation must not depend on which.
    """
    blocked_resp = (
        await supabase.table("users")
        .select("blocked_at")
        .eq("id", str(user.id))
        .maybe_single()
        .execute()
    )
    blocked_row = blocked_resp.data if blocked_resp is not None else None
    if blocked_row is not None and blocked_row.get("blocked_at") is not None:
        raise AccountBlockedError()

    token = generate_session_token()
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.SESSION_TTL_SECONDS)
    await supabase.table("sessions").insert(
        {
            "user_id": str(user.id),
            "token_hash": hash_session_token(token),
            "expires_at": expires_at.isoformat(),
        }
    ).execute()
    return token


def _duplicate_registration_error(exc: APIError) -> Exception:
    detail = f"{exc.message} {exc.details or ''}".lower()
    if "national_id" in detail:
        return NationalIdAlreadyRegisteredError()
    return EmailAlreadyRegisteredError()


async def register_user(supabase: AsyncClient, payload: RegisterRequest) -> tuple[UserRead, str]:
    is_valid, reason = validate_national_id(payload.national_id)
    if not is_valid:
        raise ValidationError(reason)

    referral_bonus_eligible, referred_by_user_id = await _resolve_referral(supabase, payload.referral_code)

    try:
        resp = (
            await supabase.table("users")
            .insert(
                {
                    "email": payload.email.lower(),
                    "password_hash": hash_password(payload.password),
                    "first_name": payload.first_name.strip(),
                    "last_name": payload.last_name.strip(),
                    "national_id": payload.national_id,
                    "gender": extract_gender(payload.national_id),
                    "date_of_birth": extract_date_of_birth(payload.national_id).isoformat(),
                    "phone": payload.phone,
                    "address": payload.address,
                    "referral_bonus_eligible": referral_bonus_eligible,
                    "referred_by_user_id": referred_by_user_id,
                }
            )
            .execute()
        )
    except APIError as exc:
        if exc.code == UNIQUE_VIOLATION:
            raise _duplicate_registration_error(exc) from exc
        raise

    user = UserRead.model_validate(resp.data[0])

    await record_audit_event(
        supabase, user_id=user.id, action="auth.register", entity=f"users:{user.id}"
    )

    token = await start_session(supabase, user)
    return user, token


async def _count_recent_failed_attempts(supabase: AsyncClient, email: str) -> int:
    window_start = datetime.now(UTC) - timedelta(minutes=LOGIN_LOCKOUT_WINDOW_MINUTES)
    resp = (
        await supabase.table("login_attempts")
        .select("id", count="exact")
        .eq("email", email)
        .eq("success", False)
        .gte("created_at", window_start.isoformat())
        .execute()
    )
    return resp.count or 0


async def check_existing_account(supabase: AsyncClient, email: str, national_id: str) -> dict:
    """Read-only pre-check for the onboarding assistant (see
    app/modules/onboarding/tool.py) - lets it short-circuit as soon as it
    has an email + CNP, instead of only discovering a duplicate at the
    final /auth/register call after a full interview. Reveals nothing
    register_user's 409 doesn't already: same two fields, same
    disclosure, just earlier.
    """
    email = email.lower()
    email_resp = (
        await supabase.table("users").select("id").eq("email", email).maybe_single().execute()
    )
    if email_resp is not None and email_resp.data is not None:
        return {"exists": True, "matched_field": "email"}

    national_id_resp = (
        await supabase.table("users")
        .select("id")
        .eq("national_id", national_id)
        .maybe_single()
        .execute()
    )
    if national_id_resp is not None and national_id_resp.data is not None:
        return {"exists": True, "matched_field": "national_id"}

    return {"exists": False, "matched_field": None}


async def login_user(supabase: AsyncClient, payload: LoginRequest) -> tuple[UserRead, str]:
    email = payload.email.lower()

    if await _count_recent_failed_attempts(supabase, email) >= LOGIN_MAX_FAILED_ATTEMPTS:
        raise LoginRateLimitedError()

    resp = await supabase.table("users").select("*").eq("email", email).maybe_single().execute()
    user_row = resp.data if resp is not None else None

    # Same error whether the email doesn't exist or the password is wrong -
    # don't leak which one it is (avoids user enumeration).
    if user_row is None or not verify_password(payload.password, user_row["password_hash"]):
        await supabase.table("login_attempts").insert({"email": email, "success": False}).execute()
        # Each REST call is already its own committed statement (unlike the
        # old SQLAlchemy request-scoped transaction), so unlike the previous
        # version there's no ambient transaction that could roll this
        # failure-log insert back - no explicit commit needed here.
        raise UnauthorizedError("Invalid email or password.")

    user = UserRead.model_validate(user_row)

    await supabase.table("login_attempts").insert({"email": email, "success": True}).execute()
    await record_audit_event(
        supabase, user_id=user.id, action="auth.login", entity=f"users:{user.id}"
    )

    token = await start_session(supabase, user)
    return user, token


async def request_password_reset(supabase: AsyncClient, email: str) -> None:
    """Always completes normally, whether or not the email is registered -
    the caller (router) must not be able to tell the two cases apart, or
    this becomes a user-enumeration oracle. If the user exists: any prior
    unconsumed code is invalidated, a fresh one is generated, stored (hash
    only), and best-effort delivered to Teams. A Teams outage never surfaces
    here - see core/teams.py."""
    email = email.lower()
    resp = await supabase.table("users").select("id").eq("email", email).maybe_single().execute()
    user_row = resp.data if resp is not None else None
    if user_row is None:
        return
    user_id = user_row["id"]

    # At most one live code per user - an old, still-unconsumed code from a
    # previous request shouldn't keep working once a new one is issued.
    await (
        supabase.table("password_reset_codes")
        .update({"consumed_at": datetime.now(UTC).isoformat()})
        .eq("user_id", user_id)
        .is_("consumed_at", "null")
        .execute()
    )

    code = generate_otp_code()
    expires_at = datetime.now(UTC) + timedelta(minutes=RESET_CODE_TTL_MINUTES)
    await supabase.table("password_reset_codes").insert(
        {
            "user_id": user_id,
            "code_hash": hash_otp_code(code),
            "expires_at": expires_at.isoformat(),
        }
    ).execute()

    await send_teams_message(
        f"🔐 Cod resetare parolă BanK pentru {email}: **{code}**  \n"
        f"Valabil {RESET_CODE_TTL_MINUTES} minute."
    )

    await record_audit_event(
        supabase, user_id=uuid.UUID(user_id), action="auth.request_password_reset", entity=f"users:{user_id}"
    )


async def _get_user_id_for_reset(supabase: AsyncClient, email: str) -> str:
    resp = await supabase.table("users").select("id").eq("email", email.lower()).maybe_single().execute()
    user_row = resp.data if resp is not None else None
    # Same generic error whether the email is unknown, the code is wrong, or
    # it expired - never confirm which one it was (see request_password_reset).
    if user_row is None:
        raise InvalidResetCodeError()
    return user_row["id"]


async def _verify_code_or_raise(supabase: AsyncClient, user_id: str, code: str) -> dict:
    """Returns the still-live code row if `code` matches, unexpired, under the
    attempt limit. Records a failed attempt (shared with the final
    reset_password call, so brute-forcing via either endpoint counts against
    the same limit) and raises otherwise. Never consumes the code itself -
    only reset_password does that, once the new password is actually set."""
    resp = (
        await supabase.table("password_reset_codes")
        .select("*")
        .eq("user_id", user_id)
        .is_("consumed_at", "null")
        .order("created_at", desc=True)
        .limit(1)
        .maybe_single()
        .execute()
    )
    code_row = resp.data if resp is not None else None
    if code_row is None or code_row["attempts"] >= RESET_CODE_MAX_ATTEMPTS:
        raise InvalidResetCodeError()

    expires_at = datetime.fromisoformat(code_row["expires_at"])
    if datetime.now(UTC) >= expires_at or hash_otp_code(code) != code_row["code_hash"]:
        await (
            supabase.table("password_reset_codes")
            .update({"attempts": code_row["attempts"] + 1})
            .eq("id", code_row["id"])
            .execute()
        )
        raise InvalidResetCodeError()

    return code_row


async def verify_password_reset_code(supabase: AsyncClient, email: str, code: str) -> None:
    """Step 2 of the flow: confirms the code alone, before the frontend shows
    the new-password form. Deliberately does not consume the code - the user
    still has to actually submit a new password (reset_password) to use it
    up, so a verified-but-abandoned attempt doesn't burn the code."""
    user_id = await _get_user_id_for_reset(supabase, email)
    await _verify_code_or_raise(supabase, user_id, code)


async def reset_password(supabase: AsyncClient, payload: ResetPasswordRequest) -> None:
    user_id = await _get_user_id_for_reset(supabase, payload.email)
    code_row = await _verify_code_or_raise(supabase, user_id, payload.code)

    await supabase.table("users").update(
        {"password_hash": hash_password(payload.new_password)}
    ).eq("id", user_id).execute()

    await (
        supabase.table("password_reset_codes")
        .update({"consumed_at": datetime.now(UTC).isoformat()})
        .eq("id", code_row["id"])
        .execute()
    )

    # A reset invalidates every existing session, on every device - the same
    # way a real bank would treat "someone just changed this password".
    await supabase.table("sessions").delete().eq("user_id", user_id).execute()

    await record_audit_event(
        supabase, user_id=uuid.UUID(user_id), action="auth.reset_password", entity=f"users:{user_id}"
    )


async def change_password(
    supabase: AsyncClient,
    user: UserRead,
    current_token: str | None,
    payload: ChangePasswordRequest,
) -> None:
    """Change password for an already-authenticated user - distinct from
    reset_password (which proves identity via an OTP instead of a session).
    Unlike reset_password, this keeps the caller's own session alive: only
    every *other* session is invalidated, since the whole point here is
    "I'm already logged in on this device and just want a new password"."""
    resp = (
        await supabase.table("users").select("password_hash").eq("id", str(user.id)).maybe_single().execute()
    )
    user_row = resp.data if resp is not None else None
    if user_row is None or not verify_password(payload.current_password, user_row["password_hash"]):
        raise UnauthorizedError("Current password is incorrect.")

    await supabase.table("users").update(
        {"password_hash": hash_password(payload.new_password)}
    ).eq("id", str(user.id)).execute()

    sessions_query = supabase.table("sessions").delete().eq("user_id", str(user.id))
    if current_token:
        sessions_query = sessions_query.neq("token_hash", hash_session_token(current_token))
    await sessions_query.execute()

    await record_audit_event(
        supabase, user_id=user.id, action="auth.change_password", entity=f"users:{user.id}"
    )

    # Fraud-alert pattern: a real attacker who changed the password would
    # otherwise leave the account owner with no signal that it happened.
    await notifications_service.create_notification(
        supabase,
        user.id,
        title="Parola a fost schimbată",
        body=(
            "Parola contului tău a fost schimbată chiar acum. Dacă nu ai "
            "fost tu, contactează banca imediat."
        ),
    )


async def logout_user(supabase: AsyncClient, user: UserRead, token: str | None) -> None:
    if token:
        await supabase.table("sessions").delete().eq(
            "token_hash", hash_session_token(token)
        ).execute()
    await record_audit_event(
        supabase, user_id=user.id, action="auth.logout", entity=f"users:{user.id}"
    )
