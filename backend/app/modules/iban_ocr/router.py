"""Lets the payments form prefill the IBAN field from a photo or PDF (a
bank card, an invoice, a bank statement, a screenshot) instead of the user
typing/pasting a 24+ character string. Authenticated - unlike id_ocr's
pre-auth registration endpoint, this only ever runs from inside the
dashboard. See extractor.py for what's actually extracted and why it's
local-only."""

from fastapi import APIRouter, Depends, File, UploadFile

from app.core.dependencies import get_current_user
from app.core.exceptions import AppError, ValidationError
from app.modules.iban_ocr.extractor import extract_iban
from app.modules.iban_ocr.schemas import IbanOcrResult
from app.modules.users.schemas import UserRead

router = APIRouter()

# A phone photo or a short bank-statement/invoice PDF comfortably fits well
# under this; mainly a guard against someone pointing an unrelated
# multi-MB file at OCR.
_MAX_UPLOAD_BYTES = 8 * 1024 * 1024
_SUFFIX_BY_CONTENT_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "application/pdf": ".pdf",
}

#: The real file signature for each accepted Content-Type, checked in
#: addition to the client-supplied header - same defence-in-depth as
#: documents/extractor.py::validate_pdf_magic_bytes. Keyed the same way as
#: `_SUFFIX_BY_CONTENT_TYPE` above, so a Content-Type that passed that
#: lookup is guaranteed to have a magic entry here too.
_MAGIC_BY_CONTENT_TYPE = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
    "application/pdf": b"%PDF-",
}


def _validate_magic_bytes(content: bytes, content_type: str) -> None:
    if not content.startswith(_MAGIC_BY_CONTENT_TYPE[content_type]):
        raise ValidationError("Fișierul nu corespunde tipului declarat.")


@router.post("/extract", response_model=IbanOcrResult)
async def extract(
    file: UploadFile = File(...),
    _user: UserRead = Depends(get_current_user),
) -> IbanOcrResult:
    suffix = _SUFFIX_BY_CONTENT_TYPE.get(file.content_type)
    if suffix is None:
        raise ValidationError("Only PNG, JPEG, or PDF files are supported.")

    contents = await file.read()
    if not contents:
        raise ValidationError("Uploaded file is empty.")
    if len(contents) > _MAX_UPLOAD_BYTES:
        raise ValidationError("File is too large (max 8 MB).")
    # file.content_type is one of _SUFFIX_BY_CONTENT_TYPE's keys here (the
    # `suffix is None` branch above already rejected anything else), so it
    # is always present in _MAGIC_BY_CONTENT_TYPE too.
    assert file.content_type is not None
    _validate_magic_bytes(contents, file.content_type)

    try:
        # The suffix travels with the upload: vision-service picks its PDF
        # reader or its image reader from it, exactly as the local extractor
        # used to branch on the temp file's suffix.
        fields = await extract_iban(contents, filename=f"upload{suffix}")
    except AppError:
        # A ValidationError (unreadable file) and a 502 (vision-service
        # down) are different problems - keep them distinguishable.
        raise
    except Exception as exc:
        raise ValidationError("Could not read the file. Try a clearer photo or PDF.") from exc

    return IbanOcrResult(**{key: value for key, value in fields.items() if key != "raw_text"})
