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
        }
        if tool_specs:
            request["tools"] = list(tool_specs)
            request["tool_choice"] = "auto"

        # Deliberately no `temperature` / `max_tokens`: the gpt-5 family rejects
        # non-default temperature and renamed the cap to `max_completion_tokens`.
        # Omitting both keeps this provider valid across deployments.
        try:
            completion = self._client.chat.completions.create(**request)
        except OpenAIError as exc:  # network, auth, quota, bad deployment, ...
            raise ProviderError(f"Azure OpenAI call failed: {exc}") from exc

        if not completion.choices:
            raise ProviderError("Azure OpenAI returned no choices")

        choice = completion.choices[0].message
        raw_calls = getattr(choice, "tool_calls", None) or []

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
