"""`get_balance` against the real Supabase-backed ledger.

The offline contract tests live in tests/ai/test_tools.py; these prove the
number the model is handed is genuinely SUM(ledger_entries) for an account the
context user actually owns.
"""

from app.ai.context import Context, build_context_for_user
from app.ai.schemas import ToolCall
from app.ai.tools.banking import GetBalanceTool


def _call(account_id: str | None = None, call_id: str = "c1") -> ToolCall:
    arguments = {} if account_id is None else {"account_id": account_id}
    return ToolCall(id=call_id, name="get_balance", arguments=arguments)


async def test_get_balance_returns_real_ledger_balance(
    supabase, user_factory, account_factory, seed_balance_factory
):
    user = await user_factory()
    account = await account_factory(user, name="Checking")
    await seed_balance_factory(account["id"], 87_650)

    context = await build_context_for_user(user, supabase)
    result = await GetBalanceTool(supabase).execute(_call(str(account["id"])), context)

    assert result.ok, result.error
    assert result.data == {
        "account_id": str(account["id"]),
        "balance_minor": 87_650,
        "currency": "USD",
        "source": "ledger",
    }


async def test_get_balance_uses_default_account_when_none_specified(
    supabase, user_factory, account_factory, seed_balance_factory
):
    """The model names no account - the Context's default is read."""
    user = await user_factory()
    account = await account_factory(user, name="Only Account")
    await seed_balance_factory(account["id"], 1_234)

    context = await build_context_for_user(user, supabase)
    result = await GetBalanceTool(supabase).execute(_call(), context)

    assert result.ok, result.error
    assert result.data["account_id"] == str(account["id"])
    assert result.data["balance_minor"] == 1_234


async def test_get_balance_refuses_account_not_owned_by_user(
    supabase, user_factory, account_factory, seed_balance_factory
):
    """SECURITY: naming someone else's account must not widen access, and the
    refusal must not echo the identifier back to the model."""
    alice = await user_factory()
    bob = await user_factory()
    await account_factory(alice, name="Alice Checking")
    bob_account = await account_factory(bob, name="Bob Checking")
    await seed_balance_factory(bob_account["id"], 999_999)

    alice_context = await build_context_for_user(alice, supabase)
    result = await GetBalanceTool(supabase).execute(
        _call(str(bob_account["id"])), alice_context
    )

    assert result.ok is False
    assert "access denied" in (result.error or "")
    # Never leaks the refused id, and emphatically does not fall back to
    # Alice's own account and report that instead.
    assert str(bob_account["id"]) not in (result.error or "")
    assert result.data is None


async def test_get_balance_reports_zero_for_new_account(
    supabase, user_factory, account_factory
):
    """An account with no ledger entries balances to zero, not to an error."""
    user = await user_factory()
    account = await account_factory(user, name="Fresh")

    context = await build_context_for_user(user, supabase)
    result = await GetBalanceTool(supabase).execute(_call(str(account["id"])), context)

    assert result.ok, result.error
    assert result.data["balance_minor"] == 0
    assert isinstance(result.data["balance_minor"], int)


async def test_get_balance_refuses_when_the_user_owns_no_accounts(
    supabase, user_factory
):
    """A just-registered user has an empty allowlist; the tool refuses cleanly
    rather than raising out of the loop."""
    user = await user_factory()

    context = await build_context_for_user(user, supabase)
    assert context.account_ids == ()

    result = await GetBalanceTool(supabase).execute(_call(), context)

    assert result.ok is False
    assert result.data is None


async def test_get_balance_is_scoped_by_user_id_in_the_query(
    supabase, user_factory, account_factory
):
    """Defence in depth: even a Context that has drifted out of sync with the
    database cannot read an account belonging to someone else."""
    alice = await user_factory()
    bob = await user_factory()
    bob_account = await account_factory(bob, name="Bob Checking")

    # A hand-built Context that wrongly claims Bob's account. resolve_account
    # therefore allows it, and only the query's user_id scoping stops the read.
    drifted = Context(user_id=str(alice.id), account_ids=(str(bob_account["id"]),))
    result = await GetBalanceTool(supabase).execute(
        _call(str(bob_account["id"])), drifted
    )

    assert result.ok is False
    assert result.data is None
