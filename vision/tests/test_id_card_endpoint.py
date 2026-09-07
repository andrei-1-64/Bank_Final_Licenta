"""ID-card extraction quality, against this service's own endpoint.

MOVED FROM backend/tests/integration/test_id_ocr_api.py when OCR moved here -
same reasoning as test_iban_endpoint.py: these render real card images and
assert what the extractor reads back, so they need tesseract and the DejaVu
fonts, which no longer exist in the backend image.
"""

import importlib
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.validation import generate_test_national_id

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_TOKEN = "test-vision-token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("VISION_SERVICE_TOKEN", _TOKEN)
    import app.main

    importlib.reload(app.main)
    return TestClient(app.main.app)


def _render_id_card_image(lines: list[str]) -> Image.Image:
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


def _render_id_card_png(cnp: str) -> bytes:
    return _to_png_bytes(
        _render_id_card_image(
            [
                "CNP " + cnp,
                "Nume/Nom",
                "POPESCU",
                "Prenume/Prenom",
                "ANDREI ION",
                "Domiciliu/Adresse",
                "STR EXEMPLU NR 1 BUCURESTI",
            ]
        )
    )


def _post(client, content: bytes):
    return client.post(
        "/v1/id-card",
        files={"file": ("id.png", content, "image/png")},
        headers={"X-Vision-Token": _TOKEN},
    )


def test_reads_cnp_name_and_address_from_photo(client):
    cnp = generate_test_national_id(1995, 6, 15, gender="F")

    resp = _post(client, _render_id_card_png(cnp))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["national_id"] == cnp
    assert body["national_id_valid"] is True
    assert body["last_name"] == "POPESCU"
    assert body["first_name"] == "ANDREI ION"
    assert body["address"].startswith("STR EXEMPLU")
    assert body["date_of_birth"] == "1995-06-15"
    assert body["gender"] == "F"
    assert body["low_confidence"] is False
    assert body["ocr_confidence"] > 0


def test_flags_low_confidence_when_cnp_not_found(client):
    """Otherwise perfectly clear text - this exercises the missing-CNP
    trigger, not the OCR-confidence-score one."""
    png = _to_png_bytes(
        _render_id_card_image(["Nume/Nom", "POPESCU", "Prenume/Prenom", "ANDREI ION"])
    )

    resp = _post(client, png)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["national_id"] is None
    assert body["low_confidence"] is True


def test_flags_low_confidence_for_blurry_image(client):
    cnp = generate_test_national_id(1988, 3, 3, gender="M")
    image = _render_id_card_image(
        ["CNP " + cnp, "Nume/Nom", "POPESCU", "Prenume/Prenom", "ANDREI ION"]
    )

    resp = _post(client, _to_png_bytes(image.filter(ImageFilter.GaussianBlur(radius=8))))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["low_confidence"] is True
    assert body["ocr_confidence"] < 60.0


def test_unreadable_image_is_422_not_500(client):
    resp = _post(client, b"this is not a valid png file")
    assert resp.status_code == 422


def test_empty_upload_is_rejected(client):
    resp = _post(client, b"")
    assert resp.status_code == 400
