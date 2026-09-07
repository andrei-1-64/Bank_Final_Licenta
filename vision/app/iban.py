"""Extracts an IBAN from a photo or PDF (a bank card, an invoice, a bank
statement, a screenshot) via local OCR - see app/core/ocr.py for the shared
preprocessing/confidence mechanics this shares with app/modules/id_ocr.

Unlike ID-card OCR (which parses labeled fields at fixed positions), an
IBAN can appear anywhere in the document in any font - so this scans every
line of the extracted text for an IBAN-shaped run of characters and leans
on the real MOD-97 checksum (validate_iban) to tell a genuine IBAN apart
from noise, the same "trust the checksum over the pattern" approach
id_ocr/extractor.py uses for the CNP.
"""

import re
from pathlib import Path

import pymupdf
import pytesseract
from PIL import Image

from app.ocr import LOW_CONFIDENCE_THRESHOLD, average_ocr_confidence, preprocess_for_ocr
from app.validation import validate_iban

# Country code + 2 check digits, then whatever alphanumeric BBAN
# characters follow - matched against a whitespace/punctuation-stripped
# copy of each line, since IBANs are commonly printed in space-separated
# groups ("RO49 AAAA 1B31 0075 9384 0000", or unevenly grouped on real
# documents, e.g. "RO71 BANK4 JG9W 0MDT 6GE2SJE").
_IBAN_CANDIDATE_PATTERN = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]+")

# ISO 13616-registered IBAN length per country - a candidate is only ever
# checked at ITS country's one correct length. Trying a range of lengths
# and accepting the first one whose checksum happens to validate (an
# earlier version of this function did that) is unsound: a genuine OCR
# misread can still leave some WRONG-length prefix that coincidentally
# passes MOD-97 by chance, silently returning a corrupted IBAN instead of
# correctly finding nothing. An unrecognized country code is skipped
# rather than guessed at.
_IBAN_LENGTH_BY_COUNTRY: dict[str, int] = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16,
    "BG": 22, "BH": 22, "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28,
    "CZ": 24, "DE": 22, "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24,
    "FI": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18,
    "GR": 27, "GT": 28, "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23,
    "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28, "LC": 32,
    "LI": 21, "LT": 20, "LU": 20, "LV": 21, "LY": 25, "MC": 27, "MD": 24,
    "ME": 22, "MK": 19, "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15,
    "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22,
    "SA": 24, "SC": 31, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "ST": 25,
    "SV": 28, "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24,
    "XK": 20,
}  # fmt: skip

# A PDF page's real text layer (when it has one) beats OCR outright - it's
# character-perfect, nothing to misread. A scanned/image-only page still
# reports a handful of stray characters sometimes (headers, metadata), so
# this floor distinguishes "has a real text layer" from "effectively none".
_MIN_NATIVE_TEXT_CHARS = 20

#: 2x raster zoom for PDF pages with no text layer - a PDF page rendered at
#: its native 72 DPI is noticeably blurrier once autocontrast+grayscale run
#: on it than a typical phone photo would be; this roughly matches a 144
#: DPI scan, closer to what tesseract expects.
_PDF_RENDER_ZOOM = 2


def _find_iban(raw_text: str) -> str | None:
    """Line by line (not the whole text at once) so unrelated text above or
    below the IBAN can't get pulled into the same candidate."""
    for line in raw_text.splitlines():
        condensed = re.sub(r"[^A-Za-z0-9]", "", line).upper()
        for match in _IBAN_CANDIDATE_PATTERN.finditer(condensed):
            expected_length = _IBAN_LENGTH_BY_COUNTRY.get(match.group(0)[:2])
            if expected_length is None:
                continue
            candidate = condensed[match.start() : match.start() + expected_length]
            if len(candidate) == expected_length and validate_iban(candidate):
                return candidate
    return None


def _read_image(image: Image.Image) -> dict:
    processed = preprocess_for_ocr(image)
    raw_text = pytesseract.image_to_string(processed, lang="ron")
    confidence = average_ocr_confidence(processed)
    iban = _find_iban(raw_text)
    return {
        "iban": iban,
        "ocr_confidence": round(confidence, 1),
        "low_confidence": confidence < LOW_CONFIDENCE_THRESHOLD or iban is None,
        "raw_text": raw_text,
    }


def _read_pdf(path: Path) -> dict:
    """Prefers each page's real text layer over OCR when it has one -
    generated documents (statements, invoices) almost always do, and OCR
    can only make a perfect read worse. Only rasterizes+OCRs a page that
    has no usable text layer (a scan)."""
    document = pymupdf.open(str(path))
    try:
        confidence = 0.0
        raw_text = ""
        for page in document:
            native_text = page.get_text()
            if len(native_text.strip()) >= _MIN_NATIVE_TEXT_CHARS:
                raw_text = native_text
                confidence = 100.0
            else:
                pixmap = page.get_pixmap(
                    matrix=pymupdf.Matrix(_PDF_RENDER_ZOOM, _PDF_RENDER_ZOOM)
                )
                page_image = Image.frombytes(
                    "RGB", (pixmap.width, pixmap.height), pixmap.samples
                )
                processed = preprocess_for_ocr(page_image)
                raw_text = pytesseract.image_to_string(processed, lang="ron")
                confidence = average_ocr_confidence(processed)

            iban = _find_iban(raw_text)
            if iban is not None:
                return {
                    "iban": iban,
                    "ocr_confidence": round(confidence, 1),
                    "low_confidence": confidence < LOW_CONFIDENCE_THRESHOLD,
                    "raw_text": raw_text,
                }

        return {
            "iban": None,
            "ocr_confidence": round(confidence, 1),
            "low_confidence": True,
            "raw_text": raw_text,
        }
    finally:
        document.close()


def extract_iban(file_path: str) -> dict:
    """Reads the photo or PDF at `file_path` and returns a best-effort dict:

        iban, ocr_confidence, low_confidence, raw_text

    For a PDF, every page is tried in order (text layer first, OCR only if
    a page has none) and the first checksum-valid IBAN found wins;
    ocr_confidence/raw_text then describe that page. If no page yields
    one, the last page's read is reported instead, so a caller debugging
    "why didn't this work" still sees what was actually read.

    low_confidence is true when either the read's confidence is below
    LOW_CONFIDENCE_THRESHOLD (always false for a PDF's native text layer -
    there's nothing to misread), or no checksum-valid IBAN was found at
    all - callers should surface this as "try a clearer photo/PDF, or
    enter it manually," not silently trust a guess.
    """
    path = Path(file_path)
    if path.suffix.lower() == ".pdf":
        return _read_pdf(path)
    return _read_image(Image.open(path))
