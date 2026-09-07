"""`detect_recurring_payments` against the real Supabase-backed ledger."""

import calendar
import uuid
from datetime import UTC, datetime

from app.ai.context import build_context_for_user
from app.ai.schemas import ToolCall
from app.ai.tools.insights import DetectRecurringPaymentsTool


def _call(call_id: str = "c1", **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name="detect_recurring_payments", arguments=arguments)


def _months_back(n: int) -> datetime:
    """The 5th of the month `n` months before now - a day that exists in
    every month, so the exact date arithmetic never has to worry about
    month-end overflow the way `days_ago` offsets would."""
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
                "reference": "TEST-RECURRING",
                "idempotency_key": f"test-recurring-{uuid.uuid4()}",
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


async def test_detect_recurring_finds_monthly_payment(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    for n in range(3):
        await _seed_entry(
            supabase, account["id"], 2_999, when=_months_back(n), description="Spotify"
        )

    context = await build_context_for_user(user, supabase)
    result = await DetectRecurringPaymentsTool(supabase).execute(_call(), context)

    assert result.ok, result.error
    payments = result.data["recurring_payments"]
    assert len(payments) == 1
    assert payments[0]["name"] == "Spotify"
    assert payments[0]["occurrences"] == 3
    assert payments[0]["average_amount_minor"] == 2_999


async def test_detect_recurring_ignores_one_off(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    await _seed_entry(
        supabase, account["id"], 15_000, when=_months_back(0), description="One Time Purchase"
    )

    context = await build_context_for_user(user, supabase)
    result = await DetectRecurringPaymentsTool(supabase).execute(_call(), context)

    assert result.ok, result.error
    assert result.data["recurring_payments"] == []
    assert result.data["estimated_monthly_cost_minor"] == 0


async def test_detect_recurring_tolerates_price_variation(
    supabase, user_factory, account_factory
):
    user = await user_factory()
    account = await account_factory(user)
    amounts = [2_999, 3_099, 2_899]
    for n, amount in enumerate(amounts):
        await _seed_entry(
            supabase, account["id"], amount, when=_months_back(n), description="Spotify"
        )

    context = await build_context_for_user(user, supabase)
    result = await DetectRecurringPaymentsTool(supabase).execute(_call(), context)

    assert result.ok, result.error
    payments = result.data["recurring_payments"]
    assert len(payments) == 1
    assert payments[0]["occurrences"] == 3
    assert payments[0]["average_amount_minor"] == round(sum(amounts) / len(amounts))


async def test_detect_recurring_returns_estimated_monthly_cost(
    supabase, user_factory, account_factory
):
    user = await user_factory()
    account = await account_factory(user)
    for n in range(3):
        await _seed_entry(
            supabase, account["id"], 2_999, when=_months_back(n), description="Spotify"
        )
        await _seed_entry(
            supabase, account["id"], 4_999, when=_months_back(n), description="Netflix"
        )
    # A one-off in the mix must not be counted towards the estimate.
    await _seed_entry(
        supabase, account["id"], 50_000, when=_months_back(0), description="One Time Purchase"
    )

    context = await build_context_for_user(user, supabase)
    result = await DetectRecurringPaymentsTool(supabase).execute(_call(), context)

    assert result.ok, result.error
    payments = result.data["recurring_payments"]
    assert len(payments) == 2
    assert result.data["estimated_monthly_cost_minor"] == 2_999 + 4_999
    # Sorted by average_amount_minor descending - Netflix (4999) before Spotify (2999).
    assert [p["name"] for p in payments] == ["Netflix", "Spotify"]
