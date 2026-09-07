from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, StringConstraints

from app.modules.chat.schemas import MAX_MESSAGE_CHARS

#: A BCP-47 locale (e.g. "ro-RO") - not validated against a fixed list,
#: same reasoning as ChatRequest.language: Azure decides what it does and
#: does not support, this backend does not maintain its own copy of that
#: list.
_Locale = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=10)]


class SpeechRequest(BaseModel):
    # Same cap as a chat message (MAX_MESSAGE_CHARS) - an AI reply can never
    # exceed it either, since it's bounded by the same constant on the way
    # in, so this is a sanity limit, not a real-world constraint.
    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_CHARS)
    ]
    #: From the frontend's own per-message language detection (see
    #: detectMessageLanguage in app.js). Falls back to AZURE_SPEECH_LANGUAGE
    #: (see Settings) when omitted.
    language: _Locale | None = None
