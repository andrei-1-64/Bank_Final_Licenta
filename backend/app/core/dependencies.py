"""get_supabase, get_current_user, require_admin - the CONTRACT every other
module builds on.

get_current_user is the read side of the session mechanism: it trusts
nothing but the hashed cookie value. It is deliberately independent of the
login/logout/register endpoints (owned by the auth teammate, see
docs/AUTH_HANDOFF.md) - anything that can insert a valid row into `sessions`
(the login endpoint, or a test fixture) can authenticate a request. This
module never writes to `sessions`, only reads.

require_admin layers on top of it and is the ONLY gate on app/modules/admin.
"""

from datetime import UTC, datetime

from fastapi import Depends, Request
from supabase import AsyncClient

from app.config import settings
from app.core.exceptions import AccountBlockedError, ForbiddenError, UnauthorizedError
from app.core.security import hash_session_token
from app.db.supabase_client import get_supabase
from app.modules.users.schemas import UserRead

#: The only value of users.role that grants admin access (see
#: migrations/0016_admin_role.sql).
ADMIN_ROLE = "admin"

#: `blocked_at` is here, not fetched separately like `role` is in
#: require_admin, because blocking has to be enforced on EVERY authenticated
#: request - a blocked user must not get past get_current_user. That makes
#: migration 0017 a hard prerequisite for this code: until it runs, this
#: select names a column that does not exist and nobody can authenticate.
_USER_COLUMNS = (
    "id, email, first_name, last_name, email_verified, "
    "national_id, gender, date_of_birth, phone, address, "
    "referral_bonus_eligible, referral_code, referred_by_user_id, created_at, "
    "blocked_at"
)


async def get_current_user(
    request: Request,
    supabase: AsyncClient = Depends(get_supabase),
) -> UserRead:
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not token:
        raise UnauthorizedError()

    token_hash = hash_session_token(token)
    # Two round-trips, deliberately, instead of a `user:users(...)` embed in
    # the same select: under contention the embed intermittently comes back
    # with `user: null` for a session row that plainly has a valid
    # `user_id` (PostgREST/PgBouncer transaction-pooling artifact, not a
    # data problem) - which this code used to treat as "no session" and
    # answer with a spurious 401. Splitting the query removes the embed, so
    # there is nothing left to come back null for a real session.
    session_resp = (
        await supabase.table("sessions")
        .select("user_id, expires_at")
        .eq("token_hash", token_hash)
        .maybe_single()
        .execute()
    )

    if session_resp is None or session_resp.data is None:
        raise UnauthorizedError()

    session_row = session_resp.data
    expires_at = datetime.fromisoformat(session_row["expires_at"])
    if expires_at <= datetime.now(UTC):
        raise UnauthorizedError("Session has expired.")

    user_resp = (
        await supabase.table("users")
        .select(_USER_COLUMNS)
        .eq("id", session_row["user_id"])
        .maybe_single()
        .execute()
    )

    if user_resp is None or user_resp.data is None:
        raise UnauthorizedError()

    user = UserRead.model_validate(user_resp.data)

    # Checked here, on the read side, rather than only at login: blocking has
    # to take effect for sessions that already exist, not just future ones.
    # (The admin also deletes the user's sessions when blocking them - this
    # is the belt to that's braces, and covers a session created in the same
    # instant.)
    if user.blocked_at is not None:
        raise AccountBlockedError()

    return user


async def require_admin(
    user: UserRead = Depends(get_current_user),
    supabase: AsyncClient = Depends(get_supabase),
) -> UserRead:
    """Authenticated AND `users.role = 'admin'`. The gate on every admin route.

    Two deliberate choices:

    * The role is re-read from the database on EVERY request rather than
      being carried on the session or in `UserRead`. Revoking an admin is
      then immediate (`UPDATE users SET role = 'customer'`), instead of
      staying valid until whatever cached it expires - which for this app's
      session cookie would be up to 7 days.
    * It is a separate query rather than a `role` column added to
      `_USER_COLUMNS` above. That keeps the whole app working when
      migration 0016 has not been applied yet: only the admin endpoints
      fail (with a clear error), not every logged-in request.

    Raises ForbiddenError, never a 404-style dodge: the caller is already
    authenticated, so there is nothing to hide from them about the existence
    of an admin area.
    """
    resp = (
        await supabase.table("users")
        .select("role")
        .eq("id", str(user.id))
        .maybe_single()
        .execute()
    )
    row = resp.data if resp is not None else None
    if row is None or row.get("role") != ADMIN_ROLE:
        raise ForbiddenError()
    return user
