"""The banking agent's write-capable tools (freeze/unfreeze_card,
set_card_spending_limit, add/remove_beneficiary, create_scheduled_transfer)
and the propose-only propose_card_order - against real Supabase.

Offline contract tests (spec shape, registration order) live in
tests/ai/test_tools.py; these prove each tool actually performs (or, for
propose_card_order, deliberately does NOT perform) the real write, and that
ownership is enforced through Context the same way the read-only tools are.
"""

from app.ai.context import build_context_for_user
from app.ai.schemas import ToolCall
from app.ai.tools.banking import (
    AddBeneficiaryTool,
    CreateScheduledTransferTool,
    FreezeCardTool,
    ProposeCardOrderTool,
    RemoveBeneficiaryTool,
    SetCardSpendingLimitTool,
    UnfreezeCardTool,
)
from app.modules.beneficiaries import service as beneficiaries_service
from app.modules.cards import service as cards_service
from app.modules.cards.schemas import CardCreate

VALID_IBAN = "RO49AAAA1B31007593840000"


def _call(name: str, call_id: str = "c1", **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


async def test_freeze_and_unfreeze_card_by_last4(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    issued = await cards_service.issue_card(supabase, user, CardCreate(account_id=account["id"]))

    context = await build_context_for_user(user, supabase)

    freeze_result = await FreezeCardTool(supabase).execute(
        _call("freeze_card", last4=issued["last4"]), context
    )
    assert freeze_result.ok, freeze_result.error
    assert freeze_result.data["status"] == "frozen"

    unfreeze_result = await UnfreezeCardTool(supabase).execute(
        _call("unfreeze_card", last4=issued["last4"]), context
    )
    assert unfreeze_result.ok, unfreeze_result.error
    assert unfreeze_result.data["status"] == "active"


async def test_freeze_card_does_not_reach_other_users_cards(
    supabase, user_factory, account_factory
):
    alice = await user_factory()
    bob = await user_factory()
    bob_account = await account_factory(bob)
    bob_card = await cards_service.issue_card(
        supabase, bob, CardCreate(account_id=bob_account["id"])
    )

    alice_context = await build_context_for_user(alice, supabase)
    result = await FreezeCardTool(supabase).execute(
        _call("freeze_card", last4=bob_card["last4"]), alice_context
    )
    # NotFoundError isn't an IdentityError, so it surfaces through the
    # generic "tool execution failed" path (see Tool.execute) rather than
    # the "access denied" one - either way, the call must fail, not freeze
    # someone else's card.
    assert not result.ok
    assert "notfounderror" in (result.error or "").lower().replace(" ", "")


async def test_set_card_spending_limit(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    issued = await cards_service.issue_card(supabase, user, CardCreate(account_id=account["id"]))
    context = await build_context_for_user(user, supabase)

    result = await SetCardSpendingLimitTool(supabase).execute(
        _call("set_card_spending_limit", last4=issued["last4"], spending_limit_minor=25_000),
        context,
    )
    assert result.ok, result.error
    assert result.data["spending_limit_minor"] == 25_000

    removed = await SetCardSpendingLimitTool(supabase).execute(
        _call("set_card_spending_limit", last4=issued["last4"]), context
    )
    assert removed.ok, removed.error
    assert removed.data["spending_limit_minor"] is None


async def test_add_and_remove_beneficiary(supabase, user_factory):
    user = await user_factory()
    context = await build_context_for_user(user, supabase)

    add_result = await AddBeneficiaryTool(supabase).execute(
        _call("add_beneficiary", iban=VALID_IBAN, display_name="Ana Pop"), context
    )
    assert add_result.ok, add_result.error
    assert add_result.data["iban"] == VALID_IBAN

    listed = await beneficiaries_service.list_beneficiaries(supabase, user)
    assert len(listed) == 1

    remove_result = await RemoveBeneficiaryTool(supabase).execute(
        _call("remove_beneficiary", iban=VALID_IBAN), context
    )
    assert remove_result.ok, remove_result.error

    listed_after = await beneficiaries_service.list_beneficiaries(supabase, user)
    assert listed_after == []


async def test_remove_unknown_beneficiary_fails_cleanly(supabase, user_factory):
    user = await user_factory()
    context = await build_context_for_user(user, supabase)

    result = await RemoveBeneficiaryTool(supabase).execute(
        _call("remove_beneficiary", iban=VALID_IBAN), context
    )
    assert not result.ok


async def test_create_scheduled_transfer(supabase, user_factory, account_factory):
    user = await user_factory()
    from_account = await account_factory(user, name="A", currency="USD")
    to_account = await account_factory(user, name="B", currency="USD")
    context = await build_context_for_user(user, supabase)

    result = await CreateScheduledTransferTool(supabase).execute(
        _call(
            "create_scheduled_transfer",
            from_account_id=from_account["id"],
            to_account_id=to_account["id"],
            amount_minor=500,
            currency="USD",
            frequency="monthly",
            start_in_days=3,
        ),
        context,
    )
    assert result.ok, result.error
    assert result.data["frequency"] == "monthly"
    assert result.data["amount_minor"] == 500


async def test_create_scheduled_transfer_refuses_unowned_account(
    supabase, user_factory, account_factory
):
    """SECURITY: the model cannot schedule a transfer touching an account it
    does not own, even one it names by a real id from another user."""
    alice = await user_factory()
    bob = await user_factory()
    alice_account = await account_factory(alice, name="Alice Checking", currency="USD")
    bob_account = await account_factory(bob, name="Bob Checking", currency="USD")

    alice_context = await build_context_for_user(alice, supabase)
    result = await CreateScheduledTransferTool(supabase).execute(
        _call(
            "create_scheduled_transfer",
            from_account_id=alice_account["id"],
            to_account_id=bob_account["id"],
            amount_minor=500,
            currency="USD",
        ),
        alice_context,
    )
    assert not result.ok
    assert "access denied" in (result.error or "").lower()


async def test_propose_card_order_does_not_write_anything(supabase, user_factory, account_factory):
    """propose_card_order stays read_only - it only echoes back the gathered
    fields, it never inserts a card_orders row itself."""
    user = await user_factory()
    account = await account_factory(user)
    context = await build_context_for_user(user, supabase)

    result = await ProposeCardOrderTool(supabase).execute(
        _call(
            "propose_card_order",
            account_id=account["id"],
            full_name="Ana Pop",
            phone="0712345678",
            address="Str. Exemplu 1",
            city="Cluj-Napoca",
            postal_code="400000",
            country="Romania",
        ),
        context,
    )
    assert result.ok, result.error
    assert result.data["full_name"] == "Ana Pop"
    assert result.data["account_id"] == account["id"]

    orders_resp = await supabase.table("card_orders").select("id").eq(
        "account_id", account["id"]
    ).execute()
    assert orders_resp.data == []
