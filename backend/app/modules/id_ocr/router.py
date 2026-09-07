"""Pre-auth endpoint: lets the registration form prefill itself from a photo
of the user's ID card. No session required - this runs before an account
exists. See extractor.py for what's actually extracted and why it's
local-only (no image ever leaves this machine)."""

from fastapi import APIRouter, File, UploadFile

from app.core.exceptions import AppError, ValidationError
from app.modules.id_ocr.extractor import extract_id_fields
from app.modules.id_ocr.schemas import IdOcrResult

router = APIRouter()

# A phone photo of an ID card comfortably fits well under this; mainly a
# guard against someone pointing an unrelated multi-MB file at OCR.
_MAX_UPLOAD_BYTES = 8 * 1024 * 1024

#: The real PNG file signature (ISO/IEC 15948). Checked in addition to the
#: client-supplied Content-Type header, which is trivially spoofable - same
#: defence-in-depth as documents/extractor.py::validate_pdf_magic_bytes.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _validate_png_magic_bytes(content: bytes) -> None:
    if not content.startswith(_PNG_MAGIC):
        raise ValidationError("Fișierul nu corespunde tipului declarat.")


@router.post("/extract", response_model=IdOcrResult)
async def extract(file: UploadFile = File(...)) -> IdOcrResult:
    if file.content_type != "image/png":
        raise ValidationError("Only PNG images are supported.")

    contents = await file.read()
    if not contents:
        raise ValidationError("Uploaded file is empty.")
    if len(contents) > _MAX_UPLOAD_BYTES:
        raise ValidationError("Image is too large (max 8 MB).")
    _validate_png_magic_bytes(contents)

    try:
        fields = await extract_id_fields(contents)
    except AppError:
        # Already a well-shaped error (a ValidationError for an unusable
        # photo, or a 502 when vision-service is down) - don't flatten those
        # two very different situations into one message.
        raise
    except Exception as exc:
        # Anything else means the same thing to the client: this isn't a
        # readable ID photo, fill it in manually.
        raise ValidationError("Could not read the ID photo. Try a clearer image.") from exc

    return IdOcrResult(**{key: value for key, value in fields.items() if key != "raw_text"})
