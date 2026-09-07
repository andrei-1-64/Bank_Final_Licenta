"""`list_transactions` against the real Supabase-backed ledger.

The offline contract tests live in tests/ai/test_tools.py; these prove the
entries the model is handed come from the real ledger, are windowed by
`days_back`, capped by `limit`, and scoped to an account the context user
actually owns.
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.ai.context import build_context_for_user
from app.ai.schemas import ToolCall
from app.ai.tools.banking import ListTransactionsTool


def _call(call_id: str = "c1", **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name="list_transactions", arguments=arguments)


async def _seed_entry(
    supabase,
    account_id,
    amount_minor: int,
    *,
    days_ago: int = 0,
    direction: str = "credit",
    description: str = "Test entry",
    currency: str = "USD",
) -> None:
    """One ledger entry at a controllable point in the past.

    `seed_balance_factory` always writes at now(); the date window is exactly
    what these tests need to vary, hence the local helper.
    """
    journal = (
        await supabase.table("journal_transactions")
        .insert(
            {
                "reference": "TEST-TX",
                "idempotency_key": f"test-tx-{uuid.uuid4()}",
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


async def test_list_transactions_returns_transactions_in_default_range(
    supabase, user_factory, account_factory
):
    user = await user_factory()
    account = await account_factory(user, name="Checking")
    await _seed_entry(supabase, account["id"], 5_000, days_ago=1, description="Salary")
    await _seed_entry(
        supabase, account["id"], 1_200, days_ago=3, direction="debit", description="Groceries"
    )

    context = await build_context_for_user(user, supabase)
    result = await ListTransactionsTool(supabase).execute(
        _call(account_id=str(account["id"])), context
    )

    assert result.ok, result.error
    assert result.data["account_id"] == str(account["id"])
    assert result.data["days_back"] == 30

    transactions = result.data["transactions"]
    assert len(transactions) == 2
    # Newest first.
    assert [t["description"] for t in transactions] == ["Salary", "Groceries"]
    assert transactions[0]["amount_minor"] == 5_000
    assert transactions[0]["direction"] == "credit"
    assert transactions[1]["direction"] == "debit"
    assert set(transactions[0]) == {
        "id",
        "created_at",
        "amount_minor",
        "currency",
        "direction",
        "description",
        "reference",
    }


async def test_list_transactions_respects_days_back_parameter(
    supabase, user_factory, account_factory
):
    user = await user_factory()
    account = await account_factory(user, name="Checking")
    await _seed_entry(supabase, account["id"], 1_000, days_ago=2, description="Recent")
    await _seed_entry(supabase, account["id"], 2_000, days_ago=45, description="Older")

    context = await build_context_for_user(user, supabase)
    tool = ListTransactionsTool(supabase)

    # A 7-day window sees only the recent one...
    narrow = await tool.execute(_call(account_id=str(account["id"]), days_back=7), context)
    assert narrow.ok, narrow.error
    assert [t["description"] for t in narrow.data["transactions"]] == ["Recent"]

    # ...and a 90-day window sees both.
    wide = await tool.execute(_call(account_id=str(account["id"]), days_back=90), context)
    assert wide.ok, wide.error
    assert [t["description"] for t in wide.data["transactions"]] == ["Recent", "Older"]


async def test_list_transactions_respects_limit_parameter(
    supabase, user_factory, account_factory
):
    user = await user_factory()
    account = await account_factory(user, name="Checking")
    for day in range(1, 6):
        await _seed_entry(supabase, account["id"], 100 * day, days_ago=day, description=f"E{day}")

    context = await build_context_for_user(user, supabase)
    result = await ListTransactionsTool(supabase).execute(
        _call(account_id=str(account["id"]), limit=2), context
    )

    assert result.ok, result.error
    transactions = result.data["transactions"]
    assert len(transactions) == 2
    # Still the newest ones, not an arbitrary two.
    assert [t["description"] for t in transactions] == ["E1", "E2"]


async def test_list_transactions_uses_default_account_when_account_id_none(
    supabase, user_factory, account_factory
):
    """The model names no account - the Context's default is read."""
    user = await user_factory()
    account = await account_factory(user, name="Only Account")
    await _seed_entry(supabase, account["id"], 7_700, days_ago=1, description="Only entry")

    context = await build_context_for_user(user, supabase)
    result = await ListTransactionsTool(supabase).execute(_call(), context)

    assert result.ok, result.error
    assert result.data["account_id"] == str(account["id"])
    assert [t["description"] for t in result.data["transactions"]] == ["Only entry"]


async def test_list_transactions_refuses_account_not_owned_by_user(
    supabase, user_factory, account_factory
):
    """SECURITY: naming someone else's account must not widen access, and the
    refusal must not echo the identifier back to the model."""
    alice = await user_factory()
    bob = await user_factory()
    await account_factory(alice, name="Alice Checking")
    bob_account = await account_factory(bob, name="Bob Checking")
    await _seed_entry(supabase, bob_account["id"], 999_999, days_ago=1, description="Bob secret")

    alice_context = await build_context_for_user(alice, supabase)
    result = await ListTransactionsTool(supabase).execute(
        _call(account_id=str(bob_account["id"])), alice_context
    )

    assert result.ok is False
    assert "access denied" in (result.error or "")
    # Never leaks the refused id, and emphatically does not fall back to
    # Alice's own account and report that instead.
    assert str(bob_account["id"]) not in (result.error or "")
    assert result.data is None


async def test_list_transactions_returns_empty_list_when_no_activity_in_range(
    supabase, user_factory, account_factory
):
    """Nothing in the window is a legitimate answer, not an error."""
    user = await user_factory()
    account = await account_factory(user, name="Quiet")
    await _seed_entry(supabase, account["id"], 3_000, days_ago=100, description="Ancient")

    context = await build_context_for_user(user, supabase)
    result = await ListTransactionsTool(supabase).execute(
        _call(account_id=str(account["id"]), days_back=30), context
    )

    assert result.ok, result.error
    assert result.data["transactions"] == []
