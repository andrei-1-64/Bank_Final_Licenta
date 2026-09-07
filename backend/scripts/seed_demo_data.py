"""DEV-ONLY, idempotent: seed the two fixed demo users the presenter demo
(docs/DEMO.md) runs against.

    python -m scripts.seed_demo_data

Re-run any number of times: each run wipes and reseeds ONLY the two demo
users' own data, then rebuilds it from scratch. No other user's data is
ever touched. Refuses to run unless SUPABASE_URL looks like the DEV project
(see _guard_dev_project below), or --confirm is passed.

WHERE THIS BYPASSES post_transaction (read before changing amounts/dates):
Every account is opened through the real accounts_service.open_account
(which itself calls the sanctioned ledger_service.grant_opening_balance for
the welcome balance), and every card through the real cards_service.
issue_card. The one deliberate exception is per-purchase transaction
HISTORY (groceries, rent, salary, subscriptions, the anomaly): those are
inserted directly into journal_transactions/ledger_entries, the same
documented, dev-only pattern scripts/seed_fake_purchases.py and
scripts/seed_dev_user.py already rely on. post_transaction cannot be used
for this because (a) its RPC has no parameter to backdate `created_at`, so
every real post lands at "now" and could never produce 30-60 days of
history, and (b) it requires BOTH legs to reference a real accounts row in
this system, and no merchant/employer/external-payee account concept
exists anywhere in the app - payments always require a real IBAN-owning
counterparty. Both constraints are structural, not stylistic; see the two
scripts above for the same reasoning applied previously. Each inserted leg
carries an idempotency_key of the form "seed:{user_id}:{index:04d}" so a
second run - even one where the delete step below somehow didn't run -
would still just skip already-seeded rows instead of doubling them.

No service function this script calls (accounts, cards, face_auth,
conversations, statements) turned out to require an app.ai.context.Context
- so the "build one manually" fallback the task described has no
applicable case here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import settings
from app.core.security import hash_password
from app.db.supabase_client import get_client
from app.modules.accounts import service as accounts_service
from app.modules.cards import service as cards_service
from app.modules.cards.schemas import CardCreate
from app.modules.chat import conversations_service
from app.modules.documents.statement_extractor import parse_layout_result
from app.modules.face_auth import service as face_auth_service
from app.modules.statements import service as statements_service
from app.modules.users.schemas import UserRead

#: The dev Supabase project id (from its https://<id>.supabase.co URL).
#: Confirmed against backend/.env's SUPABASE_URL - this script must never
#: run against the TEST project (xlyhhnmjpdzsovhzsbvf), only this one.
DEV_PROJECT_MARKER = "yksdhyyekltebuwrmtjj"

DEMO_PASSWORD = "demo1234"  # noqa: S105 - dev fixture, not a real credential

FIXTURES_DIR = Path(__file__).parent / "fixtures"
DEMO_FACE_FIXTURE = FIXTURES_DIR / "demo_face.jpg"
DEMO_STATEMENT_FIXTURE = FIXTURES_DIR / "demo_statement.json"

DEMO_USER_A_ID = uuid.UUID("00000000-0000-4000-8000-00000000000a")
DEMO_USER_B_ID = uuid.UUID("00000000-0000-4000-8000-00000000000b")

CURRENCY = "RON"


def _log(tag: str, section: str, message: str) -> None:
    print(f"[seed][{tag}][{section}] {message}")


# ---------------------------------------------------------------------------
# Transaction plans
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TxSpec:
    days_ago: float
    direction: str  # "debit" | "credit"
    amount_minor: int
    description: str
    on_target_card: bool = False


def _user_a_transactions() -> list[TxSpec]:
    """~30 days, ordinary categories, no anomalies, no recurring subscriptions."""
    return [
        TxSpec(28, "credit", 550_000, "SALARIU ACME SRL"),
        TxSpec(27, "debit", 180_000, "Chirie apartament"),
        TxSpec(25, "debit", 8_500, "LIDL ROMANIA SRL"),
        TxSpec(22, "debit", 4_500, "STB S.A."),
        TxSpec(20, "debit", 12_000, "MEGA IMAGE"),
        TxSpec(18, "debit", 3_500, "BOLT.EU/O"),
        TxSpec(15, "debit", 9_800, "KAUFLAND ROMANIA"),
        TxSpec(12, "debit", 6_000, "STB S.A."),
        TxSpec(10, "debit", 15_000, "MEGA IMAGE"),
        TxSpec(8, "debit", 2_500, "BOLT.EU/O"),
        TxSpec(5, "debit", 11_000, "LIDL ROMANIA SRL"),
        TxSpec(3, "debit", 4_200, "STB S.A."),
        TxSpec(1, "debit", 7_300, "KAUFLAND ROMANIA"),
    ]


def _user_b_transactions() -> list[TxSpec]:
    """~60 days. Two Netflix + two Spotify charges ~30 days apart (crosses a
    calendar month, stable amount) on the target card for the Step 15
    cancel_card handoff demo, plus one anomaly (EMAG at ~30-40x the size of
    any other line, day -3) for detect_anomalies."""
    return [
        TxSpec(58, "credit", 600_000, "SALARIU ACME SRL"),
        TxSpec(58, "debit", 3_499, "NETFLIX.COM", on_target_card=True),
        TxSpec(55, "debit", 2_999, "SPOTIFY", on_target_card=True),
        TxSpec(52, "debit", 9_000, "LIDL ROMANIA SRL"),
        TxSpec(49, "debit", 5_200, "STB S.A."),
        TxSpec(45, "debit", 14_000, "CARREFOUR"),
        TxSpec(40, "debit", 3_800, "BOLT.EU/O"),
        TxSpec(35, "debit", 8_700, "MEGA IMAGE"),
        TxSpec(30, "debit", 6_100, "KAUFLAND ROMANIA"),
        TxSpec(28, "credit", 600_000, "SALARIU ACME SRL"),
        TxSpec(28, "debit", 3_499, "NETFLIX.COM", on_target_card=True),
        TxSpec(25, "debit", 2_999, "SPOTIFY", on_target_card=True),
        TxSpec(22, "debit", 9_500, "LIDL ROMANIA SRL"),
        TxSpec(18, "debit", 4_700, "STB S.A."),
        TxSpec(15, "debit", 11_200, "CARREFOUR"),
        TxSpec(12, "debit", 3_200, "BOLT.EU/O"),
        TxSpec(9, "debit", 7_600, "MEGA IMAGE"),
        TxSpec(6, "debit", 5_400, "KAUFLAND ROMANIA"),
        TxSpec(3, "debit", 450_000, "EMAG.RO"),
        TxSpec(1, "debit", 4_100, "STB S.A."),
    ]


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


def _guard_dev_project(supabase_url: str, confirm: bool) -> None:
    if DEV_PROJECT_MARKER in supabase_url:
        return
    if confirm:
        return
    raise SystemExit(
        "Refusing to seed: SUPABASE_URL does not look like the DEV project "
        f"({DEV_PROJECT_MARKER}). This script must never touch the TEST "
        "project or anything else by accident. Re-run with --confirm only "
        "if you are certain this target is safe."
    )


# ---------------------------------------------------------------------------
# Wipe (FK-safe order - see module docstring's cascade notes; users are
# never deleted in this app, so every child table needs an explicit delete
# by user_id/account_id rather than relying on ON DELETE CASCADE firing)
# ---------------------------------------------------------------------------


async def _wipe_user_data(supabase, tag: str, user_id: uuid.UUID) -> None:
    uid = str(user_id)

    accounts_resp = (
        await supabase.table("accounts").select("id").eq("user_id", uid).execute()
    )
    account_ids = [row["id"] for row in (accounts_resp.data or [])]

    # statements -> CASCADEs statement_rows.
    await supabase.table("statements").delete().eq("user_id", uid).execute()
    # conversations -> CASCADEs messages, proposals.
    await supabase.table("conversations").delete().eq("user_id", uid).execute()
    await supabase.table("documents").delete().eq("user_id", uid).execute()

    if account_ids:
        await supabase.table("ledger_entries").delete().in_("account_id", account_ids).execute()
        await supabase.table("transfers").delete().in_("from_account_id", account_ids).execute()
        await supabase.table("transfers").delete().in_("to_account_id", account_ids).execute()
    await supabase.table("scheduled_transfers").delete().eq("user_id", uid).execute()

    # Only this script's own rows - never touches journals that might be
    # shared with an unrelated counterparty.
    await (
        supabase.table("journal_transactions")
        .delete()
        .like("idempotency_key", f"seed:{uid}:%")
        .execute()
    )

    if account_ids:
        await supabase.table("cards").delete().in_("account_id", account_ids).execute()
    await supabase.table("accounts").delete().eq("user_id", uid).execute()

    await supabase.table("face_credentials").delete().eq("user_id", uid).execute()
    await supabase.table("face_confirmations").delete().eq("user_id", uid).execute()
    await supabase.table("sessions").delete().eq("user_id", uid).execute()

    _log(tag, "wipe", f"cleared existing data ({len(account_ids)} prior account(s))")


# ---------------------------------------------------------------------------
# User / accounts / cards / transaction history
# ---------------------------------------------------------------------------


async def _upsert_user(
    supabase, tag: str, user_id: uuid.UUID, *, email: str, first_name: str, last_name: str
) -> UserRead:
    resp = (
        await supabase.table("users")
        .upsert(
            {
                "id": str(user_id),
                "email": email,
                "password_hash": hash_password(DEMO_PASSWORD),
                "first_name": first_name,
                "last_name": last_name,
                "referral_bonus_eligible": True,
                # Explicit rather than relying on the columns' DB defaults:
                # UserRead requires every one of these keys present (even as
                # None), and a demo user should read as a normal, usable
                # account rather than one stuck behind email verification.
                "email_verified": True,
                "national_id": None,
                "gender": None,
                "date_of_birth": None,
                "phone": None,
                "address": None,
            }
        )
        .execute()
    )
    user = UserRead.model_validate(resp.data[0])
    _log(tag, "user", f"upserted {email}")
    return user


async def _open_accounts(
    supabase, tag: str, user: UserRead, specs: list[tuple[str, str, dict]]
) -> list[dict]:
    accounts = []
    for name, product_type, extra in specs:
        account = await accounts_service.open_account(
            supabase, user, name, CURRENCY, product_type=product_type, **extra
        )
        accounts.append(account)
    _log(tag, "accounts", f"created {len(accounts)} account(s)")
    return accounts


async def _issue_card(supabase, tag: str, user: UserRead, account_id: str) -> dict:
    card = await cards_service.issue_card(
        supabase, user, CardCreate(account_id=uuid.UUID(account_id))
    )
    _log(tag, "cards", f"issued card ending {card['last4']} on account {account_id}")
    return card


async def _seed_transactions(
    supabase,
    tag: str,
    user_id: uuid.UUID,
    account_id: str,
    specs: list[TxSpec],
    *,
    target_card_id: str | None = None,
) -> None:
    now = datetime.now(UTC)
    created = 0
    for index, spec in enumerate(specs):
        idempotency_key = f"seed:{user_id}:{index:04d}"

        existing = (
            await supabase.table("journal_transactions")
            .select("id")
            .eq("idempotency_key", idempotency_key)
            .maybe_single()
            .execute()
        )
        if existing is not None and existing.data is not None:
            continue

        ts = now - timedelta(days=spec.days_ago)
        journal = (
            await supabase.table("journal_transactions")
            .insert(
                {
                    "reference": f"SEED-{index:04d}",
                    "idempotency_key": idempotency_key,
                    "description": spec.description,
                    "created_at": ts.isoformat(),
                }
            )
            .execute()
        ).data[0]

        card_id = target_card_id if (spec.on_target_card and target_card_id) else None
        await supabase.table("ledger_entries").insert(
            {
                "journal_id": journal["id"],
                "account_id": account_id,
                "direction": spec.direction,
                "amount_minor": spec.amount_minor,
                "currency": CURRENCY,
                "created_at": ts.isoformat(),
                "card_id": card_id,
            }
        ).execute()
        created += 1

    _log(tag, "transactions", f"posted {created} new transaction(s) ({len(specs)} in plan)")


# ---------------------------------------------------------------------------
# User B extras: face enrollment + pre-seeded conversation/statement
# ---------------------------------------------------------------------------


async def _enroll_face_if_fixture_present(supabase, tag: str, user: UserRead) -> None:
    if not DEMO_FACE_FIXTURE.exists():
        _log(
            tag,
            "face",
            f"fixture not found at {DEMO_FACE_FIXTURE} - skipping face enrollment "
            "(add the file manually and re-run to enable it)",
        )
        return
    image_bytes = DEMO_FACE_FIXTURE.read_bytes()
    await face_auth_service.enroll_face(supabase, user, image_bytes)
    _log(tag, "face", "enrolled face from fixture")


@dataclass
class _FakeCell:
    row_index: int
    column_index: int
    content: str
    kind: str | None = None


@dataclass
class _FakeTable:
    row_count: int
    cells: list[_FakeCell]


@dataclass
class _FakeParagraph:
    content: str
    role: str | None = None


@dataclass
class _FakeResult:
    tables: list[_FakeTable] = field(default_factory=list)
    paragraphs: list[_FakeParagraph] = field(default_factory=list)


def _load_statement_fixture_as_azdi_result() -> _FakeResult:
    """Build the same duck-typed shape parse_layout_result expects (attribute
    access, not dict indexing - see tests/unit/test_statement_extractor.py's
    FakeResult/FakeTable/FakeCell/FakeParagraph) out of the JSON fixture, so
    this script runs the SAME parsing code path production statement uploads
    do, rather than hand-building rows."""
    raw = json.loads(DEMO_STATEMENT_FIXTURE.read_text(encoding="utf-8"))
    paragraphs = [
        _FakeParagraph(content=p["content"], role=p.get("role")) for p in raw.get("paragraphs", [])
    ]
    tables = [
        _FakeTable(
            row_count=t["row_count"],
            cells=[
                _FakeCell(
                    row_index=c["row_index"],
                    column_index=c["column_index"],
                    content=c["content"],
                    kind=c.get("kind"),
                )
                for c in t["cells"]
            ],
        )
        for t in raw.get("tables", [])
    ]
    return _FakeResult(tables=tables, paragraphs=paragraphs)


async def _seed_conversation_with_statement(supabase, tag: str, user: UserRead) -> None:
    conversation = await conversations_service.create_conversation(supabase, user)
    conversation_id = conversation["id"]

    fake_result = _load_statement_fixture_as_azdi_result()
    extracted = parse_layout_result(fake_result)

    await statements_service.create_statement(
        supabase,
        user_id=str(user.id),
        conversation_id=conversation_id,
        document_id=None,
        bank_name=extracted.bank_name,
        period_start=extracted.period_start.isoformat() if extracted.period_start else None,
        period_end=extracted.period_end.isoformat() if extracted.period_end else None,
        currency=extracted.currency,
        opening_balance=extracted.opening_balance,
        closing_balance=extracted.closing_balance,
        rows=[
            {
                "posted_date": row.posted_date.isoformat() if row.posted_date else None,
                "description": row.description,
                "amount": row.amount,
                "currency": extracted.currency,
                "balance_after": row.balance_after,
                "row_index": row.row_index,
            }
            for row in extracted.rows
        ],
    )
    _log(
        tag,
        "conversation",
        f"created conversation {conversation_id} with a "
        f"{len(extracted.rows)}-row statement attached",
    )


# ---------------------------------------------------------------------------
# Per-user seeding
# ---------------------------------------------------------------------------


async def seed_user_a(supabase) -> dict:
    tag = "user-A"
    await _wipe_user_data(supabase, tag, DEMO_USER_A_ID)

    user = await _upsert_user(
        supabase,
        tag,
        DEMO_USER_A_ID,
        email="ana@demo.local",
        first_name="Ana",
        last_name="Popescu",
    )

    accounts = await _open_accounts(
        supabase,
        tag,
        user,
        [
            ("Cont Curent", accounts_service.PRODUCT_CHECKING, {}),
            ("Cont Economii", accounts_service.PRODUCT_SAVINGS, {}),
        ],
    )
    checking_id = accounts[0]["id"]

    await _issue_card(supabase, tag, user, checking_id)
    await _seed_transactions(supabase, tag, user.id, checking_id, _user_a_transactions())

    _log(tag, "face", "not enrolled (demonstrates the require_enrolled distinction)")

    return {"email": "ana@demo.local"}


async def seed_user_b(supabase) -> dict:
    tag = "user-B"
    await _wipe_user_data(supabase, tag, DEMO_USER_B_ID)

    user = await _upsert_user(
        supabase,
        tag,
        DEMO_USER_B_ID,
        email="bogdan@demo.local",
        first_name="Bogdan",
        last_name="Ionescu",
    )

    accounts = await _open_accounts(
        supabase,
        tag,
        user,
        [
            ("Cont Curent", accounts_service.PRODUCT_CHECKING, {}),
            ("Cont Economii", accounts_service.PRODUCT_SAVINGS, {}),
            (
                "Depozit la Termen",
                accounts_service.PRODUCT_TERM_DEPOSIT,
                {"term_months": 12},
            ),
        ],
    )
    checking_id = accounts[0]["id"]

    target_card = await _issue_card(supabase, tag, user, checking_id)
    await _seed_transactions(
        supabase,
        tag,
        user.id,
        checking_id,
        _user_b_transactions(),
        target_card_id=target_card["id"],
    )

    await _enroll_face_if_fixture_present(supabase, tag, user)
    await _seed_conversation_with_statement(supabase, tag, user)

    return {"email": "bogdan@demo.local"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def seed(*, confirm: bool = False) -> None:
    supabase_url = os.environ.get("SUPABASE_URL", settings.SUPABASE_URL)
    _guard_dev_project(supabase_url, confirm)

    supabase = await get_client()

    await seed_user_a(supabase)
    await seed_user_b(supabase)

    print()
    print(
        "Seeded DEMO_USER_A (email: ana@demo.local) and DEMO_USER_B "
        "(email: bogdan@demo.local). Password for both: demo1234."
    )


async def main(*, confirm: bool = False) -> None:
    await seed(confirm=confirm)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Override the dev-project guard (only if you are certain the target is safe).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(confirm=args.confirm))
