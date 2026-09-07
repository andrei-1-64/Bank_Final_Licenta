"""vision-service - stateless image/PDF processing for the BanK backend.

WHAT THIS SERVICE IS: bytes in, structured data out. It holds no database
credentials, no session state, and nothing about who the caller is acting
for. That is deliberate - it exists so the heavy native toolchain (dlib,
tesseract, pymupdf) lives in an image that has nothing worth stealing.

WHAT IT IS NOT: an authorisation boundary. Every check that decides whether
a person may do something - session, ownership, blocked account, face-match
comparison, confirmation tokens - stays in the backend. This service will
happily read any image it is handed; the backend decides whether it should
have been handed it.

NOT REACHABLE FROM THE BROWSER: it is on the compose network only, with no
published port. The shared token below is defence in depth behind that, not
the primary control.
"""

from __future__ import annotations

import logging
import os
import secrets
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, File, Header, HTTPException, UploadFile

from app.face import (
    MultipleFacesDetectedError,
    NoBlinkDetectedError,
    NoFaceDetectedError,
    extract_embedding_with_liveness,
)
from app.iban import extract_iban
from app.id_card import extract_id_fields
from app.pdf_text import UnreadablePdfError, extract_pdf_text

logger = logging.getLogger(__name__)

#: Must match VISION_SERVICE_TOKEN in the backend's environment. Unset means
#: "refuse everything" rather than "allow everything" - a missing secret must
#: never fail open.
_TOKEN = os.environ.get("VISION_SERVICE_TOKEN", "")

#: Generous ceiling; the backend enforces its own, smaller, per-endpoint
#: limits before calling. This one only stops a runaway upload from reaching
#: the parsers at all.
MAX_UPLOAD_BYTES = 16 * 1024 * 1024


async def require_service_token(
    x_vision_token: str | None = Header(default=None, alias="X-Vision-Token"),
) -> None:
    if not _TOKEN:
        logger.error("VISION_SERVICE_TOKEN is not set - refusing every request")
        raise HTTPException(status_code=503, detail="vision service not configured")
    # Constant-time: this is a fixed shared secret, so a naive == would leak
    # its length and prefix to anything that can time requests.
    if x_vision_token is None or not secrets.compare_digest(x_vision_token, _TOKEN):
        raise HTTPException(status_code=401, detail="invalid service token")


app = FastAPI(title="BanK vision-service")

# The gate goes on a ROUTER, not on the FastAPI app: an app-level dependency
# applies to every route and cannot be waived per route (a route-level
# `dependencies=[]` adds nothing, it does not override). Putting it here
# keeps /health reachable for the container healthcheck, while every real
# endpoint below is gated by construction - including ones added later.
protected = APIRouter(dependencies=[Depends(require_service_token)])


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="upload too large")
    return content


def _with_temp_file(content: bytes, suffix: str, fn):
    """The OCR extractors take a path, not bytes. Written and removed here so
    an uploaded document never outlives the request that carried it.

    PIL, pymupdf and tesseract each fail in their own library-specific way on
    a corrupt or unreadable file. All of them mean one thing to the caller -
    "this isn't readable" - so they are reported as 422, not as a 500 that
    would make the backend treat a bad upload as an outage.
    """
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        return fn(str(tmp_path))
    except Exception as exc:
        logger.info("unreadable upload (%s): %s", suffix, type(exc).__name__)
        raise HTTPException(status_code=422, detail="unreadable_file") from exc
    finally:
        tmp_path.unlink(missing_ok=True)


@app.get("/health")
async def health() -> dict[str, str]:
    """Ungated on purpose - the compose healthcheck calls it, and it reveals
    nothing beyond "this process is up"."""
    return {"status": "ok"}


@protected.post("/v1/id-card")
async def read_id_card(file: UploadFile = File(...)) -> dict:
    content = await _read_upload(file)
    return _with_temp_file(content, ".png", extract_id_fields)


@protected.post("/v1/iban")
async def read_iban(file: UploadFile = File(...)) -> dict:
    content = await _read_upload(file)
    # The extractor branches on the suffix (PDF vs image), so the temp file
    # has to keep it.
    suffix = Path(file.filename or "").suffix.lower() or ".png"
    return _with_temp_file(content, suffix, extract_iban)


@protected.post("/v1/pdf-text")
async def read_pdf_text(file: UploadFile = File(...)) -> dict:
    content = await _read_upload(file)
    try:
        text, page_count = extract_pdf_text(content)
    except UnreadablePdfError:
        raise HTTPException(status_code=422, detail="unreadable_pdf") from None
    return {"text": text, "page_count": page_count}


@protected.post("/v1/face/embedding")
async def read_face_embedding(files: list[UploadFile] = File(...)) -> dict:
    """`files` is a short BURST of frames, not one photo - see
    app/face.py's module docstring for why a single photo can never prove
    liveness. Every real caller (face_auth/service.py's enroll/login/step-up
    confirm) sends one; nothing else in this codebase calls this route."""
    contents = [await _read_upload(f) for f in files]
    try:
        return {"embedding": extract_embedding_with_liveness(contents)}
    except NoFaceDetectedError:
        raise HTTPException(status_code=422, detail="no_face_detected") from None
    except MultipleFacesDetectedError:
        raise HTTPException(status_code=422, detail="multiple_faces_detected") from None
    except NoBlinkDetectedError:
        raise HTTPException(status_code=422, detail="no_blink_detected") from None


app.include_router(protected)
