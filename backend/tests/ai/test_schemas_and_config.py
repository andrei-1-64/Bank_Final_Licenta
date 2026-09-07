"""Schema invariants and env-only configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.ai.providers.base import ProviderError
from app.ai.providers.mock_provider import MockProvider
from app.ai.schemas import Message, ModelResponse, ToolCall, ToolResult
from app.config import ConfigurationError, Settings


def test_model_response_carries_text_or_tool_calls_not_both():
    with pytest.raises(ValidationError):
        ModelResponse(text="hi", tool_calls=[ToolCall(id="c1", name="get_balance")])

    with pytest.raises(ValidationError):
        ModelResponse()

    assert ModelResponse(text="").text == ""
    assert ModelResponse(tool_calls=[ToolCall(id="c1", name="x")]).wants_tools


def test_tool_result_round_trips_into_a_tool_message():
    result = ToolResult(tool_call_id="c1", name="get_balance", data={"balance_minor": 1})

    message: Message = result.to_message()

    assert message.role == "tool"
    assert message.tool_call_id == "c1"
    assert message.name == "get_balance"
    assert '"balance_minor": 1' in (message.content or "")


def test_mock_provider_raises_when_script_is_exhausted():
    provider = MockProvider([ModelResponse(text="only one")])
    provider.complete([])

    with pytest.raises(ProviderError):
        provider.complete([])


def test_settings_read_canonical_azure_env_vars(monkeypatch):
    for name in (
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_DEPLOYMENT",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_AI_ENDPOINT",
        "AZURE_AI_API_KEY",
        "AZURE_AI_CHAT_DEPLOYMENT",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")

    config = Settings(_env_file=None).require_azure()

    assert config.endpoint == "https://example.openai.azure.com"
    assert config.deployment == "gpt-5-mini"
    assert config.api_version  # a default is supplied


def test_settings_accept_foundry_aliases_and_normalise_the_endpoint(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)

    monkeypatch.setenv("AZURE_AI_ENDPOINT", "https://res.services.ai.azure.com/models")
    monkeypatch.setenv("AZURE_AI_API_KEY", "k")
    monkeypatch.setenv("AZURE_AI_CHAT_DEPLOYMENT", "gpt-5-mini")

    config = Settings(_env_file=None).require_azure()

    # The SDK appends /openai/deployments/... itself, so /models must go.
    assert config.endpoint == "https://res.services.ai.azure.com"


def test_missing_azure_config_raises_an_actionable_error(monkeypatch):
    for name in (
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_DEPLOYMENT",
        "AZURE_AI_ENDPOINT",
        "AZURE_AI_API_KEY",
        "AZURE_AI_CHAT_DEPLOYMENT",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ConfigurationError) as excinfo:
        Settings(_env_file=None).require_azure()

    message = str(excinfo.value)
    assert "AZURE_OPENAI_ENDPOINT" in message
    assert "AZURE_OPENAI_API_KEY" in message
    assert "AZURE_OPENAI_DEPLOYMENT" in message
