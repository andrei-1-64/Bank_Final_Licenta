from app.modules.cards.card_numbers import generate_card_number, luhn_is_valid


def test_generated_number_is_luhn_valid():
    for _ in range(50):
        assert luhn_is_valid(generate_card_number())


def test_generated_number_shape():
    number = generate_card_number()
    assert len(number) == 16
    assert number.isdigit()
    assert number.startswith("4")


def test_generate_card_number_respects_prefix_and_length():
    number = generate_card_number(prefix="55", length=19)
    assert len(number) == 19
    assert number.startswith("55")
    assert luhn_is_valid(number)


def test_known_valid_luhn_numbers():
    # Well-known publicly documented test PANs (never real accounts).
    assert luhn_is_valid("4532015112830366") is True
    assert luhn_is_valid("79927398713") is True


def test_known_invalid_luhn_numbers():
    assert luhn_is_valid("4532015112830367") is False
    assert luhn_is_valid("79927398710") is False


def test_luhn_is_valid_rejects_non_digits():
    assert luhn_is_valid("453201511283036A") is False
    assert luhn_is_valid("") is False
