from pydantic import BaseModel


class IbanOcrResult(BaseModel):
    iban: str | None
    ocr_confidence: float
    low_confidence: bool
