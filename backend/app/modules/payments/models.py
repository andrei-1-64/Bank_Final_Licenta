import enum


class PaymentStatus(enum.StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
