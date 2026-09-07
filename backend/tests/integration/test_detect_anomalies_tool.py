"""`detect_anomalies` against the real Supabase-backed ledger."""

import uuid
from datetime import UTC, datetime, timedelta

from app.ai.context import build_context_for_user
from app.ai.schemas import ToolCall
from app.ai.tools.insights import DetectAnomaliesTool
from app.ai.tools.insights.detect_anomalies import TOO_FEW_TRANSACTIONS_NOTE


def _call(call_id: str = "c1", **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name="detect_anomalies", arguments=arguments)


async def _seed_entry(
    supabase,
    account_id,
    amount_minor: int,
    *,
    days_ago: int = 1,
    direction: str = "debit",
    description: str = "Test entry",
    currency: str = "RON",
) -> None:
    journal = (
        await supabase.table("journal_transactions")
        .insert(
            {
                "reference": "TEST-ANOMALY",
                "idempotency_key": f"test-anomaly-{uuid.uuid4()}",
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


async def test_detect_anomaly_by_amount(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    # Same merchant every time, so only the amount signal can fire.
    for _ in range(10):
        await _seed_entry(supabase, account["id"], 1_000, description="Coffee Shop")
    await _seed_entry(supabase, account["id"], 50_000, description="Coffee Shop")

    context = await build_context_for_user(user, supabase)
    result = await DetectAnomaliesTool(supabase).execute(_call(), context)

    assert result.ok, result.error
    anomalies = result.data["anomalies"]
    assert len(anomalies) == 1
    assert anomalies[0]["type"] == "amount"
    assert anomalies[0]["amount_minor"] == 50_000
    assert anomalies[0]["z_score"] > 2.0


async def test_detect_anomaly_new_merchant(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    # Same amount every time, so only the new-merchant signal can fire.
    for _ in range(10):
        await _seed_entry(supabase, account["id"], 2_000, description="Lidl")
    await _seed_entry(supabase, account["id"], 2_000, description="NeverSeenBefore")

    context = await build_context_for_user(user, supabase)
    result = await DetectAnomaliesTool(supabase).execute(_call(), context)

    assert result.ok, result.error
    anomalies = result.data["anomalies"]
    assert len(anomalies) == 1
    assert anomalies[0]["type"] == "new_merchant"
    assert anomalies[0]["reference"] == "TEST-ANOMALY"
    assert anomalies[0]["z_score"] is None


async def test_detect_anomaly_too_few_transactions(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    for _ in range(3):
        await _seed_entry(supabase, account["id"], 1_000, description="Coffee Shop")

    context = await build_context_for_user(user, supabase)
    result = await DetectAnomaliesTool(supabase).execute(_call(), context)

    assert result.ok, result.error
    assert result.data["anomalies"] == []
    assert result.data["anomaly_count"] == 0
    assert result.data["note"] == TOO_FEW_TRANSACTIONS_NOTE


async def test_detect_anomaly_threshold_parameter(supabase, user_factory, account_factory):
    user = await user_factory()
    account = await account_factory(user)
    for _ in range(20):
        await _seed_entry(supabase, account["id"], 1_000, description="Coffee Shop")
    await _seed_entry(supabase, account["id"], 2_000, description="Coffee Shop")
    await _seed_entry(supabase, account["id"], 4_000, description="Coffee Shop")

    context = await build_context_for_user(user, supabase)

    lenient = await DetectAnomaliesTool(supabase).execute(_call(threshold=1.0), context)
    strict = await DetectAnomaliesTool(supabase).execute(_call(threshold=3.0), context)

    assert lenient.ok, lenient.error
    assert strict.ok, strict.error
    assert len(strict.data["anomalies"]) < len(lenient.data["anomalies"])


async def test_detect_anomaly_does_not_leak_other_users_data(
    supabase, user_factory, account_factory
):
    alice = await user_factory()
    bob = await user_factory()
    alice_account = await account_factory(alice, name="Alice Checking")
    bob_account = await account_factory(bob, name="Bob Checking")
    for _ in range(5):
        await _seed_entry(supabase, alice_account["id"], 1_000, description="AliceCoffee")
    await _seed_entry(supabase, bob_account["id"], 999_999, description="BobSecretSplurge")

    alice_context = await build_context_for_user(alice, supabase)
    result = await DetectAnomaliesTool(supabase).execute(_call(), alice_context)

    assert result.ok, result.error
    assert "BobSecretSplurge" not in str(result.data)
    assert "999999" not in str(result.data)
    assert str(bob_account["id"]) not in str(result.data)
