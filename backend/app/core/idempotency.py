"""Idempotency-Key guard, shared by every money-moving endpoint (transfers
here; payments/cards/etc. in [B]'s modules).

This module only validates that the header is present and well-formed. The
actual dedupe guarantee is enforced where it matters - at the database level
via the unique constraint on journal_transactions.idempotency_key, applied
inside ledger.post_transaction() - because that's the only place that's safe
against two concurrent requests racing each other. See
app/modules/ledger/service.py for the double-checked-locking pattern that
uses this.
"""

from fastapi import Header

from app.core.exceptions import InvalidIdempotencyKeyError

IDEMPOTENCY_HEADER_NAME = "Idempotency-Key"
MAX_KEY_LENGTH = 255


async def require_idempotency_key(
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER_NAME),
) -> str:
    """FastAPI dependency: use as
    `idempotency_key: str = Depends(require_idempotency_key)`
    on any endpoint that moves money."""
    if idempotency_key is None or not idempotency_key.strip():
        raise InvalidIdempotencyKeyError(
            f"The '{IDEMPOTENCY_HEADER_NAME}' header is required for this request."
        )
    if len(idempotency_key) > MAX_KEY_LENGTH:
        raise InvalidIdempotencyKeyError(
            f"'{IDEMPOTENCY_HEADER_NAME}' must be at most {MAX_KEY_LENGTH} characters."
        )
    return idempotency_key
