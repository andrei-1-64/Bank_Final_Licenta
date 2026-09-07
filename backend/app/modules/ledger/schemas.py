"""Input type for ledger.post_transaction(). A plain dataclass (not a
Pydantic model) since this is an internal service-to-service contract, not
something that comes off the wire - callers (transfers, and later
payments/cards/round_ups) build these after their own request validation."""

import uuid
from dataclasses import dataclass

from app.modules.ledger.models import LedgerDirection


@dataclass(frozen=True, slots=True)
class LedgerLeg:
    account_id: uuid.UUID
    direction: LedgerDirection
    amount_minor: int
    currency: str
