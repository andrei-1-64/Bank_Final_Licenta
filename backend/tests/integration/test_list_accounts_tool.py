"""`list_accounts` against real Supabase-backed accounts and ledger.

The offline contract tests live in tests/ai/test_tools.py; these prove the
accounts the model is handed are genuinely the context user's, with balances
that are genuinely SUM(ledger_entries).
"""

from app.ai.context import build_context_for_user
from app.ai.schemas import ToolCall
from app.ai.tools.banking import ListAccountsTool


def _call(call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name="list_accounts", arguments={})


async def test_list_accounts_returns_all_user_accounts_with_balances(
    supabase, user_factory, account_factory, seed_balance_factory
):
    user = await user_factory()
    checking = await account_factory(user, name="Checking")
    savings = await account_factory(user, name="Savings")
    await seed_balance_factory(checking["id"], 12_300)
    await seed_balance_factory(savings["id"], 45_600)

    context = await build_context_for_user(user, supabase)
    result = await ListAccountsTool(supabase).execute(_call(), context)

    assert result.ok, result.error
    accounts = result.data["accounts"]
    assert len(accounts) == 2

    by_name = {account["name"]: account for account in accounts}
    assert by_name["Checking"]["balance_minor"] == 12_300
    assert by_name["Savings"]["balance_minor"] == 45_600
    assert by_name["Checking"]["id"] == str(checking["id"])
    assert by_name["Savings"]["id"] == str(savings["id"])
    # The full shape the model is handed.
    assert set(by_name["Checking"]) == {
        "id",
        "name",
        "currency",
        "iban",
        "status",
        "balance_minor",
    }
    assert by_name["Checking"]["currency"] == "USD"
    assert by_name["Checking"]["status"] == "active"


async def test_list_accounts_returns_empty_list_for_user_with_no_accounts(
    supabase, user_factory
):
    """A just-registered user has no accounts. That is a legitimate state and
    must read as an empty list, never as an error."""
    user = await user_factory()

    context = await build_context_for_user(user, supabase)
    result = await ListAccountsTool(supabase).execute(_call(), context)

    assert result.ok, result.error
    assert result.data["accounts"] == []


async def test_list_accounts_does_not_leak_other_users_accounts(
    supabase, user_factory, account_factory, seed_balance_factory
):
    """SECURITY: the account set comes from the context user's id, so another
    user's accounts are not reachable — the tool takes no arguments at all."""
    alice = await user_factory()
    bob = await user_factory()
    alice_account = await account_factory(alice, name="Alice Checking")
    bob_account = await account_factory(bob, name="Bob Checking")
    await seed_balance_factory(bob_account["id"], 999_999)

    alice_context = await build_context_for_user(alice, supabase)
    result = await ListAccountsTool(supabase).execute(_call(), alice_context)

    assert result.ok, result.error
    returned_ids = [account["id"] for account in result.data["accounts"]]
    assert returned_ids == [str(alice_account["id"])]
    assert str(bob_account["id"]) not in returned_ids
