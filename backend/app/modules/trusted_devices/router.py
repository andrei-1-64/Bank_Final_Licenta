"""Device trust HTTP surface. Enrollment (/enroll/*) requires a session -
it only ever runs for a user who's already authenticated (typically right
after registering). /check and /login are deliberately unauthenticated:
they're what the login page calls BEFORE a session exists, to decide
whether to show the normal email+password form or a password-only one.
"""

from fastapi import APIRouter, Depends, Request, Response
from supabase import AsyncClient

from app.config import settings
from app.core.dependencies import get_current_user
from app.db.supabase_client import get_supabase
from app.modules.trusted_devices import service
from app.modules.trusted_devices.schemas import (
    DeviceCheckResponse,
    DeviceLoginRequest,
    DeviceVerifyRequest,
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


def _set_device_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.TRUSTED_DEVICE_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        max_age=settings.TRUSTED_DEVICE_TTL_SECONDS,
    )


@router.post("/enroll/request", status_code=204)
async def request_enrollment(
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> None:
    await service.request_device_enrollment(supabase, user)


@router.post("/enroll/verify", status_code=204)
async def verify_enrollment(
    payload: DeviceVerifyRequest,
    request: Request,
    response: Response,
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> None:
    label = request.headers.get("User-Agent", "")[:255] or None
    token = await service.verify_and_enroll_device(supabase, user, payload.code, label)
    _set_device_cookie(response, token)


@router.get("/check", response_model=DeviceCheckResponse)
async def check_device(
    request: Request,
    supabase: AsyncClient = Depends(get_supabase),
) -> DeviceCheckResponse:
    raw_token = request.cookies.get(settings.TRUSTED_DEVICE_COOKIE_NAME)
    result = await service.check_trusted_device(supabase, raw_token)
    if result is None:
        return DeviceCheckResponse(trusted=False)
    return DeviceCheckResponse(
        trusted=True, email=result["email"], face_enrolled=result["face_enrolled"]
    )


@router.post("/login", response_model=UserRead)
async def login_with_device(
    payload: DeviceLoginRequest,
    request: Request,
    response: Response,
    supabase: AsyncClient = Depends(get_supabase),
) -> UserRead:
    raw_token = request.cookies.get(settings.TRUSTED_DEVICE_COOKIE_NAME)
    user, token = await service.login_with_trusted_device(supabase, raw_token, payload.password)
    _set_session_cookie(response, token)
    return user
