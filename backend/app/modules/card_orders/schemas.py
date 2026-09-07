import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.card_orders.models import CardOrderStatus
from app.modules.cards.schemas import CardRead


class CardOrderCreate(BaseModel):
    account_id: uuid.UUID
    #: An existing (non-cancelled) virtual card on `account_id` to ship
    #: physically - reuses its number/CVV/expiry instead of minting a new
    #: card. Omit to order a brand-new separate physical card, as before.
    card_id: uuid.UUID | None = None
    full_name: str = Field(..., min_length=1, max_length=200)
    phone: str = Field(..., min_length=1, max_length=20)
    address: str = Field(..., min_length=1, max_length=255)
    city: str = Field(..., min_length=1, max_length=100)
    postal_code: str = Field(..., min_length=1, max_length=20)
    country: str = Field(..., min_length=1, max_length=100)


class CardOrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    card_id: uuid.UUID | None
    card: CardRead | None
    full_name: str
    phone: str
    address: str
    city: str
    postal_code: str
    country: str
    status: CardOrderStatus
    created_at: datetime
