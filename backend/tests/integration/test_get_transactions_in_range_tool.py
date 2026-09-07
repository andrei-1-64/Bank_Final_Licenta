"""`get_transactions_in_range` against the real Supabase-backed ledger.

The offline contract tests live in tests/ai/test_orchestrator_service.py; these
prove the entries the analytical agent is handed really do span all of the
context user's accounts, are bounded by the requested dates, and never include
anyone else's money.
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.ai.context import build_context_for_user
from app.ai.schemas import ToolCall
from app.ai.tools.insights import GetTransactionsInRangeTool


def _call(call_id: str = "c1", **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name="get_transactions_in_range", arguments=arguments)


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
    currency: str = "USD",
) -> None:
    """One ledger entry at a controllable point in the past."""
    journal = (
        await supabase.table("journal_transactions")
        .insert(
            {
                "reference": "TEST-RANGE",
                "idempotency_key": f"test-range-{uuid.uuid4()}",
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


async def test_get_transactions_in_range_returns_transactions_across_all_user_accounts(
    supabase, user_factory, account_factory
):
    """The whole point of this tool: analysis spans the person, not one account."""
    user = await user_factory()
    checking = await account_factory(user, name="Checking")
    savings = await account_factory(user, name="Savings")
    await _seed_entry(supabase, checking["id"], 1_000, days_ago=5, description="Groceries")
    await _seed_entry(supabase, savings["id"], 2_000, days_ago=3, description="Rent")

    context = await build_context_for_user(user, supabase)
    result = await GetTransactionsInRangeTool(supabase).execute(
        _call(start_date=_iso(10), end_date=_iso(0)), context
    )

    assert result.ok, result.error
    transactions = result.data["transactions"]
    assert result.data["count"] == 2

    # Oldest first - a trend reads forwards.
    assert [t["description"] for t in transactions] == ["Groceries", "Rent"]
    assert {t["account_id"] for t in transactions} == {
        str(checking["id"]),
        str(savings["id"]),
    }
    assert set(transactions[0]) == {
        "id",
        "account_id",
        "created_at",
        "amount_minor",
        "currency",
        "direction",
        "description",
        "reference",
    }
    assert transactions[0]["direction"] == "debit"
    assert transactions[0]["amount_minor"] == 1_000


async def test_get_transactions_in_range_respects_date_range_boundaries(
    supabase, user_factory, account_factory
):
    user = await user_factory()
    account = await account_factory(user, name="Checking")
    await _seed_entry(supabase, account["id"], 100, days_ago=40, description="TooOld")
    await _seed_entry(supabase, account["id"], 200, days_ago=10, description="InRange")
    await _seed_entry(supabase, account["id"], 300, days_ago=1, description="AlsoInRange")

    context = await build_context_for_user(user, supabase)
    result = await GetTransactionsInRangeTool(supabase).execute(
        _call(start_date=_iso(20), end_date=_iso(0)), context
    )

    assert result.ok, result.error
    assert [t["description"] for t in result.data["transactions"]] == [
        "InRange",
        "AlsoInRange",
    ]
    assert result.data["start_date"] == _iso(20)
    assert result.data["end_date"] == _iso(0)


async def test_get_transactions_in_range_includes_the_whole_end_day(
    supabase, user_factory, account_factory
):
    """Both bounds are inclusive whole days: an entry made earlier today must
    fall inside a range that ends today, not just before midnight."""
    user = await user_factory()
    account = await account_factory(user, name="Checking")
    await _seed_entry(supabase, account["id"], 500, days_ago=0, description="Today")

    context = await build_context_for_user(user, supabase)
    result = await GetTransactionsInRangeTool(supabase).execute(
        _call(start_date=_iso(0), end_date=_iso(0)), context
    )

    assert result.ok, result.error
    assert [t["description"] for t in result.data["transactions"]] == ["Today"]


async def test_get_transactions_in_range_returns_empty_when_no_activity_in_range(
    supabase, user_factory, account_factory
):
    """Nothing in the window is a valid answer the model must be able to say
    out loud - not an error."""
    user = await user_factory()
    account = await account_factory(user, name="Quiet")
    await _seed_entry(supabase, account["id"], 900, days_ago=100, description="Ancient")

    context = await build_context_for_user(user, supabase)
    result = await GetTransactionsInRangeTool(supabase).execute(
        _call(start_date=_iso(30), end_date=_iso(0)), context
    )

    assert result.ok, result.error
    assert result.data["transactions"] == []
    assert result.data["count"] == 0


async def test_get_transactions_in_range_rejects_invalid_date_format(
    supabase, user_factory, account_factory
):
    user = await user_factory()
    await account_factory(user, name="Checking")

    context = await build_context_for_user(user, supabase)
    result = await GetTransactionsInRangeTool(supabase).execute(
        _call(start_date="last tuesday", end_date="today"), context
    )

    assert result.ok is False
    assert "invalid input" in (result.error or "")
    assert result.data is None


async def test_get_transactions_in_range_rejects_end_before_start(
    supabase, user_factory, account_factory
):
    user = await user_factory()
    await account_factory(user, name="Checking")

    context = await build_context_for_user(user, supabase)
    result = await GetTransactionsInRangeTool(supabase).execute(
        _call(start_date=_iso(1), end_date=_iso(30)), context
    )

    assert result.ok is False
    assert "invalid input" in (result.error or "")
    assert "end_date" in (result.error or "")


async def test_get_transactions_in_range_does_not_leak_other_users_transactions(
    supabase, user_factory, account_factory
):
    """SECURITY: the account set is derived from the context user's id, and the
    tool takes no account parameter at all - there is nothing to widen."""
    alice = await user_factory()
    bob = await user_factory()
    alice_account = await account_factory(alice, name="Alice Checking")
    bob_account = await account_factory(bob, name="Bob Checking")
    await _seed_entry(supabase, alice_account["id"], 111, days_ago=2, description="AliceSpend")
    await _seed_entry(supabase, bob_account["id"], 999_999, days_ago=2, description="BobSecret")

    alice_context = await build_context_for_user(alice, supabase)
    result = await GetTransactionsInRangeTool(supabase).execute(
        _call(start_date=_iso(10), end_date=_iso(0)), alice_context
    )

    assert result.ok, result.error
    descriptions = [t["description"] for t in result.data["transactions"]]
    assert descriptions == ["AliceSpend"]
    assert "BobSecret" not in str(result.data)
    assert str(bob_account["id"]) not in str(result.data)


async def test_get_transactions_in_range_is_empty_for_user_with_no_accounts(
    supabase, user_factory
):
    """A just-registered user has no accounts, so no transactions - and that
    must not raise on the way to saying so."""
    user = await user_factory()

    context = await build_context_for_user(user, supabase)
    result = await GetTransactionsInRangeTool(supabase).execute(
        _call(start_date=_iso(30), end_date=_iso(0)), context
    )

    assert result.ok, result.error
    assert result.data["transactions"] == []
