"""Sends a message to a Microsoft Teams chat/channel via a "Workflows"
incoming webhook URL (Teams: channel > Workflows > "Send webhook alerts to
a chat" template). One-way, fire-and-forget HTTP POST - Teams turns the
JSON body into a message wherever that workflow was configured to post.

The trigger this template creates ("When a Teams webhook request is
received") does NOT accept a plain {"text": ...} body - its Request Body
JSON Schema requires a `type: "message"` envelope with an `attachments`
array holding an Adaptive Card (see the flow's Code view for the exact
schema). Sending anything else makes the "Attachments is null" branch fire
and the downstream "Post card in a chat or channel" action fail with
BadRequest - discovered by inspecting a failed run's history.

Used only for password-reset OTP delivery (see auth/service.py). A Teams
outage must never be able to break the reset flow itself, so this never
raises - callers get a bool and decide what that means for them.
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10.0
_ADAPTIVE_CARD_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"


def _adaptive_card_message(text: str) -> dict:
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": _ADAPTIVE_CARD_SCHEMA,
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": text,
                            "wrap": True,
                        }
                    ],
                },
            }
        ],
    }


async def send_teams_message(text: str) -> bool:
    """Best-effort delivery. Returns whether the webhook accepted it."""
    if not settings.TEAMS_WEBHOOK_URL:
        logger.warning("TEAMS_WEBHOOK_URL not configured - skipping Teams notification.")
        return False

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(settings.TEAMS_WEBHOOK_URL, json=_adaptive_card_message(text))
            resp.raise_for_status()
        return True
    except httpx.HTTPError:
        logger.exception("Failed to send Teams notification.")
        return False
