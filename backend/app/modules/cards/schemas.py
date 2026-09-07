import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.cards.models import CardStatus


class CardCreate(BaseModel):
    account_id: uuid.UUID
    spending_limit_minor: int | None = Field(default=None, gt=0)


class CardSpendingLimitUpdate(BaseModel):
    #: None removes the limit entirely - same nullable semantics as
    #: CardCreate.spending_limit_minor above.
    spending_limit_minor: int | None = Field(default=None, gt=0)


class CardRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    card_number: str
    last4: str
    expiry_month: int
    expiry_year: int
    cvv: str
    status: CardStatus
    spending_limit_minor: int | None
    created_at: datetime
