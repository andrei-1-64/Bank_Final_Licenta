"""`savings_goal` against the real Supabase-backed ledger."""

import calendar
import uuid
from datetime import UTC, datetime, timedelta

from app.ai.context import build_context_for_user
from app.ai.schemas import ToolCall
from app.ai.tools.planning import SavingsGoalTool
from app.ai.tools.planning.savings_goal import (
    ALREADY_ACHIEVED,
    FEASIBLE,
    NOT_FEASIBLE,
)


def _call(call_id: str = "c1", **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name="savings_goal", arguments=arguments)


def _months_back(n: int) -> datetime:
    now = datetime.now(UTC)
    month_index = now.month - 1 - n
    year = now.year + month_index // 12
    month = month_index % 12 + 1
    day = min(5, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day, hour=12, minute=0, second=0, microsecond=0)


def _months_ahead_iso(n: int) -> str:
    now = datetime.now(UTC)
    month_index = now.month - 1 + n
    year = now.year + month_index // 12
    month = month_index % 12 + 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day).date().isoformat()


async def _seed_entry(
    supabase,
    account_id,
    amount_minor: int,
    *,
    when: datetime,
    direction: str = "debit",
    description: str = "Test entry",
    currency: str = "RON",
) -> None:
    journal = (
        await supabase.table("journal_transactions")
        .insert(
            {
                "reference": "TEST-GOAL",
                "idempotency_key": f"test-goal-{uuid.uuid4()}",
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
            "created_at": when.isoformat(),
        }
    ).execute()


async def _seed_baseline(supabase, account_id) -> None:
    """Net +50,000/month: 300,000 income, 250,000 spending, each month."""
    for n in range(3):
        await _seed_entry(supabase, account_id, 300_000, when=_months_back(n), direction="credit")
        await _seed_entry(supabase, account_id, 250_000, when=_months_back(n), direction="debit")


async def test_savings_goal_feasible(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    await _seed_baseline(supabase, account["id"])
    # current balance = 150,000 (see test_simulate_scenario for the math)

    context = await build_context_for_user(user, supabase)
    result = await SavingsGoalTool(supabase).execute(
        _call(goal_amount_minor=250_000, target_date=_months_ahead_iso(2)), context
    )

    assert result.ok, result.error
    data = result.data
    assert data["gap_minor"] == 100_000
    assert data["current_monthly_net_minor"] == 50_000
    assert data["months_remaining"] == 2
    # 100,000 / 2 months = 50,000/month, exactly at the current net rate.
    assert data["required_monthly_savings_minor"] == 50_000
    assert data["feasibility"] == FEASIBLE
    # account_factory defaults accounts to USD; the suggestion must reflect
    # the account's real currency, not assume RON.
    assert data["currency"] in data["suggestion"]


async def test_savings_goal_not_feasible(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    await _seed_baseline(supabase, account["id"])

    context = await build_context_for_user(user, supabase)
    result = await SavingsGoalTool(supabase).execute(
        _call(goal_amount_minor=50_000_000, target_date=_months_ahead_iso(1)), context
    )

    assert result.ok, result.error
    data = result.data
    assert data["feasibility"] == NOT_FEASIBLE
    assert data["suggestion"]


async def test_savings_goal_already_achieved(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    await _seed_baseline(supabase, account["id"])

    context = await build_context_for_user(user, supabase)
    result = await SavingsGoalTool(supabase).execute(
        _call(goal_amount_minor=100_000, target_date=_months_ahead_iso(6)), context
    )

    assert result.ok, result.error
    data = result.data
    assert data["gap_minor"] <= 0
    assert data["feasibility"] == ALREADY_ACHIEVED
    assert data["required_monthly_savings_minor"] == 0


async def test_savings_goal_date_in_past_rejected(supabase, user_factory, account_factory):
    user = await user_factory()
    await account_factory(user)

    context = await build_context_for_user(user, supabase)
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    result = await SavingsGoalTool(supabase).execute(
        _call(goal_amount_minor=100_000, target_date=yesterday), context
    )

    assert result.ok is False
    assert "invalid input" in (result.error or "")


async def test_savings_goal_does_not_leak_other_users_data(supabase, user_factory, account_factory):
    alice = await user_factory()
    bob = await user_factory()
    alice_account = await account_factory(alice, name="Alice Checking")
    bob_account = await account_factory(bob, name="Bob Checking")

    await _seed_entry(
        supabase, alice_account["id"], 10_000, when=_months_back(0), direction="credit"
    )
    await _seed_entry(
        supabase, bob_account["id"], 9_999_999, when=_months_back(0), direction="credit"
    )

    alice_context = await build_context_for_user(alice, supabase)
    result = await SavingsGoalTool(supabase).execute(
        _call(goal_amount_minor=20_000, target_date=_months_ahead_iso(3)), alice_context
    )

    assert result.ok, result.error
    assert result.data["current_balance_minor"] == 10_000
