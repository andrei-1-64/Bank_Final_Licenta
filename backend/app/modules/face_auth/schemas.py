from pydantic import BaseModel


class FaceStatusRead(BaseModel):
    enrolled: bool


class FaceConfirmationRead(BaseModel):
    token: str
