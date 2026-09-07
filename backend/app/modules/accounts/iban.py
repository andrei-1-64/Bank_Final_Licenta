"""Fictitious Romanian-format IBAN generation, one per account, so payments
(the upcoming beneficiaries/Plati feature) have something to send to. "BANK"
is a fictitious 4-letter bank code - no real bank behind it - but the check
digits are the real MOD-97 algorithm, so a generated IBAN passes
auth/validation.py::validate_iban, the same validator a real IBAN would.
"""

import secrets
import string

COUNTRY_CODE = "RO"
BANK_CODE = "BANK"
_ACCOUNT_ID_LENGTH = 16
_ACCOUNT_ID_ALPHABET = string.ascii_uppercase + string.digits


def _check_digits(bban: str, country_code: str) -> str:
    rearranged = bban + country_code + "00"
    numeric = "".join(str(int(ch, 36)) if ch.isalpha() else ch for ch in rearranged)
    remainder = int(numeric) % 97
    return f"{98 - remainder:02d}"


def generate_iban() -> str:
    account_id = "".join(secrets.choice(_ACCOUNT_ID_ALPHABET) for _ in range(_ACCOUNT_ID_LENGTH))
    bban = BANK_CODE + account_id
    return COUNTRY_CODE + _check_digits(bban, COUNTRY_CODE) + bban
