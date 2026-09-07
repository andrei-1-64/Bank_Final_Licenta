"""`categorize_transactions` against the real Supabase-backed ledger."""

import json
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from app.ai.context import build_context_for_user
from app.ai.providers.base import ModelProvider, ProviderError, ToolSpec
from app.ai.providers.mock_provider import MockProvider
from app.ai.schemas import Message, ModelResponse, ToolCall
from app.ai.tools.insights import CategorizeTransactionsTool


def _call(call_id: str = "c1", **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name="categorize_transactions", arguments=arguments)


def _tool(supabase) -> CategorizeTransactionsTool:
    """A provider whose reply is deliberately not valid JSON: these tests
    only exercise the keyword layer, so the few-shot call (still made,
    since the tool always has a provider now) should parse to "no
    improvement" and leave categorization exactly as the keyword matcher
    left it - see _parse_classification_response's JSONDecodeError branch."""
    return CategorizeTransactionsTool(supabase, MockProvider([ModelResponse(text="not json")]))


class _EchoClassifierProvider(ModelProvider):
    """Classifies whatever it's actually asked about, by reading the
    descriptions back out of the final prompt turn - so the test doesn't
    need to predict the exact normalized cache key
    (description + reference, lowercased) the tool builds internally.
    Every description it sees gets mapped to `category`."""

    def __init__(self, category: str) -> None:
        self._category = category
        self.calls: list[list[Message]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def complete(
        self, messages: Sequence[Message], tool_specs: Sequence[ToolSpec] | None = None
    ) -> ModelResponse:
        self.calls.append(list(messages))
        final_prompt = messages[-1].content
        descriptions = re.findall(r'- "(.*)"', final_prompt)
        return ModelResponse(text=json.dumps({d: self._category for d in descriptions}))


class _RaisingProvider(ModelProvider):
    """Simulates a provider outage on every call."""

    def complete(
        self, messages: Sequence[Message], tool_specs: Sequence[ToolSpec] | None = None
    ) -> ModelResponse:
        raise ProviderError("simulated outage")


def _iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).date().isoformat()


