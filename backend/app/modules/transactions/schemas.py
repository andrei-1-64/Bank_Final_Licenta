import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.modules.ledger.models import LedgerDirection


class TransactionEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    journal_id: uuid.UUID
    account_id: uuid.UUID
    direction: LedgerDirection
    amount_minor: int
    currency: str
    description: str
    reference: str
    created_at: datetime
