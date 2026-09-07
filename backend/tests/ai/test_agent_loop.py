"""The banking agent's tool loop, driven entirely by the mock provider."""

from __future__ import annotations

import json

import pytest

from app.ai.agents.banking_agent import FALLBACK_REPLY
from app.ai.schemas import Message, ModelResponse, ToolCall
from tests.ai.conftest import (
    OWNED_ACCOUNT_IDS,
    STUB_BALANCE_MINOR,
    STUB_CURRENCY,
    balance_call,
)


def user(text: str) -> list[Message]:
    return [Message(role="user", content=text)]


def tool_payload(messages: list[Message], index: int = 0) -> dict:
    """Decode the nth tool-result message from a captured provider turn."""
    tool_messages = [m for m in messages if m.role == "tool"]
    return json.loads(tool_messages[index].content or "{}")


async def test_plain_text_response_is_returned_unchanged(make_agent, context):
    """1. Model answers directly, no tool call -> agent returns that text."""
    agent, provider = make_agent([ModelResponse(text="Hello, how can I help?")])

    reply, trace = _run(await agent.run(user("hi"), context))
    assert trace == []

    assert reply == "Hello, how can I help?"
    assert provider.call_count == 1


async def test_tool_call_then_final_text(make_agent, context):
    """2. Tool call -> stub runs -> result fed back -> final text returned."""
    agent, provider = make_agent(
        [
            ModelResponse(tool_calls=[balance_call()]),
            ModelResponse(text="Your balance is $123.45."),
        ]
    )

    reply, trace = _run(await agent.run(user("what's my balance?"), context))

    assert reply == "Your balance is $123.45."
    assert provider.call_count == 2

    # The tool really ran, and its result travelled back on the tool-result path.
    second_turn = provider.calls[1]
    tool_messages = [m for m in second_turn if m.role == "tool"]
    assert len(tool_messages) == 1

    tool_message = tool_messages[0]
    assert tool_message.name == "get_balance"
    assert tool_message.tool_call_id == "call-1"

    payload = json.loads(tool_message.content or "{}")
    assert payload["ok"] is True
    assert payload["result"]["balance_minor"] == STUB_BALANCE_MINOR
    assert payload["result"]["currency"] == STUB_CURRENCY
    # The account came from the Context, not from the model.
    assert payload["result"]["account_id"] == OWNED_ACCOUNT_IDS[0]

    # The assistant turn carrying the tool call is preserved before the result.
    assistant_turns = [m for m in second_turn if m.role == "assistant"]
    assert assistant_turns[-1].tool_calls[0].name == "get_balance"

    # And the caller gets that same round trip back as a trace to persist.
    assert [m.role for m in trace] == ["assistant", "tool"]
    assert trace[0].tool_calls[0].name == "get_balance"
    assert trace[1].name == "get_balance"


async def test_tools_are_advertised_to_the_provider(make_agent, context):
    agent, provider = make_agent([ModelResponse(text="ok")])

    await agent.run(user("hi"), context)

    specs = provider.tool_specs_seen[0]
    assert [spec["function"]["name"] for spec in specs] == [
        "get_balance",
        "list_accounts",
        "list_transactions",
        "list_cards",
        "list_transfers",
        "resolve_iban_holder",
        "find_beneficiary_by_name",
        "convert_currency",
        "freeze_card",
        "unfreeze_card",
        "set_card_spending_limit",
        "add_beneficiary",
        "remove_beneficiary",
        "create_scheduled_transfer",
        "propose_card_order",
        "propose_transfer",
        "propose_payment",
        "propose_open_account",
        "propose_close_account",
        "propose_cancel_card",
    ]

    params = specs[0]["function"]["parameters"]
    assert "account_id" in params["properties"]
    # account_id is optional now — the model is not asked to supply identity.
    assert not params.get("required")

    # Nothing the first 5 (pure list/get) tools show is ever mandatory:
    # identity is supplied by the application, so the model never has to
    # produce (or guess) an identifier.
    for spec in specs[:5]:
        assert not spec["function"]["parameters"].get("required"), spec["function"]["name"]

    # resolve_iban_holder is also read-only, but genuinely needs an `iban` to
    # look up - there's no "default" IBAN to fall back to, same category of
    # exception as propose_close_account below, just for a read.
    resolve_iban_required = specs[5]["function"]["parameters"].get("required") or []
    assert specs[5]["function"]["name"] == "resolve_iban_holder"
    assert resolve_iban_required == ["iban"]

    # Beyond that, `account_id` specifically is still never mandatory -
    # EXCEPT propose_close_account, which inherently needs to know which
    # account to close and has no sane default to guess at (unlike every
    # other tool here, where "the default account" is a reasonable
    # fallback). Other required fields (last4, from_account_id/to_account_id,
    # amount, ...) are the content of the action itself, not identity.
    for spec in specs[6:]:
        required = spec["function"]["parameters"].get("required") or []
        if spec["function"]["name"] == "propose_close_account":
            assert "account_id" in required
        else:
            assert "account_id" not in required, spec["function"]["name"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"account_id": 12345},  # wrong type
        {"account_id": ""},  # fails min_length
    ],
    ids=["wrong-type", "empty"],
)
async def test_invalid_tool_input_is_reported_not_raised(make_agent, context, arguments):
    """3. Bad model-authored arguments -> validation error handled gracefully."""
    agent, provider = make_agent(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="c1", name="get_balance", arguments=arguments)]
            ),
            ModelResponse(text="I couldn't read that account."),
        ]
    )

    reply, _ = _run(await agent.run(user("balance?"), context))

    assert reply == "I couldn't read that account."

    payload = tool_payload(provider.calls[1])
    assert payload["ok"] is False
    assert "invalid input" in payload["error"]


