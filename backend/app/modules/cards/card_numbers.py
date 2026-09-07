"""Fictitious card number/expiry/CVV generation. These never correspond to
a real payment card - there's no real card network involved anywhere in
this app - but the number is structurally valid: 16 digits, correct Luhn
check digit, so it behaves like a real PAN for anything that validates the
shape.

The full number, expiry, and CVV are persisted on the Card row (see
modules/cards/models.py) and stay viewable from the cards list - a
deliberate departure from real-world practice (which never stores a CVV
and never re-shows a full PAN after issuance), made because these are
fake numbers with no real card network behind them.
"""

import datetime
import secrets

EXPIRY_YEARS_VALID = 4

# A Visa-like prefix, purely cosmetic (matches the "VISA" branding already
# in the frontend mockup) - doesn't correspond to a real BIN range.
DEFAULT_PREFIX = "4"
DEFAULT_LENGTH = 16


def luhn_is_valid(number: str) -> bool:
    if not number.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(number)):
        digit = int(ch)
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _luhn_check_digit(partial_number: str) -> int:
    """The digit to append to partial_number so the resulting full number
    passes luhn_is_valid()."""
    total = 0
    for i, ch in enumerate(reversed(partial_number)):
        digit = int(ch)
        if i % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return (10 - (total % 10)) % 10


def generate_card_number(*, prefix: str = DEFAULT_PREFIX, length: int = DEFAULT_LENGTH) -> str:
    if not prefix.isdigit():
        raise ValueError("prefix must be numeric")
    body_length = length - len(prefix) - 1  # -1 for the check digit
    if body_length < 0:
        raise ValueError("length too short for the given prefix")
    body = "".join(str(secrets.randbelow(10)) for _ in range(body_length))
    partial = prefix + body
    return partial + str(_luhn_check_digit(partial))


def generate_expiry(*, today: datetime.date | None = None) -> tuple[int, int]:
    """Returns (month, year), EXPIRY_YEARS_VALID years out from today."""
    today = today or datetime.date.today()
    return today.month, today.year + EXPIRY_YEARS_VALID


def generate_cvv() -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(3))
