"""Azure AI Speech text-to-speech - see app/config.py's AZURE_SPEECH_* and
Settings.require_azure_speech().

One configured "Multilingual" neural voice (AZURE_SPEECH_VOICE) speaks
whatever language the text itself is in - not just the 9 the frontend's UI
supports (see detectMessageLanguage in app.js), any of the several dozen
languages Azure documents that voice family as covering. That's not this
module picking a per-language voice or tagging the text with the target
language: a "Multilingual" voice auto-detects the language straight from
the text and switches on its own, confirmed directly against the real
resource with Polish and Japanese - both spoken correctly with no language
hint at all in the request. `language`, when given, only sets the SSML
root's xml:lang (a required attribute, and a hint for punctuation/prosody
defaults) - it never wraps or constrains what the voice actually detects
and speaks. Plain (non-multilingual) voices don't have this auto-detection
and mostly just keep speaking in their own native language regardless, so
AZURE_SPEECH_VOICE being multilingual is a real requirement, not a nicety.
"""

from __future__ import annotations

import logging
from xml.sax.saxutils import escape

import httpx

from app.config import AzureSpeechConfig, ConfigurationError, settings
from app.core.exceptions import SpeechProviderError, SpeechServiceUnavailableError

logger = logging.getLogger(__name__)

#: Text-to-speech for a chat-message-length string is fast, but still a
#: network hop to Azure - well past httpx's 5s default.
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"


def _require_config() -> AzureSpeechConfig:
    try:
        return settings.require_azure_speech()
    except ConfigurationError as exc:
        raise SpeechServiceUnavailableError() from exc


def _build_ssml(text: str, language: str, voice: str) -> str:
    # escape(), not an f-string straight into the XML: `text` is model- or
    # user-authored and can contain &, <, > - unescaped, either breaks the
    # SSML or (worse) lets it inject markup Azure would then interpret.
    #
    # No inner <lang> tag around the text (an earlier version of this had
    # one, naming the same `language` as here): that forces the voice to
    # treat the text AS that language rather than letting it detect for
    # itself, which only helps when the caller's guess is right and
    # actively hurts when it isn't - and this endpoint has no reliable way
    # to know the language of arbitrary text up front. The multilingual
    # voice's own auto-detection already covers everything the frontend's
    # 9-language detector does plus everything it doesn't.
    return (
        "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' "
        f"xml:lang='{escape(language)}'>"
        f"<voice name='{escape(voice)}'>{escape(text)}</voice></speak>"
    )


async def synthesize(text: str, language: str | None) -> bytes:
    """Returns MP3 bytes for `text`, spoken by the configured multilingual
    voice, which detects the language on its own (see this module's
    docstring) - `language` only fills the SSML root's required xml:lang
    attribute, as a hint, never a constraint. Raises
    SpeechServiceUnavailableError if Azure Speech isn't configured, or
    SpeechProviderError if the Azure call itself fails."""
    config = _require_config()
    ssml = _build_ssml(text, language or config.default_language, config.voice)
    url = f"{config.endpoint.rstrip('/')}/cognitiveservices/v1"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                url,
                content=ssml.encode("utf-8"),
                headers={
                    "Ocp-Apim-Subscription-Key": config.key,
                    "Content-Type": "application/ssml+xml",
                    "X-Microsoft-OutputFormat": _OUTPUT_FORMAT,
                    "User-Agent": "BanK-backend",
                },
            )
    except httpx.HTTPError as exc:
        # The URL is safe to log (no secret in it); the key is not, and is
        # never included.
        logger.exception("Azure Speech request failed: %s", url)
        raise SpeechProviderError() from exc

    if response.status_code >= 400:
        logger.error(
            "Azure Speech returned %s for %s: %s",
            response.status_code,
            url,
            response.text[:500],
        )
        raise SpeechProviderError()

    return response.content
