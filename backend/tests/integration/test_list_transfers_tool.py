"""`list_transfers` against real Supabase-backed transfers.

The offline contract tests live in tests/ai/test_tools.py; these prove the
tool reads the context user's real transfers, newest first, and never anyone
else's.
"""

import uuid

from app.ai.context import build_context_for_user
from app.ai.schemas import ToolCall
from app.ai.tools.banking import ListTransfersTool
from app.modules.transfers import service as transfers_service
from app.modules.transfers.schemas import TransferCreate


def _call(call_id: str = "c1", **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name="list_transfers", arguments=arguments)


async def _make_transfer(supabase, user, from_account, to_account, amount_minor: int) -> dict:
    return await transfers_service.create_transfer(
        supabase,
        user,
        TransferCreate(
            from_account_id=from_account["id"],
            to_account_id=to_account["id"],
            amount_minor=amount_minor,
            currency="USD",
        ),
        idempotency_key=f"test-transfer-{uuid.uuid4()}",
    )


async def test_list_transfers_returns_users_transfers_newest_first(
    supabase, user_factory, account_factory, seed_balance_factory
):
    user = await user_factory()
    checking = await account_factory(user, name="Checking")
    savings = await account_factory(user, name="Savings")
    await seed_balance_factory(checking["id"], 100_000)

    first = await _make_transfer(supabase, user, checking, savings, 1_000)
    second = await _make_transfer(supabase, user, checking, savings, 2_000)
    third = await _make_transfer(supabase, user, checking, savings, 3_000)

    context = await build_context_for_user(user, supabase)
    result = await ListTransfersTool(supabase).execute(_call(), context)

    assert result.ok, result.error
    transfers = result.data["transfers"]
    assert [t["id"] for t in transfers] == [
        str(third["id"]),
        str(second["id"]),
        str(first["id"]),
    ]

    assert set(transfers[0]) == {
        "id",
        "created_at",
        "from_account_id",
        "to_account_id",
        "amount_minor",
        "currency",
        "status",
    }
    assert transfers[0]["amount_minor"] == 3_000
    assert transfers[0]["currency"] == "USD"
    assert transfers[0]["from_account_id"] == str(checking["id"])
    assert transfers[0]["to_account_id"] == str(savings["id"])


async def test_list_transfers_respects_limit_parameter(
    supabase, user_factory, account_factory, seed_balance_factory
):
    user = await user_factory()
    checking = await account_factory(user, name="Checking")
    savings = await account_factory(user, name="Savings")
    await seed_balance_factory(checking["id"], 100_000)

    await _make_transfer(supabase, user, checking, savings, 1_000)
    await _make_transfer(supabase, user, checking, savings, 2_000)
    newest = await _make_transfer(supabase, user, checking, savings, 3_000)

    context = await build_context_for_user(user, supabase)
    result = await ListTransfersTool(supabase).execute(_call(limit=1), context)

    assert result.ok, result.error
    transfers = result.data["transfers"]
    assert len(transfers) == 1
    # The newest one, not an arbitrary one.
    assert transfers[0]["id"] == str(newest["id"])


async def test_list_transfers_returns_empty_list_for_user_with_no_transfers(
    supabase, user_factory, account_factory
):
    user = await user_factory()
    await account_factory(user, name="Untouched")

    context = await build_context_for_user(user, supabase)
    result = await ListTransfersTool(supabase).execute(_call(), context)

    assert result.ok, result.error
    assert result.data["transfers"] == []


async def test_list_transfers_does_not_leak_other_users_transfers(
    supabase, user_factory, account_factory, seed_balance_factory
):
    """SECURITY: the transfer set is derived from the context user's accounts."""
    alice = await user_factory()
    bob = await user_factory()

    alice_checking = await account_factory(alice, name="Alice Checking")
    alice_savings = await account_factory(alice, name="Alice Savings")
    bob_checking = await account_factory(bob, name="Bob Checking")
    bob_savings = await account_factory(bob, name="Bob Savings")
    await seed_balance_factory(alice_checking["id"], 50_000)
    await seed_balance_factory(bob_checking["id"], 50_000)

    alice_transfer = await _make_transfer(
        supabase, alice, alice_checking, alice_savings, 1_500
    )
    bob_transfer = await _make_transfer(supabase, bob, bob_checking, bob_savings, 9_900)

    alice_context = await build_context_for_user(alice, supabase)
    result = await ListTransfersTool(supabase).execute(_call(), alice_context)

    assert result.ok, result.error
    returned_ids = [transfer["id"] for transfer in result.data["transfers"]]
    assert returned_ids == [str(alice_transfer["id"])]
    assert str(bob_transfer["id"]) not in returned_ids
