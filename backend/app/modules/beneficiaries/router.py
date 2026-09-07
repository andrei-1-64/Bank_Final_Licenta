import uuid

from fastapi import APIRouter, Depends
from supabase import AsyncClient

from app.core.dependencies import get_current_user
from app.db.supabase_client import get_supabase
from app.modules.beneficiaries import service
from app.modules.beneficiaries.schemas import BeneficiaryCreate, BeneficiaryRead
from app.modules.users.schemas import UserRead

router = APIRouter()


@router.get("", response_model=list[BeneficiaryRead])
async def list_beneficiaries(
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> list[dict]:
    return await service.list_beneficiaries(supabase, user)


@router.post("", response_model=BeneficiaryRead, status_code=201)
async def add_beneficiary(
    payload: BeneficiaryCreate,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> dict:
    return await service.add_beneficiary(
        supabase, user, payload.iban, payload.display_name, payload.website, payload.is_subscription
    )


@router.delete("/{beneficiary_id}", status_code=204)
async def remove_beneficiary(
    beneficiary_id: uuid.UUID,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> None:
    await service.remove_beneficiary(supabase, user, beneficiary_id)
