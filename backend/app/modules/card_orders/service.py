from supabase import AsyncClient

from app.core.audit import record_audit_event
from app.core.exceptions import ValidationError
from app.modules.accounts import service as accounts_service
from app.modules.card_orders.schemas import CardOrderCreate
from app.modules.cards import service as cards_service
from app.modules.cards.models import CardStatus
from app.modules.cards.schemas import CardCreate
from app.modules.users.schemas import UserRead


async def _get_card_to_ship(
    supabase: AsyncClient, user: UserRead, payload: CardOrderCreate, account: dict
) -> dict:
    """Resolves which card a physical order actually ships: either an
    existing virtual card the user wants turned physical (payload.card_id),
    or - the pre-existing default - a brand-new one minted just for this
    order. Reusing an existing card keeps its real number/CVV/expiry rather
    than silently issuing a second, unrelated card the user never asked for."""
    if payload.card_id is None:
        return await cards_service.issue_card(supabase, user, CardCreate(account_id=account["id"]))

    card = await cards_service.get_card(supabase, user, payload.card_id)
    if card["account_id"] != str(account["id"]):
        raise ValidationError("The card doesn't belong to the selected account.")
    if card["status"] == CardStatus.CANCELLED.value:
        raise ValidationError("Cannot order a physical card for a cancelled card.")

    existing_order = (
        await supabase.table("card_orders")
        .select("id")
        .eq("card_id", str(card["id"]))
        .maybe_single()
        .execute()
    )
    if existing_order is not None and existing_order.data is not None:
        raise ValidationError("This card already has a physical card order.")

    return card


async def create_order(supabase: AsyncClient, user: UserRead, payload: CardOrderCreate) -> dict:
    account = await accounts_service.get_account(supabase, user, payload.account_id)

    card = await _get_card_to_ship(supabase, user, payload, account)

    resp = (
        await supabase.table("card_orders")
        .insert(
            {
                "account_id": str(account["id"]),
                "card_id": card["id"],
                "full_name": payload.full_name,
                "phone": payload.phone,
                "address": payload.address,
                "city": payload.city,
                "postal_code": payload.postal_code,
                "country": payload.country,
            }
        )
        .execute()
    )
    order = resp.data[0]
    order["card"] = card

    await record_audit_event(
        supabase,
        user_id=user.id,
        action="card_orders.create",
        entity=f"card_orders:{order['id']}",
        metadata={"account_id": str(account["id"])},
    )
    return order


async def list_orders(supabase: AsyncClient, user: UserRead) -> list[dict]:
    # Safe two-call fallback instead of relying on PostgREST's embedded-
    # filter syntax (unstable across versions) - not a hot/concurrent path.
    # The card:cards(*) embed is a straight FK-based join, not this same
    # instability, so it's kept for the nested card details.
    accounts_resp = (
        await supabase.table("accounts").select("id").eq("user_id", str(user.id)).execute()
    )
    account_ids = [row["id"] for row in accounts_resp.data]
    if not account_ids:
        return []

    resp = (
        await supabase.table("card_orders")
        .select("*, card:cards(*)")
        .in_("account_id", account_ids)
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data
