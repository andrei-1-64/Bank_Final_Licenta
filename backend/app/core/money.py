"""Minor-units money helpers.

Non-negotiable: amounts are always integer minor units (e.g. cents), never
float. Decimal is only used at the edges (parsing/formatting for humans).
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.core.exceptions import CurrencyMismatchError, ValidationError

# Number of minor-unit decimal places per currency. Anything not listed here
# defaults to 2 (covers USD/EUR/GBP/RON/... which is everything this app
# currently supports).
_MINOR_UNIT_EXPONENTS: dict[str, int] = {}
_DEFAULT_EXPONENT = 2


def _exponent(currency: str) -> int:
    return _MINOR_UNIT_EXPONENTS.get(currency.upper(), _DEFAULT_EXPONENT)


def to_minor_units(amount: Decimal | str, currency: str) -> int:
    """Convert a human amount ("12.50") to integer minor units (1250).

    Rejects float on purpose: floats cannot represent most decimal amounts
    exactly, which is exactly what rule #1 forbids for money.
    """
    if isinstance(amount, float):
        raise ValidationError("Amounts must be Decimal or str, not float.")
    try:
        decimal_amount = Decimal(amount)
    except Exception as exc:  # noqa: BLE001 - re-raised as a typed AppError
        raise ValidationError(f"Invalid amount: {amount!r}") from exc

    exponent = _exponent(currency)
    scaled = (decimal_amount * (10**exponent)).to_integral_value(rounding=ROUND_HALF_UP)
    return int(scaled)


def to_decimal(amount_minor: int, currency: str) -> Decimal:
    """Convert integer minor units (1250) back to a human amount (12.50)."""
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool):
        raise ValidationError("amount_minor must be an int.")
    exponent = _exponent(currency)
    return Decimal(amount_minor).scaleb(-exponent)


def format_money(amount_minor: int, currency: str) -> str:
    exponent = _exponent(currency)
    return f"{to_decimal(amount_minor, currency):,.{exponent}f} {currency.upper()}"


def ensure_positive_minor(amount_minor: int, *, field: str = "amount_minor") -> None:
    """Guard for movement amounts (ledger legs, transfer amounts, ...) which
    must always be a strictly positive integer; direction/sign is encoded
    separately (debit/credit), never via a negative amount."""
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool):
        raise ValidationError(f"{field} must be an integer number of minor units.")
    if amount_minor <= 0:
        raise ValidationError(f"{field} must be a positive amount.")


@dataclass(frozen=True, slots=True)
class Money:
    """A currency-tagged integer amount. Arithmetic/comparison enforce that
    both sides share a currency, raising CurrencyMismatchError otherwise."""

    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount_minor, int) or isinstance(self.amount_minor, bool):
            raise ValidationError("amount_minor must be an int.")
        object.__setattr__(self, "currency", self.currency.upper())

    @classmethod
    def zero(cls, currency: str) -> "Money":
        return cls(0, currency)

    def _check_currency(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(f"Cannot combine {self.currency} with {other.currency}.")

    def __add__(self, other: "Money") -> "Money":
        self._check_currency(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._check_currency(other)
        return Money(self.amount_minor - other.amount_minor, self.currency)

    def __lt__(self, other: "Money") -> bool:
        self._check_currency(other)
        return self.amount_minor < other.amount_minor

    def __le__(self, other: "Money") -> bool:
        self._check_currency(other)
        return self.amount_minor <= other.amount_minor

    def __gt__(self, other: "Money") -> bool:
        self._check_currency(other)
        return self.amount_minor > other.amount_minor

    def __ge__(self, other: "Money") -> bool:
        self._check_currency(other)
        return self.amount_minor >= other.amount_minor

    def as_decimal(self) -> Decimal:
        return to_decimal(self.amount_minor, self.currency)

    def __str__(self) -> str:
        return format_money(self.amount_minor, self.currency)
