import uuid

from supabase import AsyncClient

from app.core.audit import record_audit_event
from app.core.exceptions import AccountClosedError, NotFoundError, ValidationError
from app.modules.accounts import service as accounts_service
from app.modules.accounts.models import AccountStatus
from app.modules.cards.card_numbers import generate_card_number, generate_cvv, generate_expiry
from app.modules.cards.models import CardStatus
from app.modules.cards.schemas import CardCreate
from app.modules.users.schemas import UserRead


async def issue_card(supabase: AsyncClient, user: UserRead, payload: CardCreate) -> dict:
    account = await accounts_service.get_account(supabase, user, payload.account_id)
    if account["status"] != AccountStatus.ACTIVE.value:
        raise AccountClosedError("Cannot issue a card for a closed account.")

    card_number = generate_card_number()
    expiry_month, expiry_year = generate_expiry()
    resp = (
        await supabase.table("cards")
        .insert(
            {
                "account_id": str(account["id"]),
                "card_number": card_number,
                "last4": card_number[-4:],
                "expiry_month": expiry_month,
                "expiry_year": expiry_year,
                "cvv": generate_cvv(),
                "status": CardStatus.ACTIVE.value,
                "spending_limit_minor": payload.spending_limit_minor,
            }
        )
        .execute()
    )
    card = resp.data[0]

    await record_audit_event(
        supabase,
        user_id=user.id,
        action="cards.issue",
        entity=f"cards:{card['id']}",
        metadata={"account_id": str(account["id"])},
    )
    return card


