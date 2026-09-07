"""DEV-ONLY: create one funded user to develop the AI layer against.

    python -m scripts.seed_dev_user

THIS IS NOT AUTHENTICATION. Real login/logout/register (app/modules/auth/
router.py, service.py, schemas.py) is the real thing - see
docs/AUTH_HANDOFF.md. This script deliberately creates NO session row and
touches nothing in modules/auth beyond the users table, so it cannot
collide with that work. It exists because app/ai/ currently has no way to
get a real user id or account id to act as, and because there is no
deposit/funding concept in the app yet (see below).

What it does, idempotently:
  1. creates (or finds) the user dev@test.local, password hashed with the same
     core/security.py primitive real login will use;
  2. creates (or finds) one ACTIVE account for them;
  3. gives that account an opening balance, if it has none.

On funding: flow.md's spec only covers money MOVING between accounts - there
is no deposit or external-funding concept anywhere, and post_transaction would
reject debiting a zero-balance "funding" account (insufficient funds). So the
opening balance is a single-sided CREDIT ledger entry, exactly the approach
tests/conftest.py::seed_balance_factory takes. That is the one place in the
codebase allowed to bypass the double-entry invariant, and it must stay
dev/test-only - never do this from application code.
"""

import asyncio
import os
import uuid

from app.core.security import hash_password
from app.db.supabase_client import get_client
from app.modules.ledger import service as ledger_service

# Not a .local / .test address: this script inserts the row directly, but
# /auth/login validates the address with Pydantic's EmailStr, which rejects
# reserved TLDs - so a seeded user on one of those could never actually log in.
DEV_EMAIL = "dev@example.com"
DEV_PASSWORD = "devpassword123"  # noqa: S105 - dev fixture, not a real credential
DEV_FIRST_NAME = "Dev"
DEV_LAST_NAME = "User"

DEV_ACCOUNT_NAME = "Dev Checking"
DEV_ACCOUNT_CURRENCY = "USD"
DEV_OPENING_BALANCE_MINOR = 250_000  # 2,500.00 USD


async def _get_or_create_user(supabase) -> tuple[dict, bool]:
    resp = await supabase.table("users").select("*").eq("email", DEV_EMAIL).maybe_single().execute()
    existing = resp.data if resp is not None else None
    if existing is not None:
        return existing, False

    resp = (
        await supabase.table("users")
        .insert(
            {
                "email": DEV_EMAIL,
                "password_hash": hash_password(DEV_PASSWORD),
                "first_name": DEV_FIRST_NAME,
                "last_name": DEV_LAST_NAME,
            }
        )
        .execute()
    )
    return resp.data[0], True


async def _get_or_create_account(supabase, user: dict) -> tuple[dict, bool]:
    resp = (
        await supabase.table("accounts")
        .select("*")
        .eq("user_id", user["id"])
        .eq("name", DEV_ACCOUNT_NAME)
        .maybe_single()
        .execute()
    )
    existing = resp.data if resp is not None else None
    if existing is not None:
        return existing, False

    # Deliberately inserts directly rather than calling
    # accounts.service.open_account: that writes an audit event, and a dev
    # fixture should not litter the audit log with fake activity.
    resp = (
        await supabase.table("accounts")
        .insert(
            {
                "user_id": user["id"],
                "name": DEV_ACCOUNT_NAME,
                "currency": DEV_ACCOUNT_CURRENCY,
                "status": "active",
            }
        )
        .execute()
    )
    return resp.data[0], True


async def _fund_if_empty(supabase, account: dict) -> tuple[int, bool]:
    """Give the account an opening balance if it has none.

    Balance is always derived (SUM of ledger entries), so "already funded" is
    a balance read, not a flag. The idempotency key is derived from the
    account id, so even a concurrent second run hits the UNIQUE constraint on
    journal_transactions.idempotency_key rather than double-funding.
    """
    balance = await ledger_service.get_balance(supabase, uuid.UUID(account["id"]))
    if balance != 0:
        return balance, False

    journal = (
        await supabase.table("journal_transactions")
        .insert(
            {
                "reference": "DEV-SEED",
                "idempotency_key": f"dev-seed-opening-balance:{account['id']}",
                "description": "DEV-ONLY seeded opening balance",
            }
        )
        .execute()
    ).data[0]

    await supabase.table("ledger_entries").insert(
        {
            "journal_id": journal["id"],
            "account_id": account["id"],
            "direction": "credit",
            "amount_minor": DEV_OPENING_BALANCE_MINOR,
            "currency": DEV_ACCOUNT_CURRENCY,
        }
    ).execute()
    return DEV_OPENING_BALANCE_MINOR, True


def _describe(created: bool) -> str:
    return "created" if created else "already existed"


async def seed() -> tuple[str, list[str]]:
    supabase = await get_client()

    user, user_created = await _get_or_create_user(supabase)
    account, account_created = await _get_or_create_account(supabase, user)
    balance, funded = await _fund_if_empty(supabase, account)

    user_id = user["id"]
    account_id = account["id"]

    print("DEV SEED (not auth - see module docstring)")
    print(f"  user     {_describe(user_created):<16} {DEV_EMAIL}  password={DEV_PASSWORD}")
    print(
        f"  account  {_describe(account_created):<16} "
        f"{DEV_ACCOUNT_NAME} ({DEV_ACCOUNT_CURRENCY})"
    )
    print(
        f"  balance  {'funded' if funded else 'left as-is':<16} "
        f"{balance} minor units ({balance / 100:.2f} {DEV_ACCOUNT_CURRENCY})"
    )
    print()
    print(f"  DEV_USER_ID={user_id}")
    print(f"  DEV_ACCOUNT_IDS={account_id}")
    print()
    print("Point the AI CLI at this identity with:")
    print(f'  export DEV_USER_ID="{user_id}"')
    print(f'  export DEV_ACCOUNT_IDS="{account_id}"')
    print("  python -m app.ai.chat")

    return user_id, [account_id]


def main() -> int:
    if os.environ.get("ENV", "development").lower() in {"prod", "production"}:
        print("Refusing to seed: ENV looks like production.")
        return 2
    asyncio.run(seed())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