async def _seed_entry(
    supabase,
    account_id,
    amount_minor: int,
    *,
    days_ago: int = 0,
    direction: str = "debit",
    description: str = "Test entry",
    currency: str = "RON",
) -> None:
    journal = (
        await supabase.table("journal_transactions")
        .insert(
            {
                "reference": "TEST-CATEGORIZE",
                "idempotency_key": f"test-categorize-{uuid.uuid4()}",
                "description": description,
            }
        )
        .execute()
    ).data[0]
    await supabase.table("ledger_entries").insert(
        {
            "journal_id": journal["id"],
            "account_id": str(account_id),
            "direction": direction,
            "amount_minor": amount_minor,
            "currency": currency,
            "created_at": (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(),
        }
    ).execute()


async def test_categorize_assigns_correct_categories(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    await _seed_entry(supabase, account["id"], 3_000, description="Spotify Premium")
    await _seed_entry(supabase, account["id"], 15_000, description="Lidl Cluj")
    await _seed_entry(supabase, account["id"], 5_000, description="Random Thing Store")

    context = await build_context_for_user(user, supabase)
    result = await _tool(supabase).execute(
        _call(start_date=_iso(5), end_date=_iso(0)), context
    )

    assert result.ok, result.error
    by_name = {c["name"]: c for c in result.data["categories"]}
    assert by_name["Divertisment"]["count"] == 1
    assert by_name["Divertisment"]["total_minor"] == 3_000
    assert by_name["Cumpărături alimentare"]["count"] == 1
    assert by_name["Cumpărături alimentare"]["total_minor"] == 15_000
    assert by_name["Altele"]["count"] == 1
    assert result.data["total_transactions"] == 3
    assert result.data["uncategorized_count"] == 1


async def test_categorize_percentages_sum_to_100(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    await _seed_entry(supabase, account["id"], 3_000, description="Spotify")
    await _seed_entry(supabase, account["id"], 7_000, description="Lidl")
    await _seed_entry(supabase, account["id"], 2_000, description="Mystery Store")

    context = await build_context_for_user(user, supabase)
    result = await _tool(supabase).execute(
        _call(start_date=_iso(5), end_date=_iso(0)), context
    )

    assert result.ok, result.error
    total_percentage = sum(c["percentage"] for c in result.data["categories"])
    assert total_percentage == pytest.approx(100.0, abs=0.1)


async def test_categorize_handles_empty_range(supabase, user_factory, account_factory):
    user = await user_factory()
    await account_factory(user)

    context = await build_context_for_user(user, supabase)
    result = await _tool(supabase).execute(
        _call(start_date=_iso(5), end_date=_iso(0)), context
    )

    assert result.ok, result.error
    assert result.data["categories"] == []
    assert result.data["total_transactions"] == 0
    assert result.data["uncategorized_count"] == 0


async def test_categorize_is_case_insensitive(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    await _seed_entry(supabase, account["id"], 1_000, description="SPOTIFY", days_ago=1)
    await _seed_entry(supabase, account["id"], 1_000, description="spotify premium", days_ago=0)

    context = await build_context_for_user(user, supabase)
    result = await _tool(supabase).execute(
        _call(start_date=_iso(5), end_date=_iso(0)), context
    )

    assert result.ok, result.error
    by_name = {c["name"]: c for c in result.data["categories"]}
    assert by_name["Divertisment"]["count"] == 2


# ---------------------------------------------------------------------------
# The few-shot LLM layer: only reached for whatever the keyword map leaves
# as "Altele" - see categorize_transactions.py's module docstring.
# ---------------------------------------------------------------------------


async def test_llm_classifier_improves_an_otherwise_uncategorized_merchant(
    supabase, user_factory, account_factory
):
    user = await user_factory()
    account = await account_factory(user)
    await _seed_entry(supabase, account["id"], 5_000, description="Totally Novel Merchant Xyz")

    context = await build_context_for_user(user, supabase)
    provider = _EchoClassifierProvider("Electronice")
    result = await CategorizeTransactionsTool(supabase, provider).execute(
        _call(start_date=_iso(5), end_date=_iso(0)), context
    )

    assert result.ok, result.error
    by_name = {c["name"]: c for c in result.data["categories"]}
    assert "Electronice" in by_name
    assert by_name["Electronice"]["count"] == 1
    assert "Altele" not in by_name
    assert result.data["uncategorized_count"] == 0
    assert provider.call_count == 1


async def test_llm_classification_is_cached_and_not_repeated_for_the_same_merchant(
    supabase, user_factory, account_factory
):
    """A merchant classified once should be served from
    merchant_category_cache on the next call, never a second LLM round-trip
    - and the cache is global, so a second (different) user benefits too."""
    user_a = await user_factory()
    account_a = await account_factory(user_a)
    description = f"Unique Merchant {uuid.uuid4()}"
    await _seed_entry(supabase, account_a["id"], 5_000, description=description)

    context_a = await build_context_for_user(user_a, supabase)
    provider = _EchoClassifierProvider("Îmbrăcăminte")
    first = await CategorizeTransactionsTool(supabase, provider).execute(
        _call(start_date=_iso(5), end_date=_iso(0)), context_a
    )
    assert first.ok, first.error
    assert provider.call_count == 1

    user_b = await user_factory()
    account_b = await account_factory(user_b)
    await _seed_entry(supabase, account_b["id"], 1_000, description=description)
    context_b = await build_context_for_user(user_b, supabase)

    second = await CategorizeTransactionsTool(supabase, provider).execute(
        _call(start_date=_iso(5), end_date=_iso(0)), context_b
    )

    assert second.ok, second.error
    by_name = {c["name"]: c for c in second.data["categories"]}
    assert by_name["Îmbrăcăminte"]["count"] == 1
    # No new call: the second user's identical merchant was served from cache.
    assert provider.call_count == 1


async def test_llm_classifier_degrades_gracefully_on_provider_outage(
    supabase, user_factory, account_factory
):
    """A provider outage must never break the widget - the entry just stays
    "Altele", same as if no provider had been supplied at all."""
    user = await user_factory()
    account = await account_factory(user)
    await _seed_entry(supabase, account["id"], 5_000, description="Some Outage Merchant")

    context = await build_context_for_user(user, supabase)
    result = await CategorizeTransactionsTool(supabase, _RaisingProvider()).execute(
        _call(start_date=_iso(5), end_date=_iso(0)), context
    )

    assert result.ok, result.error
    by_name = {c["name"]: c for c in result.data["categories"]}
    assert by_name["Altele"]["count"] == 1
    assert result.data["uncategorized_count"] == 1
