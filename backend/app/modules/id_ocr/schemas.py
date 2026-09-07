from pydantic import BaseModel


class IdOcrResult(BaseModel):
    national_id: str | None
    national_id_valid: bool
    last_name: str | None
    first_name: str | None
    address: str | None
    date_of_birth: str | None
    gender: str | None
    series_number: str | None
    #: Tesseract's own average word confidence, 0-100.
    ocr_confidence: float
    #: True when the photo probably didn't read well (see extractor.py's
    #: extract_id_fields docstring for exactly what triggers this) - the
    #: caller should ask for a clearer photo rather than trust this as-is.
    low_confidence: bool
