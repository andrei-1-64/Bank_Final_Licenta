import uuid

import pytest

from app.core.exceptions import (
    AccountClosedError,
    AccountNotFoundError,
    CurrencyMismatchError,
    InsufficientFundsError,
    InvalidLedgerLegsError,
)
from app.modules.ledger.models import LedgerDirection
from app.modules.ledger.schemas import LedgerLeg
from app.modules.ledger.service import get_balance, post_transaction


def _legs(from_id, to_id, amount_minor: int, currency: str = "USD") -> list[LedgerLeg]:
    return [
        LedgerLeg(from_id, LedgerDirection.DEBIT, amount_minor, currency),
        LedgerLeg(to_id, LedgerDirection.CREDIT, amount_minor, currency),
    ]


async def test_post_transaction_moves_balance(
    supabase, user_factory, account_factory, seed_balance_factory
):
    user = await user_factory()
    a = await account_factory(user, name="A")
    b = await account_factory(user, name="B")
    await seed_balance_factory(a["id"], 10_000)

    journal = await post_transaction(
        supabase, _legs(a["id"], b["id"], 2_500), idempotency_key=str(uuid.uuid4()), description="test"
    )

    assert journal["id"] is not None
    assert await get_balance(supabase, a["id"]) == 7_500
    assert await get_balance(supabase, b["id"]) == 2_500


async def test_post_transaction_rejects_unbalanced_legs(supabase, user_factory, account_factory):
    user = await user_factory()
    a = await account_factory(user, name="A")
    b = await account_factory(user, name="B")

    legs = [
        LedgerLeg(a["id"], LedgerDirection.DEBIT, 100, "USD"),
        LedgerLeg(b["id"], LedgerDirection.CREDIT, 99, "USD"),
    ]
    with pytest.raises(InvalidLedgerLegsError):
        await post_transaction(supabase, legs, idempotency_key=str(uuid.uuid4()), description="bad")


async def test_post_transaction_rejects_insufficient_funds(supabase, user_factory, account_factory):
    user = await user_factory()
    a = await account_factory(user, name="A")
    b = await account_factory(user, name="B")

    with pytest.raises(InsufficientFundsError):
        await post_transaction(
            supabase, _legs(a["id"], b["id"], 1), idempotency_key=str(uuid.uuid4()), description="overdraft"
        )


async def test_post_transaction_rejects_currency_mismatch_with_account(
    supabase, user_factory, account_factory, seed_balance_factory
):
    user = await user_factory()
    a = await account_factory(user, name="A", currency="USD")
    b = await account_factory(user, name="B", currency="USD")
    await seed_balance_factory(a["id"], 10_000)

    legs = [
        LedgerLeg(a["id"], LedgerDirection.DEBIT, 100, "EUR"),
        LedgerLeg(b["id"], LedgerDirection.CREDIT, 100, "EUR"),
    ]
    with pytest.raises(CurrencyMismatchError):
        await post_transaction(supabase, legs, idempotency_key=str(uuid.uuid4()), description="fx")


async def test_post_transaction_rejects_unknown_account(supabase, user_factory, account_factory):
    user = await user_factory()
    a = await account_factory(user, name="A")

    with pytest.raises(AccountNotFoundError):
        await post_transaction(
            supabase, _legs(a["id"], uuid.uuid4(), 100), idempotency_key=str(uuid.uuid4()), description="x"
        )


async def test_post_transaction_rejects_closed_account(
    supabase, user_factory, account_factory, seed_balance_factory
):
    user = await user_factory()
    a = await account_factory(user, name="A")
    b = await account_factory(user, name="B")
    await seed_balance_factory(a["id"], 10_000)

    await supabase.table("accounts").update({"status": "closed"}).eq("id", b["id"]).execute()

    with pytest.raises(AccountClosedError):
        await post_transaction(
            supabase, _legs(a["id"], b["id"], 100), idempotency_key=str(uuid.uuid4()), description="x"
        )


async def test_post_transaction_is_idempotent_on_replay(
    supabase, user_factory, account_factory, seed_balance_factory
):
    user = await user_factory()
    a = await account_factory(user, name="A")
    b = await account_factory(user, name="B")
    await seed_balance_factory(a["id"], 10_000)

    key = str(uuid.uuid4())
    first = await post_transaction(
        supabase, _legs(a["id"], b["id"], 1_000), idempotency_key=key, description="x"
    )
    second = await post_transaction(
        supabase, _legs(a["id"], b["id"], 1_000), idempotency_key=key, description="x"
    )

    assert first["id"] == second["id"]
    # The effect only ever happened once, even though post_transaction was
    # called twice with the same key.
    assert await get_balance(supabase, a["id"]) == 9_000
    assert await get_balance(supabase, b["id"]) == 1_000
