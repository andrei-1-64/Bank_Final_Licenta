import uuid

from supabase import AsyncClient

from app.core.exceptions import NotFoundError, ValidationError
from app.modules.auth.validation import validate_iban
from app.modules.users.schemas import UserRead


async def upsert_beneficiary(
    supabase: AsyncClient,
    *,
    user_id: uuid.UUID,
    iban: str,
    display_name: str,
    website: str | None = None,
    is_subscription: bool | None = None,
) -> dict:
    """Called after a payment succeeds (see payments/service.py) - saving a
    contact is a side effect of paying someone, not a separate flow, per the
    "keep the contacts after entering each other's iban and data" ask.

    `website`/`is_subscription` only overwrite an existing row when
    explicitly given (non-None) - a plain payment (which never collects
    either) re-upserting an existing beneficiary must not silently wipe
    what was set earlier via the standalone add-beneficiary flow, and must
    never accidentally mark a real person a subscription (or vice versa)
    just because they got paid again."""
    row: dict = {"user_id": str(user_id), "iban": iban, "display_name": display_name}
    if website is not None:
        row["website"] = website
    if is_subscription is not None:
        row["is_subscription"] = is_subscription
    resp = (
        await supabase.table("beneficiaries")
        .upsert(row, on_conflict="user_id,iban")
        .execute()
    )
    return resp.data[0]


async def add_beneficiary_for_owner(
    supabase: AsyncClient,
    user_id: str,
    iban: str,
    display_name: str,
    website: str | None = None,
    is_subscription: bool = False,
) -> dict:
    """The standalone "add a contact" entry point, as opposed to
    `upsert_beneficiary`'s payment side-effect path above. Same underlying
    upsert (re-adding an existing IBAN just updates its display name rather
    than erroring), but validates the IBAN itself first - a payment already
    proved its `to_iban` resolves to a real account, this path hasn't.

    `is_subscription` always writes here (unlike the payment side-effect
    path) - this is the one flow that deliberately asks the user to
    classify a contact, so an unchecked box is a real "no," not "unset"."""
    cleaned_iban = iban.replace(" ", "").upper()
    if not validate_iban(cleaned_iban):
        raise ValidationError("That doesn't look like a valid IBAN.")
    return await upsert_beneficiary(
        supabase,
        user_id=uuid.UUID(user_id),
        iban=cleaned_iban,
        display_name=display_name,
        website=website,
        is_subscription=is_subscription,
    )


async def add_beneficiary(
    supabase: AsyncClient,
    user: UserRead,
    iban: str,
    display_name: str,
    website: str | None = None,
    is_subscription: bool = False,
) -> dict:
    return await add_beneficiary_for_owner(
        supabase, str(user.id), iban, display_name, website, is_subscription
    )


async def _remove_beneficiary(
    supabase: AsyncClient, user_id: str, beneficiary_id: uuid.UUID
) -> None:
    resp = (
        await supabase.table("beneficiaries")
        .delete()
        .eq("id", str(beneficiary_id))
        .eq("user_id", user_id)
        .execute()
    )
    if not resp.data:
        raise NotFoundError("Beneficiary not found.")


async def remove_beneficiary(
    supabase: AsyncClient, user: UserRead, beneficiary_id: uuid.UUID
) -> None:
    await _remove_beneficiary(supabase, str(user.id), beneficiary_id)


async def remove_beneficiary_for_owner(
    supabase: AsyncClient, user_id: str, beneficiary_id: uuid.UUID
) -> None:
    await _remove_beneficiary(supabase, user_id, beneficiary_id)


async def find_beneficiary_id_by_iban_for_owner(
    supabase: AsyncClient, user_id: str, iban: str
) -> uuid.UUID:
    cleaned_iban = iban.replace(" ", "").upper()
    resp = (
        await supabase.table("beneficiaries")
        .select("id")
        .eq("user_id", user_id)
        .eq("iban", cleaned_iban)
        .maybe_single()
        .execute()
    )
    row = resp.data if resp is not None else None
    if row is None:
        raise NotFoundError("Beneficiary not found.")
    return uuid.UUID(row["id"])


async def get_beneficiary_by_iban(
    supabase: AsyncClient, user_id: uuid.UUID, iban: str
) -> dict | None:
    """Lookup for the subscription-price-increase check (see
    payments/service.py::_detect_subscription_price_increase) - None when
    the IBAN isn't saved as a contact at all. `is_subscription` is what
    actually gates that check; `website` just enriches the message once it
    fires."""
    resp = (
        await supabase.table("beneficiaries")
        .select("website, is_subscription")
        .eq("user_id", str(user_id))
        .eq("iban", iban)
        .maybe_single()
        .execute()
    )
    return resp.data if resp is not None else None


async def list_beneficiaries_for_owner(supabase: AsyncClient, user_id: str) -> list[dict]:
    """Bare-user_id variant, for callers holding a `Context` rather than a
    full `UserRead` - the AI tool layer (see app/ai/tools/banking/find_
    beneficiary.py), same convention as transactions/service.py's `_for_
    owner` split."""
    resp = (
        await supabase.table("beneficiaries")
        .select("*")
        .eq("user_id", user_id)
        .order("display_name")
        .execute()
    )
    return resp.data


async def list_beneficiaries(supabase: AsyncClient, user: UserRead) -> list[dict]:
    return await list_beneficiaries_for_owner(supabase, str(user.id))
