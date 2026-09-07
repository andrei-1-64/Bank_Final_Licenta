import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.accounts.models import AccountStatus


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    currency: str = Field(min_length=3, max_length=3)
    #: "checking" (default), "savings", or "term_deposit" - see
    #: accounts/service.py's PRODUCT_* constants. Unknown values are
    #: rejected there, not here, so the error message can list what's valid.
    product_type: str = "checking"
    #: Required (and must be one of TERM_DEPOSIT_RATES_BPS's keys) only for
    #: product_type="term_deposit"; ignored otherwise.
    term_months: int | None = None


class AccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    currency: str
    iban: str | None
    status: AccountStatus
    balance_minor: int
    product_type: str
    interest_rate_bps: int | None
    term_months: int | None
    maturity_date: date | None
    created_at: datetime


class AccountHolderRead(BaseModel):
    first_name: str
    last_name: str


class TermDepositOption(BaseModel):
    term_months: int
    interest_rate_bps: int


class AccountProductsRead(BaseModel):
    """Powers the "Investiții & Bugetare" product picker - the frontend
    reads rates from here instead of hard-coding them a second time."""

    savings_interest_rate_bps: int
    term_deposit_options: list[TermDepositOption]
