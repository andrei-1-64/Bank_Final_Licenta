from postgrest.exceptions import APIError
from supabase import AsyncClient

from app.core.audit import record_audit_event
from app.core.security import generate_referral_code
from app.modules.users.schemas import UserRead, UserUpdate

UNIQUE_VIOLATION = "23505"
_REFERRAL_CODE_GENERATION_ATTEMPTS = 5


async def update_profile(supabase: AsyncClient, user: UserRead, payload: UserUpdate) -> UserRead:
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if not updates:
        return user

    resp = await supabase.table("users").update(updates).eq("id", str(user.id)).execute()

    await record_audit_event(
        supabase,
        user_id=user.id,
        action="users.update_profile",
        entity=f"users:{user.id}",
        metadata={"fields": list(updates)},
    )
    return UserRead.model_validate(resp.data[0])


async def ensure_referral_code(supabase: AsyncClient, user: UserRead) -> str:
    """Returns the user's referral code, generating and persisting one on
    first call if they don't have one yet (see 0008_user_referral_codes.sql).
    A generated code is practically always unique (33^8 possibilities), but
    "practically" isn't a guarantee - retry a few times on a genuine
    collision, same pattern as accounts/service.py's IBAN generation."""
    if user.referral_code:
        return user.referral_code

    last_error: APIError | None = None
    for _ in range(_REFERRAL_CODE_GENERATION_ATTEMPTS):
        code = generate_referral_code()
        try:
            await (
                supabase.table("users")
                .update({"referral_code": code})
                .eq("id", str(user.id))
                .execute()
            )
            return code
        except APIError as exc:
            if exc.code != UNIQUE_VIOLATION:
                raise
            last_error = exc
    raise last_error  # pragma: no cover - astronomically unlikely
