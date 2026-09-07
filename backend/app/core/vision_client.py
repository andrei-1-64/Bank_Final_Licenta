"""Client for vision-service - the image/PDF extraction that used to run in
this process (see vision/app/main.py for what moved and why).

THE CONTRACT: this module sends bytes and gets structured data back. It
never sends who the user is, and the answers it gets are treated exactly
like the local function results they replaced - as untrusted input that the
calling module still has to validate and authorise. Extracting a CNP from a
photo has never been an authorisation step, and it still isn't.

Failures are translated into the app's own exceptions, so callers see the
same error shapes as before the split: a caller should not have to know that
OCR now happens over a network hop.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings
from app.core.exceptions import AppError, ValidationError

logger = logging.getLogger(__name__)

#: Face embedding and OCR are CPU-bound and can genuinely take a few seconds
#: on a large scan - well past httpx's 5s default, which would turn a slow
#: read into a spurious failure.
_TIMEOUT = httpx.Timeout(60.0, connect=5.0)


class VisionServiceUnavailableError(AppError):
    """vision-service could not be reached, or failed unexpectedly.

    502, not 500: this backend is fine, a dependency it needs isn't - and
    the distinction is what tells an operator where to look.
    """

    status_code = 502
    error_code = "vision_service_unavailable"
    default_message = "Serviciul de procesare a imaginilor nu este disponibil."


async def _post_file(
    path: str,
    *,
    content: bytes,
    filename: str,
    content_type: str,
) -> dict[str, Any]:
    """POST one file and return the JSON body.

    A 422 from the service is a *content* problem (no face in the photo, an
    unreadable PDF) rather than an outage, so it comes back as the caller's
    own ValidationError carrying the service's machine-readable `detail` -
    not as a 502.
    """
    url = f"{settings.VISION_SERVICE_URL.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                url,
                files={"file": (filename, content, content_type)},
                headers={"X-Vision-Token": settings.VISION_SERVICE_TOKEN or ""},
            )
    except httpx.HTTPError as exc:
        # The URL is safe to log (no secret in it); the token is not, and is
        # never included.
        logger.exception("vision-service request failed: %s", url)
        raise VisionServiceUnavailableError() from exc

    if response.status_code == 422:
        raise ValidationError(_detail_of(response))
    if response.status_code >= 400:
        logger.error(
            "vision-service returned %s for %s: %s",
            response.status_code,
            path,
            _detail_of(response),
        )
        raise VisionServiceUnavailableError()

    return response.json()


async def _post_files(
    path: str,
    *,
    frames: list[bytes],
    content_type: str,
) -> dict[str, Any]:
    """Like `_post_file`, but for the one endpoint that takes a burst of
    frames instead of a single image (face liveness - see
    vision/app/face.py). httpx accepts a list of (field_name, file_tuple)
    pairs for a repeated multipart field, which is what lets several files
    travel under the same "files" name."""
    url = f"{settings.VISION_SERVICE_URL.rstrip('/')}{path}"
    files = [("files", (f"frame-{i}.jpg", frame, content_type)) for i, frame in enumerate(frames)]
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                url,
                files=files,
                headers={"X-Vision-Token": settings.VISION_SERVICE_TOKEN or ""},
            )
    except httpx.HTTPError as exc:
        logger.exception("vision-service request failed: %s", url)
        raise VisionServiceUnavailableError() from exc

    if response.status_code == 422:
        raise ValidationError(_detail_of(response))
    if response.status_code >= 400:
        logger.error(
            "vision-service returned %s for %s: %s",
            response.status_code,
            path,
            _detail_of(response),
        )
        raise VisionServiceUnavailableError()

    return response.json()


def _detail_of(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", ""))
    except Exception:
        return ""


async def extract_id_fields(content: bytes) -> dict[str, Any]:
    return await _post_file(
        "/v1/id-card", content=content, filename="id.png", content_type="image/png"
    )


async def extract_iban(content: bytes, *, filename: str) -> dict[str, Any]:
    """`filename` matters: the service branches on the suffix to decide
    between the PDF reader and the image reader."""
    return await _post_file(
        "/v1/iban",
        content=content,
        filename=filename or "upload.png",
        content_type="application/octet-stream",
    )


async def extract_pdf_text(content: bytes) -> tuple[str, int]:
    body = await _post_file(
        "/v1/pdf-text",
        content=content,
        filename="document.pdf",
        content_type="application/pdf",
    )
    return body["text"], body["page_count"]


async def extract_face_embedding(frames: list[bytes]) -> list[float]:
    """`frames` is a short burst captured over ~1.5-2s, not one photo - the
    vision service requires a genuine blink across them before it will hand
    back an embedding at all (see vision/app/face.py). A single still photo,
    including one held up to the camera, never blinks."""
    body = await _post_files("/v1/face/embedding", frames=frames, content_type="image/jpeg")
    return body["embedding"]
