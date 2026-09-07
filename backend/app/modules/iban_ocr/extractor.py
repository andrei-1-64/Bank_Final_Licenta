"""IBAN extraction from a photo or PDF - now a call to vision-service.

The scanning logic (OCR, the per-country IBAN lengths, the MOD-97 checksum
gate, the PDF text-layer-before-OCR preference) moved to vision/app/iban.py
along with its unit tests. See app/modules/id_ocr/extractor.py for the same
note about what deliberately stayed behind.
"""

from __future__ import annotations

from typing import Any

from app.core import vision_client


async def extract_iban(content: bytes, *, filename: str) -> dict[str, Any]:
    """Returns `iban`, `ocr_confidence`, `low_confidence`, `raw_text`.

    `filename` is passed through because the service picks the PDF reader or
    the image reader from its suffix - the same branch this function used to
    make on the local path.
    """
    return await vision_client.extract_iban(content, filename=filename)
