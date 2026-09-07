"""POST /api/v1/id-ocr/extract - this endpoint's HTTP surface.

The OCR itself moved to vision-service, and the tests that render real card
images and assert what comes back went with it
(vision/tests/test_id_card_endpoint.py). What remains here is what this
endpoint still owns: being reachable WITHOUT a session (it runs during
registration, before an account exists), rejecting the wrong content type,
the wrong magic bytes, or an empty/oversized upload, not echoing `raw_text`
back, and turning the service's failures into this API's error envelope.

vision_client is stubbed throughout, so none of this depends on tesseract,
on the DejaVu fonts (gone from this image with the OCR packages), or on
another container being up.
"""

from __future__ import annotations

import pytest

from app.core import vision_client
from app.core.exceptions import ValidationError
from app.core.vision_client import VisionServiceUnavailableError

_CNP = "2950615123456"

# Real PNG file signature (see id_ocr/router.py::_PNG_MAGIC) followed by
# throwaway bytes - passes the magic-byte check so these tests exercise what
# they actually mean to: the endpoint's own logic, not the file format.
_VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-data"
_PNG_WITH_UNREADABLE_CONTENT = b"\x89PNG\r\n\x1a\n" + b"junk"

_OK_RESULT = {
    "national_id": _CNP,
    "national_id_valid": True,
    "last_name": "POPESCU",
    "first_name": "ANDREI ION",
    "address": "STR EXEMPLU NR 1 BUCURESTI",
    "date_of_birth": "1995-06-15",
    "gender": "F",
    "series_number": "RD123456",
    "ocr_confidence": 88.0,
    "low_confidence": False,
    # Present in the service's answer, and deliberately NOT forwarded to the
    # client - the assertion below is the point of keeping it here.
    "raw_text": "CNP 2950615123456\nNume/Nom\nPOPESCU",
}


@pytest.fixture
def stub_vision(monkeypatch):
    state: dict = {"result": _OK_RESULT, "raises": None, "calls": []}

    async def fake_extract_id_fields(content: bytes):
        state["calls"].append(content)
        if state["raises"] is not None:
            raise state["raises"]
        return state["result"]

    monkeypatch.setattr(vision_client, "extract_id_fields", fake_extract_id_fields)
    monkeypatch.setattr(
        "app.modules.id_ocr.extractor.vision_client.extract_id_fields",
        fake_extract_id_fields,
    )
    return state


async def test_extract_requires_no_authentication(client, stub_vision):
    """This runs during registration, before any session exists - so unlike
    almost every other endpoint, anonymous access is the correct behaviour."""
    resp = await client.post(
        "/api/v1/id-ocr/extract", files={"file": ("id.png", _VALID_PNG, "image/png")}
    )

    assert resp.status_code == 200, resp.text


async def test_extract_returns_fields_without_raw_text(client, stub_vision):
    resp = await client.post(
        "/api/v1/id-ocr/extract", files={"file": ("id.png", _VALID_PNG, "image/png")}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["national_id"] == _CNP
    assert body["first_name"] == "ANDREI ION"
    assert body["date_of_birth"] == "1995-06-15"
    # The full OCR dump stays server-side.
    assert "raw_text" not in body


async def test_extract_rejects_non_png_content_type(client, stub_vision):
    resp = await client.post(
        "/api/v1/id-ocr/extract", files={"file": ("id.jpg", b"x", "image/jpeg")}
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    # Rejected before any work is handed to another service.
    assert stub_vision["calls"] == []


async def test_extract_rejects_empty_file(client, stub_vision):
    resp = await client.post(
        "/api/v1/id-ocr/extract", files={"file": ("id.png", b"", "image/png")}
    )

    assert resp.status_code == 422
    assert stub_vision["calls"] == []


async def test_extract_rejects_a_spoofed_content_type(client, stub_vision):
    """`image/png` on a file whose actual bytes aren't a PNG at all - the
    scenario Content-Type alone can never catch, since the client sets it
    (Step 16, item 4: see id_ocr/router.py::_validate_png_magic_bytes)."""
    resp = await client.post(
        "/api/v1/id-ocr/extract",
        files={"file": ("id.png", b"not-actually-a-png", "image/png")},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    # Rejected before any work is handed to another service.
    assert stub_vision["calls"] == []


async def test_unreadable_image_becomes_a_validation_error(client, stub_vision):
    stub_vision["raises"] = ValidationError("unreadable_file")

    resp = await client.post(
        "/api/v1/id-ocr/extract",
        files={"file": ("id.png", _PNG_WITH_UNREADABLE_CONTENT, "image/png")},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_service_outage_is_a_502_not_a_422(client, stub_vision):
    """A bad photo and a service that is down must stay distinguishable."""
    stub_vision["raises"] = VisionServiceUnavailableError()

    resp = await client.post(
        "/api/v1/id-ocr/extract", files={"file": ("id.png", _VALID_PNG, "image/png")}
    )

    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "vision_service_unavailable"
