from app.validation import generate_test_national_id
from app.id_card import _parse_fields


def _sample_card_text(cnp: str) -> str:
    return f"""ROMANIA
CARTE DE IDENTITATE
Seria RX Nr. 123456
CNP {cnp}
Nume/Nom
POPESCU
Prenume/Prenom
ANDREI ION
Cetatenie/Nationalite
ROU
Domiciliu/Adresse
STR EXEMPLU NR 1 BL A1 SC 1 AP 5 SECTOR 1 BUCURESTI
Valabilitate/Valable jusqu'au
15.06.2032
"""


def test_parses_all_labeled_fields():
    cnp = generate_test_national_id(1995, 6, 15, gender="F")
    fields = _parse_fields(_sample_card_text(cnp))

    assert fields["national_id"] == cnp
    assert fields["national_id_valid"] is True
    assert fields["last_name"] == "POPESCU"
    assert fields["first_name"] == "ANDREI ION"
    assert fields["address"].startswith("STR EXEMPLU")
    assert fields["series_number"] == "RX123456"


def test_derives_date_of_birth_and_gender_from_cnp():
    cnp = generate_test_national_id(1995, 6, 15, gender="F")
    fields = _parse_fields(_sample_card_text(cnp))

    assert fields["date_of_birth"] == "1995-06-15"
    assert fields["gender"] == "F"


def test_picks_valid_cnp_when_invalid_digit_noise_present():
    cnp = generate_test_national_id(1988, 11, 2, gender="M")
    # A stray 13-digit run (e.g. misread MRZ noise) that fails the checksum
    # should be skipped in favor of the one that actually validates.
    raw_text = "9999999999999\n" + _sample_card_text(cnp)

    fields = _parse_fields(raw_text)

    assert fields["national_id"] == cnp
    assert fields["national_id_valid"] is True


def test_missing_fields_return_none_without_raising():
    fields = _parse_fields("garbled ocr output with nothing recognizable")

    assert fields["national_id"] is None
    assert fields["national_id_valid"] is False
    assert fields["last_name"] is None
    assert fields["first_name"] is None
    assert fields["address"] is None
    assert fields["date_of_birth"] is None
    assert fields["gender"] is None
    assert fields["series_number"] is None
    assert fields["raw_text"] == "garbled ocr output with nothing recognizable"


def test_no_cnp_at_all_still_returns_dict_with_none():
    fields = _parse_fields(_sample_card_text("").replace("CNP \n", ""))

    assert fields["national_id"] is None
    assert fields["national_id_valid"] is False
    assert fields["last_name"] == "POPESCU"


# Real Romanian specimen card layout (romania.gov specimen): three-language
# labels, a two-line Domiciliu, uppercase "SERIA .. NR", and a placeholder
# CNP that fails the checksum on purpose (specimens never use a real one).
_SPECIMEN_CARD_TEXT = """ROMANIA
CARTE DE IDENTITATE
SERIA RD NR 123456
CNP 1900712345678
Nume/Nom/Last name
POPESCU
Prenume/Prenom/First name
ANDREI
Cetatenie/Nationalite/Nationality
Romana / ROU
Loc nastere/Lieu de naissance/Place of birth
Mun. Bucuresti Sec. 1
Domiciliu/Adresse/Address
Mun. Bucuresti Sec. 1
Str. Exemplu nr. 1 bl. M1 sc. 1 et. 1 ap. 1
Emisa de/Delivree par/Issued by
SPCLEP Sector 1
Valabilitate/Valable jusqu'au
01.07.2023-01.07.2033
"""


def test_specimen_card_cnp_is_extracted_despite_mismatched_checksum():
    # This app's validate_national_id doesn't enforce the official MOD-11
    # checksum (see its docstring) - only shape - so a specimen card's
    # placeholder CNP, where only the last digit is "wrong", is extracted
    # and reported as valid, same as a real one would be.
    fields = _parse_fields(_SPECIMEN_CARD_TEXT)

    assert fields["national_id"] == "1900712345678"
    assert fields["national_id_valid"] is True


def test_specimen_card_names_and_series_number():
    fields = _parse_fields(_SPECIMEN_CARD_TEXT)

    assert fields["last_name"] == "POPESCU"
    assert fields["first_name"] == "ANDREI"
    assert fields["series_number"] == "RD123456"


def test_specimen_card_address_joins_both_lines_without_bleeding_into_next_label():
    fields = _parse_fields(_SPECIMEN_CARD_TEXT)

    assert fields["address"] == "Mun. Bucuresti Sec. 1 Str. Exemplu nr. 1 bl. M1 sc. 1 et. 1 ap. 1"


def test_specimen_card_date_of_birth_still_derived_despite_bad_checksum():
    # The checksum digit is the LAST character - a bad checksum doesn't
    # invalidate the date/gender encoded in the earlier digits.
    fields = _parse_fields(_SPECIMEN_CARD_TEXT)

    assert fields["date_of_birth"] == "1990-07-12"
    assert fields["gender"] == "M"
