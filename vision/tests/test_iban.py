from app.iban import _find_iban

VALID_IBAN = "RO49AAAA1B31007593840000"


def test_finds_plain_iban_in_text():
    assert _find_iban(f"Some text\n{VALID_IBAN}\nMore text") == VALID_IBAN


def test_finds_space_grouped_iban():
    spaced = " ".join(VALID_IBAN[i : i + 4] for i in range(0, len(VALID_IBAN), 4))
    assert _find_iban(spaced) == VALID_IBAN


def test_ignores_checksum_invalid_lookalike():
    assert _find_iban("RO00AAAA1B31007593840000") is None


def test_returns_none_when_no_iban_present():
    assert _find_iban("Total: 42.50 RON\nThank you for your purchase") is None


def test_trims_trailing_noise_on_the_same_line():
    """A greedy pattern match can over-capture trailing OCR noise on the
    same line - the shrinking-length loop should still recover the real
    IBAN by trying shorter prefixes until one checksum-validates."""
    assert _find_iban(f"{VALID_IBAN}TITULARX") == VALID_IBAN


def test_does_not_merge_across_lines():
    """Two unrelated all-caps/digit fragments on separate lines must not be
    concatenated into a false IBAN - only whitespace WITHIN a line is
    stripped, not across lines."""
    assert _find_iban("RO49AAAA\n1B31007593840000") is None
