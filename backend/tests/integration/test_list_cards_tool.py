"""`list_cards` against real Supabase-backed cards.

The offline contract tests live in tests/ai/test_tools.py; these prove the
tool reads the context user's real cards and — the security invariant — that
the full card number, CVV and expiry never reach the model.
"""

from app.ai.context import build_context_for_user
from app.ai.schemas import ToolCall
from app.ai.tools.banking import ListCardsTool
from app.modules.cards import service as cards_service
from app.modules.cards.schemas import CardCreate


def _call(call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name="list_cards", arguments={})


async def test_list_cards_returns_only_last4_never_full_number(
    supabase, user_factory, account_factory
):
    """SECURITY INVARIANT: the row this reads carries card_number, cvv and the
    expiry; none of them may appear in what the model is handed."""
    user = await user_factory()
    account = await account_factory(user, name="Checking")
    issued = await cards_service.issue_card(
        supabase, user, CardCreate(account_id=account["id"], spending_limit_minor=50_000)
    )

    context = await build_context_for_user(user, supabase)
    result = await ListCardsTool(supabase).execute(_call(), context)

    assert result.ok, result.error
    cards = result.data["cards"]
    assert len(cards) == 1
    card = cards[0]

    assert set(card) == {"id", "account_id", "last4", "status", "spending_limit_minor"}
    assert card["last4"] == issued["last4"]
    assert card["status"] == "active"
    assert card["spending_limit_minor"] == 50_000

    # The sensitive fields are absent as keys AND as values anywhere in the
    # payload - a rename must not quietly reintroduce them.
    serialised = str(result.data)
    assert issued["card_number"] not in serialised
    assert issued["cvv"] not in serialised
    assert "card_number" not in serialised
    assert "cvv" not in serialised
    assert "expiry" not in serialised


async def test_list_cards_returns_empty_list_for_user_with_no_cards(
    supabase, user_factory, account_factory
):
    user = await user_factory()
    await account_factory(user, name="Cardless")

    context = await build_context_for_user(user, supabase)
    result = await ListCardsTool(supabase).execute(_call(), context)

    assert result.ok, result.error
    assert result.data["cards"] == []


async def test_list_cards_does_not_leak_other_users_cards(
    supabase, user_factory, account_factory
):
    """SECURITY: the card set is derived from the context user's accounts."""
    alice = await user_factory()
    bob = await user_factory()
    alice_account = await account_factory(alice, name="Alice Checking")
    bob_account = await account_factory(bob, name="Bob Checking")

    alice_card = await cards_service.issue_card(
        supabase, alice, CardCreate(account_id=alice_account["id"])
    )
    bob_card = await cards_service.issue_card(
        supabase, bob, CardCreate(account_id=bob_account["id"])
    )

    alice_context = await build_context_for_user(alice, supabase)
    result = await ListCardsTool(supabase).execute(_call(), alice_context)

    assert result.ok, result.error
    returned_ids = [card["id"] for card in result.data["cards"]]
    assert returned_ids == [str(alice_card["id"])]
    assert str(bob_card["id"]) not in returned_ids
