"""COPIED FROM backend/app/modules/auth/validation.py - keep the two in sync.

Pure functions over strings (CNP checksum, IBAN MOD-97, date/gender decoding),
no imports beyond the standard library. Duplicated rather than shared because
the alternative - publishing a `bank-common` package that both images install -
buys little for 148 lines that have not changed since they were written, and
costs a build step in two Dockerfiles.

The backend needs them for registration validation; this service needs them
during extraction, to tell a real CNP/IBAN apart from OCR noise. If the
checksum rules ever change, change them in BOTH places.
"""

from datetime import date

# Official weights used to compute the CNP check digit (MOD 11).
_CHECK_DIGIT_WEIGHTS = "279146358279"

# County codes 01-46 (counties) + 51-52 (Bucharest sectors) + 47 (non-
# residents) are all treated generically as the 01-52 range.
_COUNTY_CODE_MIN = 1
_COUNTY_CODE_MAX = 52

# Days per month (1-indexed); February uses 29 so leap years are never
# rejected (strict leap-year validation isn't required here).
_DAYS_IN_MONTH = [0, 31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def validate_national_id(national_id: str) -> tuple[bool, str]:
    """Validates the structure of a Romanian CNP - shape and the parts that
    feed extract_date_of_birth/extract_gender (first digit, month, day,
    county), NOT the official MOD-11 check digit.

    This app never goes to production and nobody testing it should have to
    go find a real person's CNP (or hand-compute a checksum) just to
    register - see generate_test_national_id below for the one place a
    real checksum still gets computed, purely so its own output is
    internally consistent. A card with a placeholder/specimen CNP (the
    checksum digit is the only part that's "wrong") should still work here.
    Returns (True, "") if valid, otherwise (False, reason)."""
    if not national_id or not isinstance(national_id, str):
        return False, "National ID is required"

    if len(national_id) != 13 or not national_id.isdigit():
        return False, "National ID must be exactly 13 digits"

    first_digit = int(national_id[0])
    if first_digit < 1 or first_digit > 9:
        return False, "National ID's first digit is invalid"

    month = int(national_id[3:5])
    if month < 1 or month > 12:
        return False, "National ID's birth month is invalid"

    day = int(national_id[5:7])
    if day < 1 or day > _DAYS_IN_MONTH[month]:
        return False, "National ID's birth day is invalid"

    county = int(national_id[7:9])
    if county < _COUNTY_CODE_MIN or county > _COUNTY_CODE_MAX:
        return False, "National ID's county code is invalid"

    return True, ""


def _compute_check_digit(national_id: str) -> int:
    """Computes the official check digit (MOD 11 algorithm)."""
    total = sum(int(national_id[i]) * int(_CHECK_DIGIT_WEIGHTS[i]) for i in range(12))
    remainder = total % 11
    return 0 if remainder == 10 else remainder


def extract_date_of_birth(national_id: str) -> date:
    """Determines the full birth date from a valid national ID, accounting
    for century (first digit: 1/2 -> 1900, 3/4 -> 1800, 5/6 -> 2000)."""
    first_digit = int(national_id[0])
    short_year = int(national_id[1:3])
    month = int(national_id[3:5])
    day = int(national_id[5:7])

    if first_digit in (1, 2):
        century = 1900
    elif first_digit in (3, 4):
        century = 1800
    elif first_digit in (5, 6):
        century = 2000
    else:
        # 7/8/9 = foreign residents; assume the current century as a
        # reasonable fallback.
        century = 2000

    return date(century + short_year, month, day)


def extract_gender(national_id: str) -> str:
    """Odd first digit (1,3,5,7,9) = M, even (2,4,6,8) = F."""
    first_digit = int(national_id[0])
    return "M" if first_digit % 2 == 1 else "F"


def generate_test_national_id(
    year: int, month: int, day: int, county: int = 1, gender: str = "M", sequence: int = 1
) -> str:
    """Generates a structurally valid CNP for test purposes. Does not
    correspond to any real person - the sequence number is caller-chosen."""
    century_digit = _century_digit_from_year_and_gender(year, gender)
    if century_digit is None:
        raise ValueError("Year/gender combination not covered by the test generator")

    short_year = year % 100

    national_id_without_check_digit = (
        f"{century_digit}{short_year:02d}{month:02d}{day:02d}{county:02d}{sequence:03d}"
    )

    check_digit = _compute_check_digit(national_id_without_check_digit + "0")
    return national_id_without_check_digit + str(check_digit)


def _century_digit_from_year_and_gender(year: int, gender: str) -> int | None:
    gender = gender.upper()
    if 1900 <= year <= 1999:
        return 1 if gender == "M" else 2
    if 1800 <= year <= 1899:
        return 3 if gender == "M" else 4
    if 2000 <= year <= 2099:
        return 5 if gender == "M" else 6
    return None


def validate_iban(iban: str) -> bool:
    """Validates an IBAN using the standard MOD 97 algorithm. General
    utility, not currently used by the auth flow - kept for a future
    beneficiaries/payments feature that needs to validate account numbers.
    """
    if not iban or not isinstance(iban, str):
        return False

    cleaned = iban.replace(" ", "").upper()

    if len(cleaned) < 15 or len(cleaned) > 34:
        return False

    if not cleaned.isalnum():
        return False

    # Move the first 4 characters to the end.
    rearranged = cleaned[4:] + cleaned[:4]

    # Convert letters to numbers: A=10, B=11, ..., Z=35.
    numeric = "".join(str(int(char, 36)) if char.isalpha() else char for char in rearranged)

    return int(numeric) % 97 == 1
