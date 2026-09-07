"""Shared fixtures for the AI layer's tests.

Every test in this suite is OFFLINE: the mock provider is the only provider
used, no Azure credentials are read or required, and the OpenAI SDK is never
imported. These tests also touch no database - see the `clean_db` override
below for why that needs saying out loud.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ai.agents.banking_agent import BankingAgent
from app.ai.context import Context
from app.ai.providers.mock_provider import MockProvider
from app.ai.schemas import ModelResponse, ToolCall
from app.ai.service import build_banking_tools
from app.ai.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def clean_db():
    """Override the DB-truncating autouse fixture from tests/conftest.py.

    tests/conftest.py declares `clean_db` as autouse, so without this it
    would apply to the whole tests/ tree - dragging the real Supabase
    project (via the session-scoped `supabase` fixture) into this suite,
    which is deliberately offline and DB-free. pytest resolves fixtures by
    name from the closest conftest, so defining it here shadows the
    parent's for tests/ai only; unit/ and integration/ still get the real
    one.
    """
    return None


# The accounts the test user owns, and one they emphatically do not. Tests use
# UNOWNED_ACCOUNT_ID whenever they play the part of a model trying to widen access.
OWNED_ACCOUNT_IDS = ("acc-owned-1", "acc-owned-2")
UNOWNED_ACCOUNT_ID = "acc-someone-else-9"
TEST_USER_ID = "user-under-test"

# What the fake Supabase below reports for any account it is asked about.
STUB_BALANCE_MINOR = 4_242
STUB_CURRENCY = "USD"
# A real UUID: the tool parses `account["id"]` before calling the ledger, so
# the fake's row has to look like a genuine database row.
STUB_ACCOUNT_ROW_ID = "11111111-2222-3333-4444-555555555555"


class _FakeQuery:
    """Swallows the PostgREST builder chain and returns a canned payload."""

    def __init__(self, data: object) -> None:
        self._data = data

    def select(self, *args: object, **kwargs: object) -> _FakeQuery:
        return self

    def eq(self, *args: object, **kwargs: object) -> _FakeQuery:
        return self

    def maybe_single(self) -> _FakeQuery:
        return self

    async def execute(self) -> SimpleNamespace:
        return SimpleNamespace(data=self._data)


class FakeSupabase:
    """Offline stand-in for `supabase.AsyncClient`.

    Only the two calls `get_balance` makes are supported: the ownership-scoped
    `accounts` read and the `get_account_balance` RPC. It answers for whatever
    account it is asked about - ownership is enforced by `Context` in memory,
    well before any of this is reached, so these tests never depend on the fake
    to refuse anything.
    """

    def __init__(
        self, balance_minor: int = STUB_BALANCE_MINOR, currency: str = STUB_CURRENCY
    ) -> None:
        self.balance_minor = balance_minor
        self.currency = currency

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery({"id": STUB_ACCOUNT_ROW_ID, "currency": self.currency})

    def rpc(self, function_name: str, params: dict | None = None) -> _FakeQuery:
        return _FakeQuery(self.balance_minor)


@pytest.fixture
def supabase() -> FakeSupabase:
    """Offline DB stand-in. Overrides the real client fixture in the parent
    conftest for this suite, exactly as `clean_db` above does."""
    return FakeSupabase()


@pytest.fixture
def context() -> Context:
    """A trusted identity, as the edge would build it."""
    return Context(user_id=TEST_USER_ID, account_ids=OWNED_ACCOUNT_IDS)


@pytest.fixture
def tools(supabase: FakeSupabase) -> ToolRegistry:
    return build_banking_tools(supabase)


@pytest.fixture
def make_agent(tools: ToolRegistry):
    """Build a BankingAgent over a scripted mock provider."""

    def _make(
        script: list[ModelResponse],
        *,
        repeat_last: bool = False,
        max_iterations: int = 5,
    ) -> tuple[BankingAgent, MockProvider]:
        provider = MockProvider(script, repeat_last=repeat_last)
        agent = BankingAgent(provider, tools, max_iterations=max_iterations)
        return agent, provider

    return _make


def balance_call(account_id: str | None = None, call_id: str = "call-1") -> ToolCall:
    """A get_balance tool call, optionally naming an account.

    Passing no account_id is the normal case now: the tool resolves the account
    from the Context instead.
    """
    arguments = {} if account_id is None else {"account_id": account_id}
    return ToolCall(id=call_id, name="get_balance", arguments=arguments)
