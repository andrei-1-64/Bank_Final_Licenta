"""GET /api/v1/accounts/{id}/statement/pdf - see
app/modules/accounts/statement_service.py (balance math, ownership,
transaction fetch) and statement_pdf.py (rendering).

Ledger rows are seeded directly (journal_transactions + ledger_entries),
not through a transfer/payment endpoint - conftest.py's seed_balance_factory
is close but fixes direction/timestamp, and these tests need control over
both (an entry dated BEFORE the period, to prove it only affects the
opening balance and never appears as a row).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pymupdf


async def _seed_entry(
    supabase,
    *,
    account_id: str,
    amount_minor: int,
    direction: str,
    currency: str = "RON",
    description: str = "Test entry",
    created_at: datetime,
) -> None:
    journal = (
        await supabase.table("journal_transactions")
        .insert(
            {
                "reference": "TEST",
                "idempotency_key": f"test-stmt-{uuid.uuid4()}",
                "description": description,
                "created_at": created_at.isoformat(),
            }
        )
        .execute()
    ).data[0]
    await supabase.table("ledger_entries").insert(
        {
            "journal_id": journal["id"],
            "account_id": account_id,
            "direction": direction,
            "amount_minor": amount_minor,
            "currency": currency,
            "created_at": created_at.isoformat(),
        }
    ).execute()


def _extract_text(pdf_bytes: bytes) -> str:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    return "\n".join(page.get_text() for page in doc)


async def test_statement_reflects_opening_and_closing_balance(
    authed_client, supabase, account_factory
):
    client, user = authed_client
    account = await account_factory(user, name="Cont Test", currency="RON")

    period_start = date(2026, 1, 10)
    period_end = date(2026, 1, 20)

    # Before the period - must count toward the opening balance, never
    # appear as a row.
    await _seed_entry(
        supabase, account_id=account["id"], amount_minor=100_000, direction="credit",
        description="Sold anterior perioadei",
        created_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
    )
    # Inside the period.
    await _seed_entry(
        supabase, account_id=account["id"], amount_minor=5_000, direction="debit",
        description="Cheltuială în perioadă",
        created_at=datetime(2026, 1, 15, 12, tzinfo=UTC),
    )
    # After the period - must not affect anything shown.
    await _seed_entry(
        supabase, account_id=account["id"], amount_minor=999_000, direction="credit",
        description="Tranzacție ulterioară perioadei",
        created_at=datetime(2026, 1, 25, 12, tzinfo=UTC),
    )

    resp = await client.get(
        f"/api/v1/accounts/{account['id']}/statement/pdf",
        params={"period_start": period_start.isoformat(), "period_end": period_end.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]

    text = _extract_text(resp.content)
    assert "1000,00 RON" in text  # opening balance (100_000 minor)
    assert "950,00 RON" in text  # closing balance (100_000 - 5_000)
    assert "Cheltuială în perioadă" in text
    assert "Sold anterior perioadei" not in text
    assert "Tranzacție ulterioară perioadei" not in text


async def test_statement_default_period_is_last_30_days(authed_client, account_factory):
    client, user = authed_client
    account = await account_factory(user, name="Cont Implicit", currency="RON")

    resp = await client.get(f"/api/v1/accounts/{account['id']}/statement/pdf")
    assert resp.status_code == 200, resp.text

    expected_start = (date.today() - timedelta(days=30)).isoformat()
    expected_end = date.today().isoformat()
    assert expected_start in resp.headers["content-disposition"]
    assert expected_end in resp.headers["content-disposition"]


async def test_statement_rejects_start_after_end(authed_client, account_factory):
    client, user = authed_client
    account = await account_factory(user, name="Cont Test", currency="RON")

    resp = await client.get(
        f"/api/v1/accounts/{account['id']}/statement/pdf",
        params={"period_start": "2026-02-01", "period_end": "2026-01-01"},
    )
    assert resp.status_code == 422, resp.text


async def test_statement_for_unowned_account_is_404(
    authed_client, authed_client_factory, account_factory
):
    owner_client, owner = authed_client
    account = await account_factory(owner, name="Cont Privat", currency="RON")

    other_client, _other_user = await authed_client_factory()
    resp = await other_client.get(f"/api/v1/accounts/{account['id']}/statement/pdf")
    assert resp.status_code == 404, resp.text


async def test_statement_with_no_transactions_still_renders(authed_client, account_factory):
    client, user = authed_client
    account = await account_factory(user, name="Cont Gol", currency="RON")

    resp = await client.get(f"/api/v1/accounts/{account['id']}/statement/pdf")
    assert resp.status_code == 200, resp.text
    text = _extract_text(resp.content)
    assert "Nicio tranzacție în această perioadă." in text
    assert "0,00 RON" in text  # opening and closing balance, both zero
