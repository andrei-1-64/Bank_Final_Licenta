from app.modules.accounts.iban import BANK_CODE, COUNTRY_CODE, generate_iban
from app.modules.auth.validation import validate_iban


def test_generated_iban_is_valid():
    for _ in range(50):
        assert validate_iban(generate_iban())


def test_generated_iban_shape():
    iban = generate_iban()
    assert len(iban) == 24
    assert iban.startswith(COUNTRY_CODE)
    assert iban[4:8] == BANK_CODE


def test_generated_ibans_are_unique():
    ibans = {generate_iban() for _ in range(1000)}
    assert len(ibans) == 1000
