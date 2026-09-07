import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    first_name: str
    last_name: str
    email_verified: bool
    national_id: str | None
    gender: str | None
    date_of_birth: date | None
    phone: str | None
    address: str | None
    referral_bonus_eligible: bool
    referral_code: str | None = None
    referred_by_user_id: uuid.UUID | None = None
    created_at: datetime
    #: Set by an admin (see app/modules/admin). Not-null means every
    #: authenticated request is refused - see core/dependencies.py. Defaults
    #: to None so a UserRead built from a row that predates migration 0017
    #: still validates.
    blocked_at: datetime | None = None


class UserUpdate(BaseModel):
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=20)
    address: str | None = Field(default=None, max_length=255)


class ReferralCodeRead(BaseModel):
    code: str
