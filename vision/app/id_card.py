"""Extracts PII from a photo of a Romanian national ID card (Carte de
Identitate) via local OCR - tesseract runs inside this container, so the
image and everything read off it never leave this machine.

Best-effort: ID card OCR is noisy (stylized fonts, holograms, the photo
overlapping text), so every field except raw_text can come back None if
it wasn't confidently found. Treat the result as a form-prefill suggestion
a human should double-check, not a verified identity document.
"""

import re
from pathlib import Path

import pytesseract
from PIL import Image

from app.ocr import LOW_CONFIDENCE_THRESHOLD, average_ocr_confidence, preprocess_for_ocr
from app.validation import (
    extract_date_of_birth,
    extract_gender,
    validate_national_id,
)

# Anchored to the "CNP" label itself - this is what real cards print
# ("CNP 1234567890123") right next to the label, so it's far more precise
# than scanning the whole card for any 13-digit run (which also catches
# noise from the MRZ lines at the bottom). Falls back to a checksum-valid
# blind scan (_CNP_FALLBACK_PATTERN) only if the label itself wasn't read.
_CNP_LABELED_PATTERN = re.compile(r"CNP\s*[:\-]?\s*(\d{13})", re.IGNORECASE)
_CNP_FALLBACK_PATTERN = re.compile(r"\b\d{13}\b")

# e.g. "Seria RD Nr. 123456" or "SERIA RD NR 123456" - the two letters and
# six digits are usually separated by a "Nr." label, not just whitespace;
# case varies by card/OCR, hence IGNORECASE throughout this module.
_SERIES_NUMBER_PATTERN = re.compile(r"\b([A-Z]{2})\s*(?:Nr\.?)?\s*(\d{6})\b", re.IGNORECASE)

# Romanian ID cards print each field as a "Label/FrenchLabel/EnglishLabel"
# line followed by the value on the NEXT line - [^\n]* eats the rest of the
# label line (however many languages, in whatever order) so the capture
# always lands on the line after it, not on another language's label text.
# Diacritics appear in both Unicode forms in the wild (comma-below Ș/Ț vs
# cedilla Ş/Ţ) depending on the font/scan, so both are accepted.
_NAME_CHARS = r"A-ZĂÂÎȘȚŞŢ\- "
_LAST_NAME_PATTERN = re.compile(rf"(?:Nume|Nom)[^\n]*\n\s*([{_NAME_CHARS}]{{2,40}})", re.IGNORECASE)
_FIRST_NAME_PATTERN = re.compile(
    rf"(?:Prenume|Pr[ée]nom)[^\n]*\n\s*([{_NAME_CHARS}]{{2,40}})", re.IGNORECASE
)
# Domiciliu commonly wraps two lines (locality, then street) - both are
# captured and joined. The second line is optional (some cards fit it on
# one line), and is only taken if it does NOT contain "/" - every label
# line on these cards is "Romanian/French[/English]", so a "/" anywhere in
# what would be the second line means it's actually the NEXT field's
# label, not a continuation of the address.
_ADDRESS_PATTERN = re.compile(
    r"(?:Domiciliu|Adresse)[^\n]*\n\s*([^\n]{3,60})(?:\n(?!\s*[^\n]*/)\s*([^\n]{3,60}))?",
    re.IGNORECASE,
)


def _find_cnp(raw_text: str) -> tuple[str | None, bool]:
    """Returns (national_id, is_checksum_valid). Prefers the digits printed
    right after the "CNP" label - unambiguous, and doesn't depend on the
    checksum (a specimen/placeholder card, or a single misread digit at the
    checksum position, still has a real CNP-shaped number worth returning;
    national_id_valid tells the caller whether to trust it as-is)."""
    labeled_match = _CNP_LABELED_PATTERN.search(raw_text)
    if labeled_match:
        candidate = labeled_match.group(1)
        is_valid, _reason = validate_national_id(candidate)
        return candidate, is_valid

    # No "CNP" label found (misread or absent) - fall back to scanning the
    # whole text, but only trust a checksum-valid hit here, since without
    # the label as an anchor this could just as easily match MRZ noise.
    for candidate in _CNP_FALLBACK_PATTERN.findall(raw_text):
        is_valid, _reason = validate_national_id(candidate)
        if is_valid:
            return candidate, True
    return None, False


def _find_labeled_field(raw_text: str, pattern: re.Pattern) -> str | None:
    match = pattern.search(raw_text)
    if not match:
        return None
    value = match.group(1).strip(" :-\n\t")
    return value or None


def _find_address(raw_text: str) -> str | None:
    match = _ADDRESS_PATTERN.search(raw_text)
    if not match:
        return None
    lines = [g.strip(" :-\n\t") for g in match.groups() if g]
    combined = " ".join(line for line in lines if line)
    return combined or None


def _derive_date_of_birth_and_gender(national_id: str) -> tuple[str | None, str | None]:
    """The CNP's check digit (last position) is the only part checksum
    failure could implicate - date/gender are decoded from earlier digits,
    so still worth trying even when national_id_valid is False. Guards
    against a genuinely garbled OCR read producing an impossible date."""
    try:
        date_of_birth = extract_date_of_birth(national_id).isoformat()
    except ValueError:
        return None, None
    return date_of_birth, extract_gender(national_id)


def _parse_fields(raw_text: str) -> dict:
    """The text-parsing half of extract_id_fields, split out so it's
    testable against known OCR output without needing tesseract or an
    actual image file."""
    national_id, national_id_valid = _find_cnp(raw_text)

    fields: dict = {
        "national_id": national_id,
        "national_id_valid": national_id_valid,
        "last_name": _find_labeled_field(raw_text, _LAST_NAME_PATTERN),
        "first_name": _find_labeled_field(raw_text, _FIRST_NAME_PATTERN),
        "address": _find_address(raw_text),
        "date_of_birth": None,
        "gender": None,
        "series_number": None,
        "raw_text": raw_text,
    }

    if national_id:
        fields["date_of_birth"], fields["gender"] = _derive_date_of_birth_and_gender(national_id)

    series_match = _SERIES_NUMBER_PATTERN.search(raw_text)
    if series_match:
        fields["series_number"] = f"{series_match.group(1)}{series_match.group(2)}".upper()

    return fields


def extract_id_fields(png_path: str) -> dict:
    """Reads the ID card at `png_path` and returns a best-effort PII dict:

        national_id, national_id_valid, last_name, first_name, address,
        date_of_birth, gender, series_number, ocr_confidence,
        low_confidence, raw_text

    date_of_birth/gender are decoded from the CNP itself (reliable) rather
    than OCR'd separately, once a CNP is found - see
    app/modules/auth/validation.py, the same logic /auth/register uses.

    low_confidence is true when either Tesseract's own average word
    confidence is below LOW_CONFIDENCE_THRESHOLD, or no CNP was found at
    all (the single most important field - a photo that fails to yield one
    is unreliable regardless of what the confidence score says about the
    rest of the card). Callers should surface this as "try a clearer
    photo," not silently trust whatever partial data came back.
    """
    image = preprocess_for_ocr(Image.open(Path(png_path)))
    raw_text = pytesseract.image_to_string(image, lang="ron")
    fields = _parse_fields(raw_text)

    confidence = average_ocr_confidence(image)
    fields["ocr_confidence"] = round(confidence, 1)
    fields["low_confidence"] = (
        confidence < LOW_CONFIDENCE_THRESHOLD or fields["national_id"] is None
    )
    return fields