async def test_unknown_tool_is_reported_not_raised(make_agent, context):
    agent, provider = make_agent(
        [
            ModelResponse(tool_calls=[ToolCall(id="c1", name="transfer_money")]),
            ModelResponse(text="I can't do that."),
        ]
    )

    reply, _ = _run(await agent.run(user("send money"), context))

    assert reply == "I can't do that."
    payload = tool_payload(provider.calls[1])
    assert payload["ok"] is False
    assert "unknown tool" in payload["error"]


async def test_max_iterations_guard_returns_fallback(make_agent, context):
    """4. Model only ever asks for tools -> loop caps out with a safe reply."""
    agent, provider = make_agent(
        [ModelResponse(tool_calls=[balance_call()])],
        repeat_last=True,
        max_iterations=5,
    )

    reply, _ = _run(await agent.run(user("loop forever"), context))

    assert reply == FALLBACK_REPLY
    assert provider.call_count == 5  # stopped exactly at the cap


async def test_caller_history_is_not_mutated(make_agent, context):
    agent, _ = make_agent(
        [
            ModelResponse(tool_calls=[balance_call()]),
            ModelResponse(text="done"),
        ]
    )
    history = user("balance?")

    await agent.run(history, context)

    assert len(history) == 1
    assert history[0].role == "user"


async def test_system_prompt_is_prepended(make_agent, context):
    agent, provider = make_agent([ModelResponse(text="ok")])

    await agent.run(user("hi"), context)

    assert provider.calls[0][0].role == "system"


async def test_system_prompt_carries_no_language_directive_for_romanian(make_agent, context):
    """context.language defaults to "ro" - the native language every prompt
    is already written in, so nothing should be appended (see
    app/ai/language_directive.py)."""
    agent, provider = make_agent([ModelResponse(text="ok")])

    await agent.run(user("hi"), context)

    assert provider.calls[0][0].content == agent._system_prompt


async def test_system_prompt_gets_a_language_directive_for_a_non_romanian_context(
    make_agent, context
):
    """A caller whose context carries a non-"ro" language gets the reply-
    language override appended after the agent's own (Romanian) prompt."""
    agent, provider = make_agent([ModelResponse(text="ok")])
    french_context = context.model_copy(update={"language": "fr"})

    await agent.run(user("hi"), french_context)

    system_content = provider.calls[0][0].content
    assert system_content.startswith(agent._system_prompt)
    assert "franceză" in system_content


async def test_context_identity_is_never_sent_to_the_model(make_agent, context):
    """The model is told nothing about who it is serving.

    Identity travels beside the conversation, not inside it, so there is no
    prompt content for a user to read back, contradict, or override.
    """
    agent, provider = make_agent(
        [
            ModelResponse(tool_calls=[balance_call()]),
            ModelResponse(text="done"),
        ]
    )

    await agent.run(user("who am I?"), context)

    prompt_text = " ".join(
        m.content or "" for m in provider.calls[0] if m.role != "tool"
    )
    assert context.user_id not in prompt_text
    for account_id in context.account_ids:
        assert account_id not in prompt_text


def _run(result):
    """Unpack a TurnResult the way these tests read `(reply, trace)`.

    Step 15 replaced the tuple `Agent.run` returned with `TurnResult`. The
    assertions below are about the loop, not about that shape, so they keep
    reading two values through this rather than growing a `.reply` on every
    line.
    """
    return result.reply, result.trace
