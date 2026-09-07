"""PDF text extraction (pymupdf).

Moved out of backend/app/modules/documents/extractor.py. Only the pymupdf
half came across - `validate_pdf_magic_bytes` stayed in the backend, because
rejecting a non-PDF upload is a request-validation concern and should happen
before the bytes ever travel over the wire to this service.
"""

from __future__ import annotations

import pymupdf


class UnreadablePdfError(Exception):
    """Corrupt or unparseable PDF."""


def extract_pdf_text(pdf_bytes: bytes) -> tuple[str, int]:
    """Extract text from PDF bytes. Returns (text, page_count).

    An empty result is NOT an error: a scanned PDF with no text layer
    legitimately yields "" plus a real page count. Deliberately no OCR
    fallback here - see the original module docstring.
    """
    try:
        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise UnreadablePdfError() from exc

    try:
        page_count = document.page_count
        text = "\n".join(page.get_text() for page in document)
    except Exception as exc:
        raise UnreadablePdfError() from exc
    finally:
        document.close()

    return text, page_count
