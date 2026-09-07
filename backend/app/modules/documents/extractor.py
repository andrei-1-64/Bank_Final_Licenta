"""PDF handling for uploaded documents.

Split by the vision-service extraction: `validate_pdf_magic_bytes` stayed
here because rejecting a file that isn't a PDF is request validation and
should happen before the bytes travel anywhere. `extract_pdf_text` is now a
call to vision-service (vision/app/pdf_text.py), which owns pymupdf.

Still no OCR fallback: a scanned, no-text-layer PDF yields an empty string,
and DocumentAgent's system prompt handles that case.
"""

from __future__ import annotations

from app.core import vision_client
from app.core.exceptions import ValidationError

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
ALLOWED_MIME_TYPES = {"application/pdf"}

#: The real PDF file signature (ISO 32000). Checked in addition to the
#: client-supplied Content-Type header, which is trivially spoofable.
_PDF_MAGIC = b"%PDF-"


def validate_pdf_magic_bytes(content: bytes) -> None:
    """Verify the actual file signature, not just the Content-Type header.

    A client can label any file `application/pdf`; this is the defence in
    depth that catches it - now also before the bytes leave this container.
    """
    if not content.startswith(_PDF_MAGIC):
        raise ValidationError("Fișierul nu este un PDF valid.")


async def extract_pdf_text(pdf_bytes: bytes) -> tuple[str, int]:
    """Extract text from PDF bytes. Returns (text, page_count).

    Raises ValidationError on a corrupt or unreadable PDF - vision-service
    reports that as a 422, which `vision_client` already translates. An
    empty result is not an error: a scanned PDF with no text layer returns
    "" plus its real page count.
    """
    try:
        return await vision_client.extract_pdf_text(pdf_bytes)
    except ValidationError as exc:
        # The service's machine-readable detail ("unreadable_pdf") is not
        # something to show a user - restore this module's own wording.
        raise ValidationError("PDF ilizibil sau corupt.") from exc
