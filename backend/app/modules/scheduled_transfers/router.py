import uuid

from fastapi import APIRouter, Depends
from supabase import AsyncClient

from app.core.dependencies import get_current_user
from app.db.supabase_client import get_supabase
from app.modules.scheduled_transfers import service
from app.modules.scheduled_transfers.schemas import ScheduledTransferCreate, ScheduledTransferRead
from app.modules.users.schemas import UserRead

router = APIRouter()


@router.post("", response_model=ScheduledTransferRead, status_code=201)
async def create_scheduled_transfer(
    payload: ScheduledTransferCreate,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> dict:
    return await service.create_scheduled_transfer(supabase, user, payload)


@router.get("", response_model=list[ScheduledTransferRead])
async def list_scheduled_transfers(
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> list[dict]:
    return await service.list_scheduled_transfers(supabase, user)


@router.post("/{scheduled_id}/cancel", response_model=ScheduledTransferRead)
async def cancel_scheduled_transfer(
    scheduled_id: uuid.UUID,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> dict:
    return await service.cancel_scheduled_transfer(supabase, user, scheduled_id)


@router.post("/{scheduled_id}/pause", response_model=ScheduledTransferRead)
async def pause_scheduled_transfer(
    scheduled_id: uuid.UUID,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> dict:
    return await service.pause_scheduled_transfer(supabase, user, scheduled_id)


@router.post("/{scheduled_id}/resume", response_model=ScheduledTransferRead)
async def resume_scheduled_transfer(
    scheduled_id: uuid.UUID,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> dict:
    return await service.resume_scheduled_transfer(supabase, user, scheduled_id)
