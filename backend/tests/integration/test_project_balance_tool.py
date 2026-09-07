"""`project_balance` against the real Supabase-backed ledger."""

import calendar
import uuid
from datetime import UTC, datetime, timedelta

from app.ai.context import build_context_for_user
from app.ai.schemas import ToolCall
from app.ai.tools.planning import ProjectBalanceTool
from app.ai.tools.planning.project_balance import NO_HISTORY_NOTE


def _call(call_id: str = "c1", **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name="project_balance", arguments=arguments)


def _months_back(n: int) -> datetime:
    """The 5th of the month `n` months before now - always inside the
    tool's 3-month averaging window, and never a month-end overflow risk."""
    now = datetime.now(UTC)
    month_index = now.month - 1 - n
    year = now.year + month_index // 12
    month = month_index % 12 + 1
    day = min(5, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day, hour=12, minute=0, second=0, microsecond=0)


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
                "reference": "TEST-PROJECT",
                "idempotency_key": f"test-project-{uuid.uuid4()}",
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


async def _seed_three_months_of_history(supabase, account_id) -> None:
    # Outside the 3-month averaging window, but still part of the
    # all-time ledger balance.
    opening_when = datetime.now(UTC) - timedelta(days=200)
    await _seed_entry(supabase, account_id, 1_000_000, when=opening_when, direction="credit")

    for n in range(3):
        await _seed_entry(
            supabase, account_id, 300_000, when=_months_back(n), direction="credit"
        )
        await _seed_entry(
            supabase, account_id, 250_000, when=_months_back(n), direction="debit"
        )


async def test_project_balance_basic_projection(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    await _seed_three_months_of_history(supabase, account["id"])

    context = await build_context_for_user(user, supabase)
    result = await ProjectBalanceTool(supabase).execute(_call(months_ahead=2), context)

    assert result.ok, result.error
    data = result.data
    assert data["current_balance_minor"] == 1_150_000
    assert data["monthly_income_avg_minor"] == 300_000
    assert data["monthly_spending_avg_minor"] == 250_000
    assert data["net_monthly_minor"] == 50_000
    assert data["projection"] == [
        {"month": 1, "projected_balance_minor": 1_200_000},
        {"month": 2, "projected_balance_minor": 1_250_000},
    ]
    assert data["months_until_zero"] is None
    assert "note" not in data


async def test_project_balance_with_savings_override(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    await _seed_three_months_of_history(supabase, account["id"])

    context = await build_context_for_user(user, supabase)
    # Deliberately different from the computed net (50_000), so a bug that
    # ignores the override and falls back to the computed value would be
    # caught rather than accidentally matching.
    override = 80_000
    result = await ProjectBalanceTool(supabase).execute(
        _call(months_ahead=2, monthly_savings_override=override), context
    )

    assert result.ok, result.error
    data = result.data
    # The measured averages are still reported...
    assert data["monthly_income_avg_minor"] == 300_000
    assert data["monthly_spending_avg_minor"] == 250_000
    # ...but the projection uses the override, not the computed net.
    assert data["net_monthly_minor"] == override
    assert data["projection"] == [
        {"month": 1, "projected_balance_minor": 1_150_000 + override},
        {"month": 2, "projected_balance_minor": 1_150_000 + 2 * override},
    ]


async def test_project_balance_no_history(supabase, user_factory, account_factory):
    user = await user_factory()
    await account_factory(user)

    context = await build_context_for_user(user, supabase)
    result = await ProjectBalanceTool(supabase).execute(_call(months_ahead=3), context)

    assert result.ok, result.error
    data = result.data
    assert data["current_balance_minor"] == 0
    assert data["projection"] == [
        {"month": 1, "projected_balance_minor": 0},
        {"month": 2, "projected_balance_minor": 0},
        {"month": 3, "projected_balance_minor": 0},
    ]
    assert data["note"] == NO_HISTORY_NOTE


async def test_project_balance_does_not_leak_other_users_data(
    supabase, user_factory, account_factory
):
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
    result = await ProjectBalanceTool(supabase).execute(_call(months_ahead=1), alice_context)

    assert result.ok, result.error
    assert result.data["current_balance_minor"] == 10_000
    assert result.data["monthly_income_avg_minor"] == round(10_000 / 3)
