import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.scheduled_transfers.models import ScheduledTransferFrequency, ScheduledTransferStatus


class ScheduledTransferCreate(BaseModel):
    from_account_id: uuid.UUID
    to_account_id: uuid.UUID
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    description: str | None = Field(default=None, max_length=500)
    #: Null means a one-time transfer that fires once at start_at, then is
    #: marked completed - never null AND recurring at the same time.
    frequency: ScheduledTransferFrequency | None = None
    #: When the first (or only) transfer becomes due. May be now or in the
    #: past - it just runs on the next lazy check instead of waiting.
    start_at: datetime


class ScheduledTransferRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_account_id: uuid.UUID
    to_account_id: uuid.UUID
    amount_minor: int
    currency: str
    description: str | None
    frequency: ScheduledTransferFrequency | None
    next_run_at: datetime
    status: ScheduledTransferStatus
    last_run_at: datetime | None
    last_error: str | None
    created_at: datetime
