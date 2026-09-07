"""`simulate_scenario` against the real Supabase-backed ledger."""

import calendar
import uuid
from datetime import UTC, datetime

from app.ai.context import build_context_for_user
from app.ai.schemas import ToolCall
from app.ai.tools.planning import SimulateScenarioTool


def _call(call_id: str = "c1", **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name="simulate_scenario", arguments=arguments)


def _months_back(n: int) -> datetime:
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
                "reference": "TEST-SCENARIO",
                "idempotency_key": f"test-scenario-{uuid.uuid4()}",
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


async def test_simulate_scenario_applies_adjustments(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    await _seed_baseline(supabase, account["id"])

    context = await build_context_for_user(user, supabase)
    result = await SimulateScenarioTool(supabase).execute(
        _call(
            months_ahead=2,
            adjustments=[
                {"description": "reduce coffee", "monthly_amount_minor": 15_000},
                {"description": "raise", "monthly_amount_minor": 100_000},
            ],
        ),
        context,
    )

    assert result.ok, result.error
    data = result.data
    assert data["baseline_net_monthly_minor"] == 50_000
    assert data["adjusted_net_monthly_minor"] == 50_000 + 15_000 + 100_000
    assert data["adjustments_applied"] == [
        {"description": "reduce coffee", "monthly_amount_minor": 15_000},
        {"description": "raise", "monthly_amount_minor": 100_000},
    ]
    # 3 months of the seeded baseline (+50,000/month net) are all-time ledger
    # history too, so the current balance is 150,000 - both projections start
    # from there and diverge by the adjustment total (115,000) each month.
    current_balance = 150_000
    assert data["baseline_projection"][0]["projected_balance_minor"] == current_balance + 50_000
    assert (
        data["adjusted_projection"][0]["projected_balance_minor"]
        == current_balance + 50_000 + 115_000
    )
    assert data["difference_at_end_minor"] == 115_000 * 2


async def test_simulate_scenario_negative_adjustment(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    await _seed_baseline(supabase, account["id"])

    context = await build_context_for_user(user, supabase)
    result = await SimulateScenarioTool(supabase).execute(
        _call(
            months_ahead=1,
            adjustments=[{"description": "new subscription", "monthly_amount_minor": -20_000}],
        ),
        context,
    )

    assert result.ok, result.error
    data = result.data
    assert data["adjusted_net_monthly_minor"] == 50_000 - 20_000
    assert data["difference_at_end_minor"] == -20_000
    # Balance grows slower (not faster) under the negative adjustment.
    assert (
        data["adjusted_projection"][0]["projected_balance_minor"]
        < data["baseline_projection"][0]["projected_balance_minor"]
    )


async def test_simulate_scenario_empty_adjustments(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    await _seed_baseline(supabase, account["id"])

    context = await build_context_for_user(user, supabase)
    result = await SimulateScenarioTool(supabase).execute(_call(months_ahead=3), context)

    assert result.ok, result.error
    data = result.data
    assert data["adjustments_applied"] == []
    assert data["baseline_net_monthly_minor"] == data["adjusted_net_monthly_minor"]
    assert data["baseline_projection"] == data["adjusted_projection"]
    assert data["difference_at_end_minor"] == 0
