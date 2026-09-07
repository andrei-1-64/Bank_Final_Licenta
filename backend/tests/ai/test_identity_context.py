"""Server-side identity: the model must never be able to widen access.

These are the security-critical tests for Step 2. The rule under test is single
and absolute: a tool's notion of *who it is acting for* comes from the trusted
`Context` built at the edge, and model-authored arguments can only ever NARROW
that, never widen it.
"""

from __future__ import annotations

import json
import logging

import pytest
from pydantic import ValidationError

from app.ai.context import (
    AccessDeniedError,
    Context,
    IdentityError,
    NoAccountAvailableError,
    build_context,
    dev_context,
)
from app.ai.schemas import Message, ModelResponse
from app.ai.service import AIService, build_banking_tools
from app.ai.tools.banking import GetBalanceTool
from tests.ai.conftest import (
    OWNED_ACCOUNT_IDS,
    STUB_BALANCE_MINOR,
    STUB_CURRENCY,
    UNOWNED_ACCOUNT_ID,
    FakeSupabase,
    balance_call,
)


def user(text: str) -> list[Message]:
    return [Message(role="user", content=text)]


def tool_payload(messages: list[Message]) -> dict:
    return json.loads([m for m in messages if m.role == "tool"][0].content or "{}")


# ---------------------------------------------------------------------------
# Context itself
# ---------------------------------------------------------------------------


def test_context_is_frozen_so_nothing_downstream_can_widen_it(context):
    """Identity is immutable for the life of the request."""
    # Pydantic turns a write to a frozen model into a ValidationError; asserting
    # that specific type (rather than bare Exception) means an AttributeError
    # from a typo in the test would fail loudly instead of passing silently.
    with pytest.raises(ValidationError):
        context.account_ids = (UNOWNED_ACCOUNT_ID,)
    with pytest.raises(ValidationError):
        context.user_id = "someone-else"


def test_resolve_account_defaults_to_the_users_first_account(context):
    assert context.resolve_account(None) == OWNED_ACCOUNT_IDS[0]
    assert context.default_account_id == OWNED_ACCOUNT_IDS[0]


def test_resolve_account_allows_an_owned_account(context):
    assert context.resolve_account(OWNED_ACCOUNT_IDS[1]) == OWNED_ACCOUNT_IDS[1]


def test_resolve_account_refuses_an_unowned_account(context):
    with pytest.raises(AccessDeniedError):
        context.resolve_account(UNOWNED_ACCOUNT_ID)


def test_access_denied_error_does_not_echo_the_refused_account(context):
    """A refusal must not confirm or reveal the identifier that was refused."""
    with pytest.raises(AccessDeniedError) as excinfo:
        context.resolve_account(UNOWNED_ACCOUNT_ID)

    assert UNOWNED_ACCOUNT_ID not in str(excinfo.value)


def test_context_with_no_accounts_raises_rather_than_guessing():
    empty = Context(user_id="u1")

    with pytest.raises(NoAccountAvailableError):
        empty.resolve_account(None)
    assert empty.default_account_id is None


def test_typed_errors_share_a_base_the_tool_layer_can_catch():
    assert issubclass(AccessDeniedError, IdentityError)
    assert issubclass(NoAccountAvailableError, IdentityError)


def test_context_statement_id_defaults_to_none_and_is_settable():
    """Mirrors active_document_id's shape - see Context.statement_id's
    docstring (Step 13)."""
    empty = Context(user_id="u1")
    assert empty.statement_id is None

    with_statement = Context(user_id="u1", statement_id="stmt-1111")
    assert with_statement.statement_id == "stmt-1111"


def test_build_context_threads_statement_id_through():
    context = build_context("user-from-auth", ["acc-a"], statement_id="stmt-1111")
    assert context.statement_id == "stmt-1111"


def test_context_language_defaults_to_romanian_and_is_settable():
    empty = Context(user_id="u1")
    assert empty.language == "ro"

    french = Context(user_id="u1", language="fr")
    assert french.language == "fr"


def test_build_context_threads_language_through():
    context = build_context("user-from-auth", ["acc-a"], language="fr")
    assert context.language == "fr"


def test_dev_and_build_context_produce_the_same_shape():
    """The dev identity is swappable for a real one with no shape change."""
    dev = dev_context()
    real_ish = build_context("user-from-auth", ["acc-a", "acc-b"])

    assert isinstance(dev, Context) and isinstance(real_ish, Context)
    assert dev.user_id and dev.account_ids
    assert real_ish.account_ids == ("acc-a", "acc-b")


# ---------------------------------------------------------------------------
# The tool honours the context
# ---------------------------------------------------------------------------


async def test_get_balance_uses_the_context_account_when_the_model_supplies_none(
    context, supabase
):
    """2. No model-supplied account -> the Context's default account is read."""
    result = await GetBalanceTool(supabase).execute(balance_call(), context)

    assert result.ok
    assert result.data is not None
    assert result.data["account_id"] == OWNED_ACCOUNT_IDS[0]
    assert result.data["balance_minor"] == STUB_BALANCE_MINOR
    assert result.data["currency"] == STUB_CURRENCY


