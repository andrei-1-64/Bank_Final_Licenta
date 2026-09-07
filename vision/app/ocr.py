"""Shared OCR primitives - image preprocessing and confidence scoring used
by every OCR-backed feature (ID card extraction, IBAN extraction). Kept
here instead of duplicated per-module so "how confident was this read"
means exactly the same thing everywhere it's reported.

Every OCR feature in this app runs fully offline (tesseract, in-container) -
no photo, or anything read off one, ever leaves this machine.
"""

import pytesseract
from PIL import Image, ImageOps

#: Tesseract's own per-word confidence is 0-100. Below this, the read is
#: unreliable enough that the caller should ask for a clearer photo rather
#: than silently trust (possibly wrong) autofilled data.
LOW_CONFIDENCE_THRESHOLD = 60.0


def preprocess_for_ocr(image: Image.Image) -> Image.Image:
    """Grayscale + autocontrast: real phone photos (uneven lighting, low
    contrast against the surface behind the text) OCR noticeably worse than
    a clean scan/render - this is the standard cheap fix, applied before
    tesseract ever sees the image."""
    return ImageOps.autocontrast(ImageOps.grayscale(image))


def average_ocr_confidence(image: Image.Image, *, lang: str = "ron") -> float:
    """Tesseract reports a confidence (0-100) per detected word, and -1 for
    boxes that aren't real text (whitespace/layout regions) - those are
    excluded, not averaged in. No real text detected at all is scored 0,
    not skipped - that itself means the photo didn't read."""
    data = pytesseract.image_to_data(image, lang=lang, output_type=pytesseract.Output.DICT)
    scores = [float(conf) for conf in data.get("conf", []) if float(conf) >= 0]
    if not scores:
        return 0.0
    return sum(scores) / len(scores)
