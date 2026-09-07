"""The service token gate.

The service is unreachable from outside the compose network, so this token
is defence in depth - but "defence in depth" is only true if it actually
holds, including the case nobody thinks about: an unset secret must refuse
everything rather than let everything through.
"""

import importlib

import pytest
from fastapi.testclient import TestClient

ENDPOINTS = [
    "/v1/id-card",
    "/v1/iban",
    "/v1/pdf-text",
]


def _client(monkeypatch, token: str):
    """Rebuild the app with a given token - it is read at import time."""
    monkeypatch.setenv("VISION_SERVICE_TOKEN", token)
    import app.main

    importlib.reload(app.main)
    return TestClient(app.main.app)


@pytest.mark.parametrize("path", ENDPOINTS)
def test_missing_token_is_rejected(monkeypatch, path):
    client = _client(monkeypatch, "s3cret")
    resp = client.post(path, files={"file": ("x.png", b"not-an-image", "image/png")})
    assert resp.status_code == 401


@pytest.mark.parametrize("path", ENDPOINTS)
def test_wrong_token_is_rejected(monkeypatch, path):
    client = _client(monkeypatch, "s3cret")
    resp = client.post(
        path,
        files={"file": ("x.png", b"not-an-image", "image/png")},
        headers={"X-Vision-Token": "wrong"},
    )
    assert resp.status_code == 401


@pytest.mark.parametrize("path", ENDPOINTS)
def test_unset_token_fails_closed(monkeypatch, path):
    """The case that matters most: no secret configured must NOT mean
    'allow anyone'."""
    client = _client(monkeypatch, "")
    resp = client.post(
        path,
        files={"file": ("x.png", b"not-an-image", "image/png")},
        headers={"X-Vision-Token": "anything"},
    )
    assert resp.status_code == 503


def test_health_needs_no_token(monkeypatch):
    """Otherwise the container healthcheck could never pass."""
    client = _client(monkeypatch, "s3cret")
    assert client.get("/health").status_code == 200


# /v1/face/embedding takes a BURST of frames under a repeated "files" field,
# not the single "file" field the other three endpoints share (see
# app/face.py's liveness docstring) - kept as its own small set of tests
# rather than folded into the parametrized ENDPOINTS list above.
_FACE_FILES = [("files", ("x.jpg", b"not-an-image", "image/jpeg"))]


def test_face_embedding_missing_token_is_rejected(monkeypatch):
    client = _client(monkeypatch, "s3cret")
    resp = client.post("/v1/face/embedding", files=_FACE_FILES)
    assert resp.status_code == 401


def test_face_embedding_wrong_token_is_rejected(monkeypatch):
    client = _client(monkeypatch, "s3cret")
    resp = client.post(
        "/v1/face/embedding", files=_FACE_FILES, headers={"X-Vision-Token": "wrong"}
    )
    assert resp.status_code == 401


def test_face_embedding_unset_token_fails_closed(monkeypatch):
    client = _client(monkeypatch, "")
    resp = client.post(
        "/v1/face/embedding", files=_FACE_FILES, headers={"X-Vision-Token": "anything"}
    )
    assert resp.status_code == 503
