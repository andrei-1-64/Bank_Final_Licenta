import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BeneficiaryCreate(BaseModel):
    iban: str = Field(..., min_length=15, max_length=34)
    display_name: str = Field(..., min_length=1, max_length=200)
    #: Optional - e.g. a subscription merchant's site, so the
    #: subscription-price-increase warning (see payments/service.py) can
    #: point the user somewhere concrete to cancel instead of just blocking.
    website: str | None = Field(default=None, max_length=255)
    #: Gates the subscription-price-increase check - only a contact
    #: explicitly marked this way can ever trigger it, so a friend paid a
    #: recurring amount twice is never mistaken for a subscription.
    is_subscription: bool = False


class BeneficiaryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    iban: str
    display_name: str
    website: str | None = None
    is_subscription: bool = False
    created_at: datetime
