from pydantic import BaseModel, Field


class DeviceVerifyRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class DeviceCheckResponse(BaseModel):
    trusted: bool
    #: Only set when trusted - lets the login page greet the user and skip
    #: straight to a password-only prompt without them retyping their email.
    email: str | None = None
    #: Only meaningful when trusted - lets the login page try Face ID first
    #: (falling back to password) instead of always going straight to
    #: password on a recognized browser.
    face_enrolled: bool = False


class DeviceLoginRequest(BaseModel):
    password: str
