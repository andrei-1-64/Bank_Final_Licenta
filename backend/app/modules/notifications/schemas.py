import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class NotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    body: str
    read_at: datetime | None
    created_at: datetime
    #: Stable event tag (e.g. "money_received") for callers that need to key
    #: off what happened rather than parse the free-text title/body - see
    #: notifications/service.py::create_notification. None for most rows.
    category: str | None = None


class UnreadCountRead(BaseModel):
    count: int