async def list_cards_for_owner(supabase: AsyncClient, user_id: uuid.UUID | str) -> list[dict]:
    """Card list for callers holding a bare user id (the AI layer's `Context`),
    mirroring `accounts_service.get_account_for_owner`. `list_cards` below is
    the same read for the banking modules, which have a full `UserRead`."""
    # Safe two-call fallback instead of relying on PostgREST's embedded-
    # filter syntax (unstable across versions) - not a hot/concurrent path.
    accounts_resp = (
        await supabase.table("accounts").select("id").eq("user_id", str(user_id)).execute()
    )
    account_ids = [row["id"] for row in accounts_resp.data]
    if not account_ids:
        return []

    resp = (
        await supabase.table("cards")
        .select("*")
        .in_("account_id", account_ids)
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data


async def list_cards(supabase: AsyncClient, user: UserRead) -> list[dict]:
    return await list_cards_for_owner(supabase, user.id)


async def get_card(supabase: AsyncClient, user: UserRead, card_id: uuid.UUID) -> dict:
    """Public entry point for callers outside this module that need a single
    owned card by id (e.g. card_orders/service.py turning an existing
    virtual card into a physical one) - thin wrapper over the same
    ownership check every other card-touching function here uses."""
    return await _get_owned_card(supabase, str(user.id), card_id)


async def _get_owned_card(supabase: AsyncClient, user_id: str, card_id: uuid.UUID) -> dict:
    """Shared ownership check: a card has no `user_id` column of its own, so
    ownership is proven transitively through the account it's attached to -
    same shape as every other card-touching function in this module."""
    resp = (
        await supabase.table("cards").select("*").eq("id", str(card_id)).maybe_single().execute()
    )
    card = resp.data if resp is not None else None
    if card is None:
        raise NotFoundError("Card not found.")

    account_resp = (
        await supabase.table("accounts")
        .select("id")
        .eq("id", card["account_id"])
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if account_resp is None or account_resp.data is None:
        raise NotFoundError("Card not found.")

    return card


async def cancel_card(supabase: AsyncClient, user: UserRead, card_id: uuid.UUID) -> dict:
    card = await _get_owned_card(supabase, str(user.id), card_id)

    if card["status"] != CardStatus.CANCELLED.value:
        update_resp = (
            await supabase.table("cards")
            .update({"status": CardStatus.CANCELLED.value})
            .eq("id", str(card_id))
            .execute()
        )
        card = update_resp.data[0]
        await record_audit_event(
            supabase, user_id=user.id, action="cards.cancel", entity=f"cards:{card_id}"
        )

    return card


async def _set_frozen(
    supabase: AsyncClient, user_id: str, card_id: uuid.UUID, *, frozen: bool
) -> dict:
    card = await _get_owned_card(supabase, user_id, card_id)

    if card["status"] == CardStatus.CANCELLED.value:
        raise ValidationError("Cannot freeze or unfreeze a cancelled card.")

    target_status = CardStatus.FROZEN.value if frozen else CardStatus.ACTIVE.value
    if card["status"] != target_status:
        update_resp = (
            await supabase.table("cards")
            .update({"status": target_status})
            .eq("id", str(card_id))
            .execute()
        )
        card = update_resp.data[0]
        await record_audit_event(
            supabase,
            user_id=uuid.UUID(user_id),
            action="cards.freeze" if frozen else "cards.unfreeze",
            entity=f"cards:{card_id}",
        )

    return card


async def freeze_card(supabase: AsyncClient, user: UserRead, card_id: uuid.UUID) -> dict:
    return await _set_frozen(supabase, str(user.id), card_id, frozen=True)


async def unfreeze_card(supabase: AsyncClient, user: UserRead, card_id: uuid.UUID) -> dict:
    return await _set_frozen(supabase, str(user.id), card_id, frozen=False)


async def freeze_card_for_owner(supabase: AsyncClient, user_id: str, card_id: uuid.UUID) -> dict:
    """AI-tool entry point: the caller only holds a bare `Context.user_id`,
    not a full `UserRead` - mirrors `list_cards_for_owner` alongside
    `list_cards`."""
    return await _set_frozen(supabase, user_id, card_id, frozen=True)


async def unfreeze_card_for_owner(supabase: AsyncClient, user_id: str, card_id: uuid.UUID) -> dict:
    return await _set_frozen(supabase, user_id, card_id, frozen=False)


async def _set_spending_limit(
    supabase: AsyncClient, user_id: str, card_id: uuid.UUID, spending_limit_minor: int | None
) -> dict:
    card = await _get_owned_card(supabase, user_id, card_id)

    if card["status"] == CardStatus.CANCELLED.value:
        raise ValidationError("Cannot change the spending limit of a cancelled card.")

    update_resp = (
        await supabase.table("cards")
        .update({"spending_limit_minor": spending_limit_minor})
        .eq("id", str(card_id))
        .execute()
    )
    card = update_resp.data[0]
    await record_audit_event(
        supabase,
        user_id=uuid.UUID(user_id),
        action="cards.set_spending_limit",
        entity=f"cards:{card_id}",
        metadata={"spending_limit_minor": spending_limit_minor},
    )
    return card


async def set_spending_limit(
    supabase: AsyncClient, user: UserRead, card_id: uuid.UUID, spending_limit_minor: int | None
) -> dict:
    return await _set_spending_limit(supabase, str(user.id), card_id, spending_limit_minor)


async def set_spending_limit_for_owner(
    supabase: AsyncClient, user_id: str, card_id: uuid.UUID, spending_limit_minor: int | None
) -> dict:
    return await _set_spending_limit(supabase, user_id, card_id, spending_limit_minor)


async def find_card_id_by_last4_for_owner(
    supabase: AsyncClient, user_id: str, last4: str
) -> uuid.UUID:
    """Resolves a model-friendly "card ending in 1234" reference to an actual
    id, scoped to this user's own cards. A cancelled card is excluded - it
    can no longer be frozen/unfrozen/have its limit changed, so it should
    never be what "the card ending in X" resolves to once another,
    still-live card shares those same last 4 digits."""
    cards = await list_cards_for_owner(supabase, user_id)
    matches = [
        card
        for card in cards
        if card["last4"] == last4 and card["status"] != CardStatus.CANCELLED.value
    ]
    if not matches:
        raise NotFoundError("No matching card found.")
    if len(matches) > 1:
        raise ValidationError(
            "Multiple cards match those last 4 digits; ask the user which account it's on."
        )
    return uuid.UUID(matches[0]["id"])