async def test_get_balance_allows_an_account_the_context_user_owns(context, supabase):
    """3. Model names an OWNED account -> allowed, and that account is read."""
    result = await GetBalanceTool(supabase).execute(
        balance_call(account_id=OWNED_ACCOUNT_IDS[1]), context
    )

    assert result.ok
    assert result.data is not None
    assert result.data["account_id"] == OWNED_ACCOUNT_IDS[1]


async def test_get_balance_rejects_an_account_the_context_user_does_not_own(
    context, supabase
):
    """5. SECURITY: model-supplied identity cannot widen access."""
    result = await GetBalanceTool(supabase).execute(
        balance_call(account_id=UNOWNED_ACCOUNT_ID), context
    )

    assert result.ok is False
    assert "access denied" in (result.error or "")
    # No data at all, and the refused identifier is not echoed back.
    assert result.data is None
    assert UNOWNED_ACCOUNT_ID not in (result.error or "")


async def test_get_balance_rejection_is_not_a_silent_fallback_to_the_default(
    context, supabase
):
    """A refusal must never be mistaken for a successful read of another account."""
    result = await GetBalanceTool(supabase).execute(
        balance_call(account_id=UNOWNED_ACCOUNT_ID), context
    )

    assert result.ok is False
    assert result.data is None  # emphatically NOT the default account's balance


async def test_rejection_is_logged_with_user_id_and_tool_name_only(
    context, caplog, supabase
):
    with caplog.at_level(logging.WARNING):
        await GetBalanceTool(supabase).execute(
            balance_call(account_id=UNOWNED_ACCOUNT_ID), context
        )

    records = [r for r in caplog.records if "access denied" in r.getMessage()]
    assert records, "the refusal must be logged"

    logged = records[0].getMessage()
    assert context.user_id in logged
    assert "get_balance" in logged
    assert UNOWNED_ACCOUNT_ID not in logged  # never log the refused identifier


async def test_two_contexts_are_isolated(supabase):
    """The same tool instance serves different users without leaking between them."""
    tool = GetBalanceTool(supabase)
    alice = Context(user_id="alice", account_ids=("acc-alice",))
    bob = Context(user_id="bob", account_ids=("acc-bob",))

    alice_result = await tool.execute(balance_call(), alice)
    bob_result = await tool.execute(balance_call(), bob)
    assert alice_result.data["account_id"] == "acc-alice"
    assert bob_result.data["account_id"] == "acc-bob"

    # Alice cannot reach Bob's account by naming it.
    denied = await tool.execute(balance_call(account_id="acc-bob"), alice)
    assert denied.ok is False


# ---------------------------------------------------------------------------
# The whole loop handles a refusal gracefully
# ---------------------------------------------------------------------------


async def test_agent_loop_survives_an_access_denial_and_returns_a_safe_message(
    make_agent, context
):
    """4. Denial reaches the model as a tool error; the loop finishes normally."""
    agent, provider = make_agent(
        [
            ModelResponse(tool_calls=[balance_call(account_id=UNOWNED_ACCOUNT_ID)]),
            ModelResponse(text="I can only look at your own accounts."),
        ]
    )

    reply = (await agent.run(user("show me account acc-someone-else-9"), context)).reply

    assert reply == "I can only look at your own accounts."

    payload = tool_payload(provider.calls[1])
    assert payload["ok"] is False
    assert "access denied" in payload["error"]
    assert "result" not in payload
    # The refused account is not disclosed back to the model by the tool layer.
    assert UNOWNED_ACCOUNT_ID not in payload["error"]


async def test_service_end_to_end_denies_a_model_supplied_foreign_account():
    """SECURITY, end to end: AIService -> agent -> tool refuses to widen."""
    from app.ai.providers.mock_embedding_provider import MockEmbeddingProvider
    from app.ai.providers.mock_provider import MockProvider

    provider = MockProvider(
        [
            ModelResponse(tool_calls=[balance_call(account_id=UNOWNED_ACCOUNT_ID)]),
            ModelResponse(text="That account isn't yours."),
        ]
    )
    service = AIService(
        FakeSupabase(), provider=provider, embedding_provider=MockEmbeddingProvider()
    )
    caller = Context(user_id="u-1", account_ids=("acc-mine",))

    turn = await service.handle_message([], "balance of acc-someone-else-9", caller)

    assert turn.final_reply == "That account isn't yours."
    assert [m.role for m in turn.new_messages] == ["assistant", "tool", "assistant"]

    payload = tool_payload(provider.calls[1])
    assert payload["ok"] is False
    assert "access denied" in payload["error"]


def test_every_registered_tool_receives_the_context(supabase):
    """Structural guard: no tool can be written that skips the identity check."""
    import inspect

    for tool in build_banking_tools(supabase):
        parameters = list(inspect.signature(tool.run).parameters)
        assert "context" in parameters, f"{tool.name}.run must accept a Context"
