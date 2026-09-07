from decimal import Decimal

import pytest

from app.core.exceptions import CurrencyMismatchError, ValidationError
from app.core.money import Money, ensure_positive_minor, format_money, to_decimal, to_minor_units


def test_to_minor_units_basic():
    assert to_minor_units(Decimal("12.50"), "USD") == 1250
    assert to_minor_units("0.01", "eur") == 1
    assert to_minor_units(Decimal("100"), "USD") == 10000


def test_to_minor_units_rejects_float():
    with pytest.raises(ValidationError):
        to_minor_units(12.5, "USD")  # type: ignore[arg-type]


def test_to_decimal_roundtrip():
    assert to_decimal(1250, "USD") == Decimal("12.50")
    assert to_decimal(0, "USD") == Decimal("0.00")


def test_to_decimal_rejects_non_int():
    with pytest.raises(ValidationError):
        to_decimal(12.5, "USD")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        to_decimal(True, "USD")  # bool is an int subclass - must be rejected explicitly


def test_format_money():
    assert format_money(123456, "USD") == "1,234.56 USD"


def test_ensure_positive_minor():
    ensure_positive_minor(1)  # does not raise
    with pytest.raises(ValidationError):
        ensure_positive_minor(0)
    with pytest.raises(ValidationError):
        ensure_positive_minor(-1)
    with pytest.raises(ValidationError):
        ensure_positive_minor(1.5)  # type: ignore[arg-type]


def test_money_arithmetic():
    a = Money(1000, "usd")
    b = Money(250, "USD")
    assert a.currency == "USD"  # normalized to uppercase
    assert (a + b).amount_minor == 1250
    assert (a - b).amount_minor == 750
    assert b < a
    assert a >= a


def test_money_currency_mismatch_raises():
    usd = Money(100, "USD")
    eur = Money(100, "EUR")
    with pytest.raises(CurrencyMismatchError):
        usd + eur
    with pytest.raises(CurrencyMismatchError):
        usd < eur  # noqa: B015 - comparing is the point, we're asserting it raises


def test_money_zero_and_str():
    zero = Money.zero("USD")
    assert zero.amount_minor == 0
    assert str(Money(150000, "USD")) == "1,500.00 USD"
