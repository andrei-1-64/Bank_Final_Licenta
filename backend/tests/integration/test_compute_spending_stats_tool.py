"""`compute_spending_stats` against the real Supabase-backed ledger."""

import uuid
from datetime import UTC, datetime, timedelta

from app.ai.context import build_context_for_user
from app.ai.schemas import ToolCall
from app.ai.tools.insights import ComputeSpendingStatsTool


def _call(call_id: str = "c1", **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name="compute_spending_stats", arguments=arguments)


def _iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).date().isoformat()


async def _seed_entry(
    supabase,
    account_id,
    amount_minor: int,
    *,
    days_ago: int = 0,
    direction: str = "debit",
    description: str = "Test entry",
    currency: str = "RON",
) -> None:
    journal = (
        await supabase.table("journal_transactions")
        .insert(
            {
                "reference": "TEST-STATS",
                "idempotency_key": f"test-stats-{uuid.uuid4()}",
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


async def test_compute_stats_correct_totals(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    await _seed_entry(supabase, account["id"], 10_000, direction="credit", description="Salary")
    await _seed_entry(supabase, account["id"], 3_000, direction="debit", description="Rent")
    await _seed_entry(supabase, account["id"], 2_000, direction="debit", description="Food")

    context = await build_context_for_user(user, supabase)
    result = await ComputeSpendingStatsTool(supabase).execute(
        _call(start_date=_iso(5), end_date=_iso(0)), context
    )

    assert result.ok, result.error
    data = result.data
    assert data["total_income_minor"] == 10_000
    assert data["total_spending_minor"] == 5_000
    assert data["net_minor"] == 5_000
    assert data["transaction_count"] == 3
    assert data["avg_transaction_minor"] == 2_500


async def test_compute_stats_identifies_largest_and_smallest(
    supabase, user_factory, account_factory
):
    user = await user_factory()
    account = await account_factory(user)
    await _seed_entry(supabase, account["id"], 100, description="Smallest")
    await _seed_entry(supabase, account["id"], 900, description="Largest")
    await _seed_entry(supabase, account["id"], 500, description="Middle")

    context = await build_context_for_user(user, supabase)
    result = await ComputeSpendingStatsTool(supabase).execute(
        _call(start_date=_iso(5), end_date=_iso(0)), context
    )

    assert result.ok, result.error
    assert result.data["largest_transaction"]["amount_minor"] == 900
    assert result.data["smallest_transaction"]["amount_minor"] == 100


async def test_compute_stats_handles_zero_transactions(supabase, user_factory, account_factory):
    user = await user_factory()
    await account_factory(user)

    context = await build_context_for_user(user, supabase)
    result = await ComputeSpendingStatsTool(supabase).execute(
        _call(start_date=_iso(5), end_date=_iso(0)), context
    )

    assert result.ok, result.error
    data = result.data
    assert data["total_income_minor"] == 0
    assert data["total_spending_minor"] == 0
    assert data["net_minor"] == 0
    assert data["transaction_count"] == 0
    assert data["avg_transaction_minor"] == 0
    assert data["largest_transaction"] is None
    assert data["smallest_transaction"] is None
    assert data["busiest_day"] is None
    assert data["daily_average_spending_minor"] == 0
