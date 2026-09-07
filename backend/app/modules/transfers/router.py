import uuid

from fastapi import APIRouter, Depends
from supabase import AsyncClient

from app.core.dependencies import get_current_user
from app.core.idempotency import require_idempotency_key
from app.db.supabase_client import get_supabase
from app.modules.face_auth.router import (
    optional_face_confirmation_token,
    optional_step_up_password,
)
from app.modules.transfers import service
from app.modules.transfers.schemas import TransferCreate, TransferRead
from app.modules.users.schemas import UserRead

router = APIRouter()


@router.post("", response_model=TransferRead, status_code=201)
async def create_transfer(
    payload: TransferCreate,
    idempotency_key: str = Depends(require_idempotency_key),
    face_token: str | None = Depends(optional_face_confirmation_token),
    step_up_password: str | None = Depends(optional_step_up_password),
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> dict:
    return await service.create_transfer(
        supabase, user, payload, idempotency_key, face_token, password=step_up_password
    )


@router.get("", response_model=list[TransferRead])
async def list_transfers(
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> list[dict]:
    return await service.list_transfers(supabase, user)


@router.get("/{transfer_id}", response_model=TransferRead)
async def get_transfer(
    transfer_id: uuid.UUID,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> dict:
    return await service.get_transfer(supabase, user, transfer_id)
