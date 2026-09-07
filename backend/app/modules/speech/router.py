"""/speech - text-to-speech for the chat "read aloud" button.

Stateless: nothing here is persisted, there's no ownership to check beyond
"the caller is logged in" (get_current_user), which exists mainly to keep
this - a paid Azure call - from being an open proxy anyone can hit.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.core.dependencies import get_current_user
from app.modules.speech import service as speech_service
from app.modules.speech.schemas import SpeechRequest
from app.modules.users.schemas import UserRead

router = APIRouter()


@router.post("")
async def synthesize_speech(
    payload: SpeechRequest,
    user: UserRead = Depends(get_current_user),
) -> Response:
    audio = await speech_service.synthesize(payload.text, payload.language)
    return Response(content=audio, media_type="audio/mpeg")
