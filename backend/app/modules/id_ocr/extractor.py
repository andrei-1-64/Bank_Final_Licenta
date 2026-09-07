"""ID-card field extraction - now a call to vision-service.

The parsing itself (tesseract, the CNP/name/address regexes, the confidence
scoring) moved to vision/app/id_card.py, together with its unit tests, so
this image no longer needs tesseract or Pillow. What did NOT move: deciding
whether the caller may upload at all, and what to do with a low-confidence
read - those stay with the router and the onboarding flow.

Kept as a module rather than calling `vision_client` straight from the
router so the seam stays where it always was: `id_ocr` still owns "how an ID
photo becomes fields", it just no longer does the pixels itself.
"""

from __future__ import annotations

from typing import Any

from app.core import vision_client


async def extract_id_fields(content: bytes) -> dict[str, Any]:
    """Best-effort PII from an ID-card photo. Same dict shape as before the
    split:

        national_id, national_id_valid, last_name, first_name, address,
        date_of_birth, gender, series_number, ocr_confidence,
        low_confidence, raw_text

    Still a form-prefill suggestion a human should check, never a verified
    identity document.
    """
    return await vision_client.extract_id_fields(content)
