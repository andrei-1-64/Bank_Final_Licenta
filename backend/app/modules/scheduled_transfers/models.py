import enum


class ScheduledTransferFrequency(enum.StrEnum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class ScheduledTransferStatus(enum.StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
