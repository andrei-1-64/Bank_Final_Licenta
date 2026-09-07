"""IBAN extraction quality, against this service's own endpoint.

MOVED FROM backend/tests/integration/test_iban_ocr_api.py when OCR moved
here. These tests render real images and PDFs and assert what comes back out
of the extractor, so they need tesseract and the DejaVu fonts - both of
which live in this image and no longer in the backend's.

The backend keeps a separate, thinner test of its own /iban-ocr HTTP surface
(auth, content-type and size validation) with this service stubbed out.
"""

import importlib
import io

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFilter, ImageFont

VALID_IBAN = "RO49AAAA1B31007593840000"
_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_TOKEN = "test-vision-token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("VISION_SERVICE_TOKEN", _TOKEN)
    import app.main

    importlib.reload(app.main)
    return TestClient(app.main.app)


def _render_text_image(lines: list[str]) -> Image.Image:
    font = ImageFont.truetype(_FONT_PATH, 28)
    image = Image.new("RGB", (900, 320), "white")
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(lines):
        draw.text((20, 20 + i * 40), line, fill="black", font=font)
    return image


def _to_png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _render_pdf_bytes(pages: list[list[str]]) -> bytes:
    document = pymupdf.open()
    try:
        for lines in pages:
            page = document.new_page()
            for i, line in enumerate(lines):
                page.insert_text((50, 80 + i * 40), line, fontsize=18)
        return document.tobytes()
    finally:
        document.close()


def _post(client, filename: str, content: bytes, content_type: str):
    return client.post(
        "/v1/iban",
        files={"file": (filename, content, content_type)},
        headers={"X-Vision-Token": _TOKEN},
    )


def test_reads_iban_from_photo(client):
    png = _to_png_bytes(_render_text_image(["Card holder: Ion Popescu", VALID_IBAN]))

    resp = _post(client, "card.png", png, "image/png")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["iban"] == VALID_IBAN
    assert body["low_confidence"] is False
    assert body["ocr_confidence"] > 0


def test_reads_iban_with_spaced_grouping(client):
    """IBANs are commonly printed in space-separated groups of 4. Double
    spaces between groups (not single) - tesseract reliably misreads "1B31"
    as "1B831" at single-space width with this font/size, a rendering
    artifact unrelated to the extraction logic under test (which has its own
    direct, rendering-free coverage in test_iban.py)."""
    spaced = "  ".join(VALID_IBAN[i : i + 4] for i in range(0, len(VALID_IBAN), 4))

    resp = _post(client, "card.png", _to_png_bytes(_render_text_image([spaced])), "image/png")

    assert resp.status_code == 200, resp.text
    assert resp.json()["iban"] == VALID_IBAN


def test_ignores_checksum_invalid_lookalike(client):
    """Proves the checksum, not just the pattern, gates a match - this string
    is IBAN-shaped but its check digits are wrong."""
    fake = "RO00AAAA1B31007593840000"

    resp = _post(client, "card.png", _to_png_bytes(_render_text_image([fake])), "image/png")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["iban"] is None
    assert body["low_confidence"] is True


def test_flags_low_confidence_when_no_iban_present(client):
    png = _to_png_bytes(_render_text_image(["Total: 42.50 RON", "Multumim!"]))

    resp = _post(client, "receipt.png", png, "image/png")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["iban"] is None
    assert body["low_confidence"] is True


def test_flags_low_confidence_for_blurry_image(client):
    blurred = _render_text_image([VALID_IBAN]).filter(ImageFilter.GaussianBlur(radius=4))

    resp = _post(client, "blurry.png", _to_png_bytes(blurred), "image/png")

    assert resp.status_code == 200, resp.text
    assert resp.json()["low_confidence"] is True


def test_reads_iban_from_pdf(client):
    pdf = _render_pdf_bytes([["Extras de cont", VALID_IBAN]])

    resp = _post(client, "statement.pdf", pdf, "application/pdf")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["iban"] == VALID_IBAN
    assert body["low_confidence"] is False


def test_finds_iban_on_second_pdf_page(client):
    """The IBAN isn't always on the first page of a statement - every page is
    tried in order (see app/iban.py::extract_iban)."""
    pdf = _render_pdf_bytes([["Pagina 1 - fara IBAN aici"], ["Pagina 2", VALID_IBAN]])

    resp = _post(client, "statement.pdf", pdf, "application/pdf")

    assert resp.status_code == 200, resp.text
    assert resp.json()["iban"] == VALID_IBAN


def test_prefers_pdf_text_layer_over_ocr(client):
    """Regression test: a PDF with a real text layer (a generated
    statement/invoice, not a scan) must be read via that text layer, not
    rasterized and OCR'd - OCR can misread a character ("0" as "O") that the
    text layer never gets wrong. Uses the same unevenly-grouped spacing a
    real statement used when this bug was reported."""
    iban = "RO71BANK4JG9W0MDT6GE2SJE"
    pdf = _render_pdf_bytes([["IBAN", "RO71 BANK4 JG9W 0MDT 6GE2SJE"]])

    resp = _post(client, "statement.pdf", pdf, "application/pdf")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["iban"] == iban
    assert body["ocr_confidence"] == 100.0
    assert body["low_confidence"] is False


def test_unreadable_pdf_is_422_not_500(client):
    """A corrupt upload is the caller's problem, not an outage - the backend
    turns 422 into a ValidationError and anything else into a 502, so this
    distinction decides which error the user sees."""
    resp = _post(client, "statement.pdf", b"not a real pdf", "application/pdf")
    assert resp.status_code == 422


def test_unreadable_image_is_422_not_500(client):
    resp = _post(client, "card.png", b"this is not a valid png file", "image/png")
    assert resp.status_code == 422


def test_empty_upload_is_rejected(client):
    resp = _post(client, "card.png", b"", "image/png")
    assert resp.status_code == 400
