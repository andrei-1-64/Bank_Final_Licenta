"""DEV-ONLY: populate one existing user's account with fake purchase history.

    python -m scripts.seed_fake_purchases --email someone@example.com

Purely cosmetic/demo data (Lidl, Mega Image, eMag, STB, ...) for exercising
the dashboard/transactions UI. Like scripts/seed_dev_user.py's opening
balance, each purchase is a single-sided DEBIT ledger entry with no matching
credit leg anywhere: post_transaction() enforces balanced double-entry
journals and would reject an unbalanced "money leaves to a merchant that
isn't a real account in this system" movement, so this bypasses it and
writes journal_transactions/ledger_entries directly. That is the same
deliberate, documented exception seed_dev_user.py already relies on - never
do this from application code, dev-only fixture script.

Idempotent per (account, index): re-running with the same account regenerates
nothing that already exists (idempotency_key is deterministic per slot), so a
second run is a no-op rather than doubling up the fake history.
"""

import argparse
import asyncio
import os
import random
import uuid
from datetime import datetime, timedelta, timezone

from app.db.supabase_client import get_client
from app.modules.ledger import service as ledger_service

# (merchant label as it would show on a statement, category amount range in
# minor units RON). Ranges are deliberately overlapping-but-distinct so the
# generated history doesn't look like it came from a uniform RNG.
MERCHANTS: list[tuple[str, int, int]] = [
    ("LIDL ROMANIA SRL", 2000, 25000),
    ("MEGA IMAGE", 1500, 18000),
    ("KAUFLAND ROMANIA", 3000, 28000),
    ("CARREFOUR", 2500, 22000),
    ("PROFI ROM FOOD", 1200, 12000),
    ("EMAG.RO", 5000, 60000),
    ("STB S.A.", 700, 7000),
    ("BOLT.EU/O", 1000, 4500),
    ("GLOVO ROMANIA", 3000, 12000),
    ("NETFLIX.COM", 3499, 3499),
    ("SPOTIFY", 2999, 2999),
    ("OMV PETROM", 10000, 35000),
    ("CATENA FARMACIE", 1500, 15000),
]

NUM_TRANSACTIONS = 30
LOOKBACK_DAYS = 30


async def _get_user_by_email(supabase, email: str) -> dict:
    resp = await supabase.table("users").select("*").eq("email", email).maybe_single().execute()
    if resp is None or resp.data is None:
        raise SystemExit(f"No user found with email {email!r}.")
    return resp.data


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
    # Prefer RON since the fake merchants are all Romanian; otherwise just
    # take the first active account.
    for acc in accounts:
        if acc["currency"].upper() == "RON":
            return acc
    return accounts[0]


def _random_timestamp(now: datetime) -> datetime:
    seconds_back = random.uniform(0, LOOKBACK_DAYS * 86400)
    return now - timedelta(seconds=seconds_back)


async def seed_fake_purchases(email: str) -> None:
    supabase = await get_client()

    user = await _get_user_by_email(supabase, email)
    account = await _get_active_account(supabase, user["id"])
    account_id = account["id"]
    currency = account["currency"]

    balance = await ledger_service.get_balance(supabase, uuid.UUID(account_id))

    # Card purchases need a card on the entry (ledger_entries.card_id, see
    # migration 0017) - otherwise the admin panel's "transactions by card"
    # filter has nothing to show. Cards are picked at random per purchase so
    # a user with several gets a realistic mix; a user with none still gets
    # history, just with card_id NULL, exactly like a transfer would.
    cards_resp = (
        await supabase.table("cards")
        .select("id")
        .eq("account_id", account_id)
        .neq("status", "cancelled")
        .execute()
    )
    card_ids = [row["id"] for row in (cards_resp.data or [])]

    now = datetime.now(timezone.utc)
    created, skipped = 0, 0

    for i in range(NUM_TRANSACTIONS):
        merchant, lo, hi = random.choice(MERCHANTS)
        amount_minor = random.randint(lo, hi)
        ts = _random_timestamp(now)
        idempotency_key = f"fake-purchase-seed:{account_id}:{i:03d}"

        existing = (
            await supabase.table("journal_transactions")
            .select("id")
            .eq("idempotency_key", idempotency_key)
            .maybe_single()
            .execute()
        )
        if existing is not None and existing.data is not None:
            skipped += 1
            continue

        journal = (
            await supabase.table("journal_transactions")
            .insert(
                {
                    "reference": f"FAKE-{i:03d}",
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
                "card_id": random.choice(card_ids) if card_ids else None,
            }
        ).execute()
        created += 1

    new_balance = await ledger_service.get_balance(supabase, uuid.UUID(account_id))

    print("FAKE PURCHASE SEED (dev-only, see module docstring)")
    print(f"  user      {email}")
    print(f"  account   {account['name']} ({currency})  id={account_id}")
    print(f"  created   {created} purchase(s), skipped {skipped} already-seeded slot(s)")
    print(f"  balance   {balance / 100:.2f} -> {new_balance / 100:.2f} {currency}")


def main() -> int:
    if os.environ.get("ENV", "development").lower() in {"prod", "production"}:
        print("Refusing to seed: ENV looks like production.")
        return 2

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="Email of the existing user to seed purchases for.")
    args = parser.parse_args()

    asyncio.run(seed_fake_purchases(args.email))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
