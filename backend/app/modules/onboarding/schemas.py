from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.ai.schemas import Message

MAX_MESSAGE_CHARS = 4000


class OnboardingChatRequest(BaseModel):
    message: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_CHARS),
    ]
    # Pre-auth: there's no user to own server-side history against (unlike
    # /chat's conversations table), so the client holds the transcript and
    # reposts it each turn - same Message shape, just round-tripped instead
    # of stored.
    history: list[Message] = Field(default_factory=list)
    #: Same field, same rationale as ChatRequest.language (see
    #: app/modules/chat/schemas.py) - Comodul is the pre-auth equivalent of
    #: the main chat, and the register page has the same language switcher.
    language: str = "ro"


class OnboardingChatResponse(BaseModel):
    reply: str
    history: list[Message]
    #: Set once the agent has called propose_registration - every
    #: /auth/register field EXCEPT password (deliberately never collected
    #: here - see onboarding/tool.py). The frontend adds the password,
    #: gathered from its own real password field, right before POSTing.
    #: None until propose_registration has been called.
    collected_fields: dict | None = None
    #: Set once check_existing_account found a match ({exists, matched_field,
    #: email}) - lets the frontend short-circuit straight to the
    #: password-reset offer instead of waiting for a later 409 on submit.
    account_conflict: dict | None = None
