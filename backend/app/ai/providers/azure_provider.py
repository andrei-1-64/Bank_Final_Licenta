"""Azure OpenAI provider (gpt-5-mini via a deployment name).

All configuration comes from `Settings`; nothing Azure-specific is hard-coded.
This is the only module in the AI layer that knows the OpenAI SDK exists.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from openai import AzureOpenAI, OpenAIError

from app.ai.providers.base import ModelProvider, ProviderError, ToolSpec
from app.ai.schemas import Message, ModelResponse, ToolCall
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Hard ceiling on one model turn (reply text OR tool-call JSON, never both at
# once - see complete() below). Every agent's own prompt already asks for a
# short reply ("scurt", "3-5 propoziții"), but a prompt is a request, not a
# guarantee - this is the actual backstop against a runaway reply, not a
# tight leash: for the gpt-5 family this budget is shared with the model's
# own internal reasoning tokens, which are invisible here but paid for from
# the same pool. 1500 was tried first and confirmed too tight - a real
# InsightsAgent turn reasoning over a full detect_recurring_payments result
# before deciding to hand off burned the whole budget on reasoning and came
# back with finish_reason="length" and empty content. This value has margin
# above that observed failure; the guard right after the API call below is
# what actually protects the user if a heavier turn ever exhausts it anyway.
_MAX_COMPLETION_TOKENS = 4096


def _to_wire(message: Message) -> dict[str, Any]:
    """Translate our Message into the SDK's chat-completions shape."""
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id or "",
            "content": message.content or "",
        }

    if message.role == "assistant" and message.tool_calls:
        return {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
                for call in message.tool_calls
            ],
        }

    return {"role": message.role, "content": message.content or ""}


class AzureOpenAIProvider(ModelProvider):
    """Real provider. Sends the DEPLOYMENT name as `model`, per Azure."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: AzureOpenAI | None = None,
    ) -> None:
        self._config = (settings or get_settings()).require_azure()
        self._client = client or AzureOpenAI(
            azure_endpoint=self._config.endpoint,
            api_key=self._config.api_key,
            api_version=self._config.api_version,
        )

    @property
    def deployment(self) -> str:
        return self._config.deployment

    def complete(
        self,
        messages: Sequence[Message],
        tool_specs: Sequence[ToolSpec] | None = None,
    ) -> ModelResponse:
        request: dict[str, Any] = {
            # On Azure, `model` is the deployment name, not a base model string.
            "model": self._config.deployment,
            "messages": [_to_wire(message) for message in messages],
            "max_completion_tokens": _MAX_COMPLETION_TOKENS,
        }
        if tool_specs:
            request["tools"] = list(tool_specs)
            request["tool_choice"] = "auto"

        # Deliberately no `temperature`: the gpt-5 family rejects non-default
        # values, so omitting it keeps this provider valid across deployments.
        try:
            completion = self._client.chat.completions.create(**request)
        except OpenAIError as exc:  # network, auth, quota, bad deployment, ...
            raise ProviderError(f"Azure OpenAI call failed: {exc}") from exc

        if not completion.choices:
            raise ProviderError("Azure OpenAI returned no choices")

        choice = completion.choices[0].message
        raw_calls = getattr(choice, "tool_calls", None) or []

        if not raw_calls and not (choice.content or "").strip():
            # No tool call AND no text: either a genuinely empty completion,
            # or - the known gpt-5-mini failure mode - _MAX_COMPLETION_TOKENS
            # got spent entirely on invisible reasoning tokens before any
            # visible output. Either way, an empty reply is never routed to
            # the user as if it were a real answer; the caller's existing
            # ProviderError handling (chat/router.py -> AIProviderError) turns
            # this into a proper "try again" response instead of a blank
            # bubble.
            finish_reason = completion.choices[0].finish_reason
            raise ProviderError(
                f"Azure OpenAI returned an empty completion (finish_reason={finish_reason!r})"
            )

        tool_calls: list[ToolCall] = []
        for raw in raw_calls:
            function = getattr(raw, "function", None)
            if function is None:  # non-function tool types are not used here
                continue
            tool_calls.append(
                ToolCall(
                    id=raw.id,
                    name=function.name,
                    arguments=_decode_arguments(function.arguments),
                )
            )

        if tool_calls:
            return ModelResponse(tool_calls=tool_calls)
        return ModelResponse(text=choice.content or "")


def _decode_arguments(raw: str | None) -> dict[str, Any]:
    """Best-effort decode of model-authored JSON.

    Malformed or non-object arguments become `{}` so the tool's Pydantic schema
    rejects the call cleanly instead of the loop blowing up.
    """
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Discarding malformed tool arguments from model")
        return {}
    return decoded if isinstance(decoded, dict) else {}
