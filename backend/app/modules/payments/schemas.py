import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.payments.models import PaymentStatus


class PaymentCreate(BaseModel):
    from_account_id: uuid.UUID
    to_iban: str = Field(..., min_length=15, max_length=34)
    beneficiary_name: str = Field(..., min_length=1, max_length=200)
    amount_minor: int = Field(gt=0)
    description: str | None = Field(default=None, max_length=500)
    # Beneficiaries otherwise auto-save on every successful payment (see
    # beneficiaries_service.upsert_beneficiary) - this opts a one-off payment
    # out of that. True by default to keep existing behavior unchanged.
    save_beneficiary: bool = True
    # Set on a retry after the user has seen and accepted a
    # SubscriptionPriceIncreaseError (see payments/service.py) - bypasses
    # that check on this specific request. False by default: the check
    # runs on every first attempt.
    confirm_price_increase: bool = False


class PaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_account_id: uuid.UUID
    to_account_id: uuid.UUID
    to_iban: str
    amount_minor: int
    currency: str
    status: PaymentStatus
    created_at: datetime
