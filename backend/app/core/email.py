"""Sends OTP/notification email via Gmail SMTP.

Requires a Gmail account with 2-Step Verification enabled and an "App
Password" generated for it (Google Account > Security > 2-Step Verification
> App passwords) - a regular account password is rejected by Gmail's SMTP
server.

Used for OTP delivery (password reset, trusted-device enrollment, document
signing - see auth/service.py, trusted_devices/service.py, esign/service.py).
An outage here must never break the underlying flow itself, so this never
raises - callers get a bool and decide what that means for them (same
contract the Teams webhook this replaces used).
"""

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from app.config import settings

logger = logging.getLogger(__name__)

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 465  # implicit TLS (SMTPS)
_TIMEOUT_SECONDS = 10.0


def _send_sync(to_email: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = settings.GMAIL_SENDER_EMAIL
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT, timeout=_TIMEOUT_SECONDS) as smtp:
        smtp.login(settings.GMAIL_SENDER_EMAIL, settings.GMAIL_APP_PASSWORD)
        smtp.send_message(message)


async def send_otp_email(to_email: str, subject: str, body: str) -> bool:
    """Best-effort delivery. Returns whether Gmail's SMTP server accepted it."""
    if not settings.GMAIL_SENDER_EMAIL or not settings.GMAIL_APP_PASSWORD:
        logger.warning("GMAIL_SENDER_EMAIL/GMAIL_APP_PASSWORD not configured - skipping email delivery.")
        return False

    try:
        await asyncio.to_thread(_send_sync, to_email, subject, body)
        return True
    except (smtplib.SMTPException, OSError):
        logger.exception("Failed to send OTP email.")
        return False
