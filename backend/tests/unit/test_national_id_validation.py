from datetime import date

from app.modules.auth.validation import (
    extract_date_of_birth,
    extract_gender,
    generate_test_national_id,
    validate_iban,
    validate_national_id,
)


def test_valid_national_id_passes():
    national_id = generate_test_national_id(1995, 6, 15, county=1, gender="M")
    is_valid, reason = validate_national_id(national_id)
    assert is_valid is True
    assert reason == ""


def test_wrong_length_rejected():
    is_valid, reason = validate_national_id("123456789")
    assert is_valid is False
    assert "13 digits" in reason


def test_non_digit_rejected():
    is_valid, _ = validate_national_id("199506159A123")
    assert is_valid is False


def test_bad_month_rejected():
    valid = generate_test_national_id(1995, 6, 15, gender="M")
    tampered = valid[:3] + "13" + valid[5:]  # month = 13
    is_valid, reason = validate_national_id(tampered)
    assert is_valid is False
    assert "month" in reason


def test_bad_day_rejected():
    valid = generate_test_national_id(1995, 6, 15, gender="M")
    tampered = valid[:5] + "32" + valid[7:]  # day = 32
    is_valid, reason = validate_national_id(tampered)
    assert is_valid is False
    assert "day" in reason


def test_bad_county_rejected():
    valid = generate_test_national_id(1995, 6, 15, gender="M")
    tampered = valid[:7] + "99" + valid[9:]  # county = 99
    is_valid, reason = validate_national_id(tampered)
    assert is_valid is False
    assert "county" in reason


def test_wrong_check_digit_is_still_accepted():
    # Deliberate: the official MOD-11 checksum is NOT enforced (see
    # validate_national_id's docstring) - this app never goes to
    # production, and a specimen/placeholder CNP (the checksum digit is
    # the only "wrong" part) should still work for testing.
    valid = generate_test_national_id(1995, 6, 15, gender="M")
    last_digit = "0" if valid[-1] != "0" else "1"
    tampered = valid[:-1] + last_digit
    is_valid, reason = validate_national_id(tampered)
    assert is_valid is True
    assert reason == ""


def test_extract_gender():
    male_id = generate_test_national_id(1995, 6, 15, gender="M")
    female_id = generate_test_national_id(1995, 6, 15, gender="F")
    assert extract_gender(male_id) == "M"
    assert extract_gender(female_id) == "F"


def test_extract_date_of_birth_by_century():
    assert extract_date_of_birth(generate_test_national_id(1995, 6, 15, gender="M")) == date(
        1995, 6, 15
    )
    assert extract_date_of_birth(generate_test_national_id(1885, 6, 15, gender="M")) == date(
        1885, 6, 15
    )
    assert extract_date_of_birth(generate_test_national_id(2010, 6, 15, gender="M")) == date(
        2010, 6, 15
    )


def test_valid_iban_passes():
    assert validate_iban("GB82WEST12345698765432") is True
    assert validate_iban("gb82 west1234 5698 7654 32") is True  # case/whitespace insensitive


def test_invalid_iban_rejected():
    assert validate_iban("GB82WEST12345698765431") is False  # tampered check digits
    assert validate_iban("TOO_SHORT") is False
    assert validate_iban("") is False
