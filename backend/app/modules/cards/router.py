import uuid

from fastapi import APIRouter, Depends
from supabase import AsyncClient

from app.core.dependencies import get_current_user
from app.db.supabase_client import get_supabase
from app.modules.cards import service
from app.modules.cards.schemas import CardCreate, CardRead, CardSpendingLimitUpdate
from app.modules.users.schemas import UserRead

router = APIRouter()


@router.post("", response_model=CardRead, status_code=201)
async def issue_card(
    payload: CardCreate,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> dict:
    return await service.issue_card(supabase, user, payload)


@router.get("", response_model=list[CardRead])
async def list_cards(
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> list[dict]:
    return await service.list_cards(supabase, user)


@router.delete("/{card_id}", response_model=CardRead)
async def cancel_card(
    card_id: uuid.UUID,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> dict:
    return await service.cancel_card(supabase, user, card_id)


@router.post("/{card_id}/freeze", response_model=CardRead)
async def freeze_card(
    card_id: uuid.UUID,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> dict:
    return await service.freeze_card(supabase, user, card_id)


@router.post("/{card_id}/unfreeze", response_model=CardRead)
async def unfreeze_card(
    card_id: uuid.UUID,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> dict:
    return await service.unfreeze_card(supabase, user, card_id)


@router.patch("/{card_id}/spending-limit", response_model=CardRead)
async def update_spending_limit(
    card_id: uuid.UUID,
    payload: CardSpendingLimitUpdate,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> dict:
    return await service.set_spending_limit(supabase, user, card_id, payload.spending_limit_minor)
