import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class StatementRow(BaseModel):
    """One extracted line from a bank statement - EXTRACTED, UNVERIFIED, and
    never linked to a ledger row (see statements/service.py's module
    docstring). `amount` is signed: positive is a credit (money in),
    negative is a debit (money out)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    posted_date: date | None
    description: str
    amount: float
    currency: str
    balance_after: float | None
    row_index: int
    extracted_category: str | None


class StatementSummary(BaseModel):
    """A statement's metadata, without its rows - the list-view shape."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    conversation_id: uuid.UUID | None
    document_id: uuid.UUID | None
    bank_name: str | None
    period_start: date | None
    period_end: date | None
    currency: str
    opening_balance: float | None
    closing_balance: float | None
    row_count: int


class StatementDetail(StatementSummary):
    """A statement with its extracted rows - the single-item read shape."""

    rows: list[StatementRow]


class StatementUploadResponse(BaseModel):
    statement: StatementSummary
    #: Echoed back explicitly, same reasoning as DocumentUploadResponse -
    #: the caller may not have supplied one (a new conversation is created
    #: when it doesn't).
    conversation_id: uuid.UUID
    row_count: int
    #: Romanian, user-facing: extracted rows are unverified and never reach
    #: the ledger - see statements/service.py's module docstring.
    note: str
