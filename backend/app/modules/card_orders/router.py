from fastapi import APIRouter, Depends
from supabase import AsyncClient

from app.core.dependencies import get_current_user
from app.db.supabase_client import get_supabase
from app.modules.card_orders import service
from app.modules.card_orders.schemas import CardOrderCreate, CardOrderRead
from app.modules.users.schemas import UserRead

router = APIRouter()


@router.post("", response_model=CardOrderRead, status_code=201)
async def create_order(
    payload: CardOrderCreate,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> dict:
    return await service.create_order(supabase, user, payload)


@router.get("", response_model=list[CardOrderRead])
async def list_orders(
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> list[dict]:
    return await service.list_orders(supabase, user)
