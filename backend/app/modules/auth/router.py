from fastapi import APIRouter, Depends, Request, Response
from supabase import AsyncClient

from app.config import settings
from app.core.dependencies import get_current_user
from app.db.supabase_client import get_supabase
from app.modules.auth import service
from app.modules.auth.schemas import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    VerifyResetCodeRequest,
)
from app.modules.users.schemas import UserRead

router = APIRouter()


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        max_age=settings.SESSION_TTL_SECONDS,
    )


@router.post("/register", response_model=UserRead, status_code=201)
async def register(
    payload: RegisterRequest,
    response: Response,
    supabase: AsyncClient = Depends(get_supabase),
) -> UserRead:
    user, token = await service.register_user(supabase, payload)
    _set_session_cookie(response, token)
    return user


@router.post("/login", response_model=UserRead)
async def login(
    payload: LoginRequest,
    response: Response,
    supabase: AsyncClient = Depends(get_supabase),
) -> UserRead:
    user, token = await service.login_user(supabase, payload)
    _set_session_cookie(response, token)
    return user


@router.post("/forgot-password", status_code=204)
async def forgot_password(
    payload: ForgotPasswordRequest,
    supabase: AsyncClient = Depends(get_supabase),
) -> None:
    await service.request_password_reset(supabase, payload.email)


@router.post("/verify-reset-code", status_code=204)
async def verify_reset_code(
    payload: VerifyResetCodeRequest,
    supabase: AsyncClient = Depends(get_supabase),
) -> None:
    await service.verify_password_reset_code(supabase, payload.email, payload.code)


@router.post("/reset-password", status_code=204)
async def reset_password(
    payload: ResetPasswordRequest,
    supabase: AsyncClient = Depends(get_supabase),
) -> None:
    await service.reset_password(supabase, payload)


@router.post("/change-password", status_code=204)
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> None:
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    await service.change_password(supabase, user, token, payload)


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> None:
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    await service.logout_user(supabase, user, token)
    response.delete_cookie(settings.SESSION_COOKIE_NAME)
