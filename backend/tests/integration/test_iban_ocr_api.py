"""POST /api/v1/iban-ocr/extract - this endpoint's HTTP surface.

The OCR itself moved to vision-service, and so did the tests that render real
images and assert what comes back out (vision/tests/test_iban_endpoint.py).
What is left here is what this endpoint still owns and vision-service
deliberately does not: requiring a session, rejecting the wrong content type
or the wrong magic bytes, rejecting an oversized or empty upload, and
translating the service's failures into this API's error envelope.

vision_client is stubbed throughout - a test of "does this endpoint check the
session" must not depend on tesseract being installed, on the DejaVu fonts
being present (they are not, since the OCR packages left this image), or on
another container being up.
"""

from __future__ import annotations

import pytest

from app.core import vision_client
from app.core.exceptions import ValidationError
from app.core.vision_client import VisionServiceUnavailableError

VALID_IBAN = "RO49AAAA1B31007593840000"

# Real PNG file signature (see iban_ocr/router.py::_MAGIC_BY_CONTENT_TYPE)
# followed by throwaway bytes - passes the magic-byte check so these tests
# exercise what they actually mean to: the endpoint's own logic, not the
# file format.
_VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-data"
_PNG_WITH_UNREADABLE_CONTENT = b"\x89PNG\r\n\x1a\n" + b"junk"

_OK_RESULT = {
    "iban": VALID_IBAN,
    "ocr_confidence": 91.5,
    "low_confidence": False,
    "raw_text": f"Card holder: Ion Popescu\n{VALID_IBAN}",
}


@pytest.fixture
def stub_vision(monkeypatch):
    """Replace the network call with a canned answer. Returns a dict the test
    can mutate to choose what the service 'returns'."""
    state: dict = {"result": _OK_RESULT, "raises": None, "calls": []}

    async def fake_extract_iban(content: bytes, *, filename: str):
        state["calls"].append({"content": content, "filename": filename})
        if state["raises"] is not None:
            raise state["raises"]
        return state["result"]

    monkeypatch.setattr(vision_client, "extract_iban", fake_extract_iban)
    # The module under test imported the function directly.
    monkeypatch.setattr(
        "app.modules.iban_ocr.extractor.vision_client.extract_iban", fake_extract_iban
    )
    return state


async def test_extract_requires_authentication(client, stub_vision):
    resp = await client.post(
        "/api/v1/iban-ocr/extract", files={"file": ("card.png", _VALID_PNG, "image/png")}
    )

    assert resp.status_code == 401
    # The session check has to happen BEFORE any work is handed to another
    # service - otherwise an anonymous caller could still burn its CPU.
    assert stub_vision["calls"] == []


async def test_extract_returns_the_services_result(authed_client, stub_vision):
    client, _user = authed_client

    resp = await client.post(
        "/api/v1/iban-ocr/extract", files={"file": ("card.png", _VALID_PNG, "image/png")}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["iban"] == VALID_IBAN
    assert body["low_confidence"] is False
    # raw_text is deliberately not echoed back to the client.
    assert "raw_text" not in body


async def test_extract_passes_the_suffix_through(authed_client, stub_vision):
    """vision-service picks its PDF reader or its image reader from the
    filename suffix, so the endpoint has to send a truthful one."""
    client, _user = authed_client

    await client.post(
        "/api/v1/iban-ocr/extract",
        files={"file": ("statement.pdf", b"%PDF-fake", "application/pdf")},
    )

    assert stub_vision["calls"][0]["filename"].endswith(".pdf")


async def test_extract_rejects_non_image_content_type(authed_client, stub_vision):
    client, _user = authed_client

    resp = await client.post(
        "/api/v1/iban-ocr/extract", files={"file": ("card.jpg", b"x", "text/plain")}
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    assert stub_vision["calls"] == []


async def test_extract_rejects_empty_file(authed_client, stub_vision):
    client, _user = authed_client

    resp = await client.post(
        "/api/v1/iban-ocr/extract", files={"file": ("card.png", b"", "image/png")}
    )

    assert resp.status_code == 422
    assert stub_vision["calls"] == []


async def test_extract_rejects_a_spoofed_content_type(authed_client, stub_vision):
    """`application/pdf` on a file whose actual bytes aren't a PDF at all -
    the scenario Content-Type alone can never catch, since the client sets
    it (Step 16, item 4: see iban_ocr/router.py::_validate_magic_bytes)."""
    client, _user = authed_client

    resp = await client.post(
        "/api/v1/iban-ocr/extract",
        files={"file": ("statement.pdf", b"not-actually-a-pdf", "application/pdf")},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    assert stub_vision["calls"] == []


async def test_unreadable_file_becomes_a_validation_error(authed_client, stub_vision):
    """422 from vision-service means 'this upload is unusable' - the caller
    should be told to try a clearer file, not that something broke."""
    client, _user = authed_client
    stub_vision["raises"] = ValidationError("unreadable_file")

    resp = await client.post(
        "/api/v1/iban-ocr/extract",
        files={"file": ("card.png", _PNG_WITH_UNREADABLE_CONTENT, "image/png")},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_service_outage_is_a_502_not_a_422(authed_client, stub_vision):
    """The distinction that matters operationally: a bad photo and a service
    that is down must not look the same to the client or in the logs."""
    client, _user = authed_client
    stub_vision["raises"] = VisionServiceUnavailableError()

    resp = await client.post(
        "/api/v1/iban-ocr/extract", files={"file": ("card.png", _VALID_PNG, "image/png")}
    )

    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "vision_service_unavailable"
