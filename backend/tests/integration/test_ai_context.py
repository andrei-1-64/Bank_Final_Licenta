"""`build_context_for_user` — the AI layer's real identity boundary.

These live in integration/ rather than alongside the other identity tests in
tests/ai/ because they need a real database: the whole point of this builder is
that the account allowlist comes from Postgres, not from a caller. tests/ai/ is
deliberately DB-free (see its conftest), so it can't cover this.

The security-critical case is `test_build_context_does_not_leak_other_users_accounts`:
if the allowlist ever widened past its owner, every downstream `resolve_account`
check would inherit the leak.
"""

import pytest
from pydantic import ValidationError

from app.ai.context import Context, build_context_for_user


async def test_build_context_returns_real_user_id(supabase, user_factory, account_factory):
    user = await user_factory()
    first = await account_factory(user, name="Checking")
    second = await account_factory(user, name="Savings")

    context = await build_context_for_user(user, supabase)

    assert context.user_id == str(user.id)
    # Order-independent: the builder's job is the SET of owned accounts.
    assert set(context.account_ids) == {str(first["id"]), str(second["id"])}


async def test_build_context_with_no_accounts_returns_empty_tuple(supabase, user_factory):
    """A freshly-registered user owns nothing yet - a valid state, not an error."""
    user = await user_factory()

    context = await build_context_for_user(user, supabase)

    assert context.account_ids == ()
    assert context.user_id == str(user.id)
    assert context.default_account_id is None


async def test_build_context_does_not_leak_other_users_accounts(
    supabase, user_factory, account_factory
):
    alice = await user_factory()
    bob = await user_factory()
    alice_account = await account_factory(alice, name="Alice Checking")
    bob_account = await account_factory(bob, name="Bob Checking")

    context = await build_context_for_user(alice, supabase)

    assert context.account_ids == (str(alice_account["id"]),)
    assert str(bob_account["id"]) not in context.account_ids
    # The allowlist is the ceiling every tool resolves against, so prove the
    # refusal actually happens rather than only that the id is absent.
    assert not context.owns(str(bob_account["id"]))


async def test_build_context_returned_context_is_frozen(supabase, user_factory, account_factory):
    """Regression guard: the builder must not hand back a mutable Context."""
    user = await user_factory()
    await account_factory(user)

    context = await build_context_for_user(user, supabase)

    with pytest.raises(ValidationError):
        context.account_ids = ("acc-someone-else",)
    with pytest.raises(ValidationError):
        context.user_id = "someone-else"


async def test_build_context_ids_are_strings(supabase, user_factory, account_factory):
    """Context is typed as strings; the DB hands back UUIDs, so the builder
    is what has to do the conversion."""
    user = await user_factory()
    await account_factory(user)

    context = await build_context_for_user(user, supabase)

    assert isinstance(context, Context)
    assert isinstance(context.user_id, str)
    assert context.account_ids
    assert all(isinstance(account_id, str) for account_id in context.account_ids)
