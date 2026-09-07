"""DEV-ONLY: seed transaction data tailored to exercise every InsightsAgent
tool (categorize_transactions, compute_spending_stats, detect_anomalies,
detect_recurring_payments, get_transactions_in_range) with meaningful,
non-trivial output.

    python -m scripts.seed_insights_test_data --email someone@example.com

Same technique as seed_fake_purchases.py: single-sided DEBIT ledger entries,
bypassing post_transaction()'s double-entry invariant - dev-only fixture,
never do this from application code. Unlike that script, merchants here are
deliberately chosen to hit every category in
app/ai/tools/insights/categorize_transactions.py::CATEGORY_KEYWORDS, plus:
  - three merchants repeated ~30 days apart at a fixed amount, so
    detect_recurring_payments has an obvious subscription pattern to find;
  - one oversized purchase against an otherwise-tight spending baseline, so
    detect_anomalies' z-score check has something unambiguous to flag.
"""

import argparse
import asyncio
import os
import random
from datetime import datetime, timedelta, timezone

from app.db.supabase_client import get_client

# One entry per CATEGORY_KEYWORDS bucket (see the insights tool), plus a
# couple of one-off "Altele" (uncategorized) purchases for realism.
ONE_OFF_MERCHANTS: list[tuple[str, int, int]] = [
    ("LIDL ROMANIA SRL", 4000, 22000),
    ("CARREFOUR", 3000, 25000),
    ("KAUFLAND ROMANIA", 3500, 28000),
    ("OMV PETROM", 12000, 30000),
    ("STARBUCKS", 1500, 4500),
    ("MCDONALD'S", 2000, 6000),
    ("RESTAURANT LA MAMA", 8000, 18000),
    ("FARMACIA TEI", 2000, 12000),  # uncategorized on purpose - "Altele"
    ("LIBRARIA CARTURESTI", 3000, 9000),  # uncategorized on purpose
]

# (merchant, fixed amount_minor, months_back count) - same amount, ~30 days
# apart, so this reads as a subscription/bill rather than random noise.
RECURRING_MERCHANTS: list[tuple[str, int, int]] = [
    ("SPOTIFY", 2999, 3),
    ("NETFLIX.COM", 3499, 3),
    ("VODAFONE ROMANIA", 5999, 3),
]

# One deliberately oversized purchase - well above every other amount here -
# so detect_anomalies' z-score check has an obvious outlier to find.
ANOMALY_MERCHANT = ("EMAG.RO", 350000)


async def _get_user_id(supabase, email: str) -> str:
    resp = await supabase.table("users").select("id").eq("email", email).maybe_single().execute()
    if resp is None or resp.data is None:
        raise SystemExit(f"No user found with email {email!r}.")
    return resp.data["id"]


async def _get_active_account(supabase, user_id: str) -> dict:
    resp = (
        await supabase.table("accounts")
        .select("*")
        .eq("user_id", user_id)
        .eq("status", "active")
        .order("created_at")
        .execute()
    )
    accounts = resp.data or []
    if not accounts:
        raise SystemExit(f"User {user_id} has no active account to seed.")
    for acc in accounts:
        if acc["currency"].upper() == "RON":
            return acc
    return accounts[0]


async def _insert_purchase(supabase, account_id: str, currency: str, index: int, merchant: str, amount_minor: int, ts: datetime) -> bool:
    idempotency_key = f"insights-test-seed:{account_id}:{index:03d}"
    existing = (
        await supabase.table("journal_transactions")
        .select("id")
        .eq("idempotency_key", idempotency_key)
        .maybe_single()
        .execute()
    )
    if existing is not None and existing.data is not None:
        return False

    journal = (
        await supabase.table("journal_transactions")
        .insert(
            {
                "reference": f"INSIGHTS-TEST-{index:03d}",
                "idempotency_key": idempotency_key,
                "description": merchant,
                "created_at": ts.isoformat(),
            }
        )
        .execute()
    ).data[0]

    await supabase.table("ledger_entries").insert(
        {
            "journal_id": journal["id"],
            "account_id": account_id,
            "direction": "debit",
            "amount_minor": amount_minor,
            "currency": currency,
            "created_at": ts.isoformat(),
        }
    ).execute()
    return True


async def seed(email: str) -> None:
    supabase = await get_client()
    user_id = await _get_user_id(supabase, email)
    account = await _get_active_account(supabase, user_id)
    account_id = account["id"]
    currency = account["currency"]

    now = datetime.now(timezone.utc)
    index = 0
    created = 0

    # One-off purchases, spread across the last 30 days.
    for merchant, lo, hi in ONE_OFF_MERCHANTS:
        ts = now - timedelta(seconds=random.uniform(0, 30 * 86400))
        if await _insert_purchase(supabase, account_id, currency, index, merchant, random.randint(lo, hi), ts):
            created += 1
        index += 1

    # Recurring merchants: same amount, ~30 days apart, going back several months.
    for merchant, amount_minor, months_back in RECURRING_MERCHANTS:
        for month in range(months_back):
            ts = now - timedelta(days=30 * month + random.uniform(0, 2))
            if await _insert_purchase(supabase, account_id, currency, index, merchant, amount_minor, ts):
                created += 1
            index += 1

    # The anomaly: one oversized purchase, recent, against the tight baseline above.
    merchant, amount_minor = ANOMALY_MERCHANT
    ts = now - timedelta(days=2)
    if await _insert_purchase(supabase, account_id, currency, index, merchant, amount_minor, ts):
        created += 1

    print("INSIGHTS TEST DATA SEED (dev-only, see module docstring)")
    print(f"  user      {email}")
    print(f"  account   {account['name']} ({currency})  id={account_id}")
    print(f"  created   {created} transaction(s)")
    print("  categories covered: Abonamente/Streaming, Cumpărături alimentare,")
    print("    Transport/Combustibil, Electronice, Mâncare & Băutură,")
    print("    Telecomunicații, plus 2 uncategorized (\"Altele\")")
    print(f"  recurring: {', '.join(m for m, _, _ in RECURRING_MERCHANTS)} (~monthly)")
    print(f"  anomaly:   {ANOMALY_MERCHANT[0]} at {ANOMALY_MERCHANT[1] / 100:.2f} {currency}")


def main() -> int:
    if os.environ.get("ENV", "development").lower() in {"prod", "production"}:
        print("Refusing to seed: ENV looks like production.")
        return 2

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="Email of the existing user to seed data for.")
    args = parser.parse_args()

    asyncio.run(seed(args.email))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
