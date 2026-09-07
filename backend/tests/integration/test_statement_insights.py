"""InsightsAgent tools reading from an active statement instead of the
ledger (Step 13's `load_rows` branch in app/ai/tools/insights/_shared.py),
against the real Supabase-backed statements/statement_rows tables.

Also covers the /chat end-to-end path: a statement upload followed by a
statement-less follow-up message still routes to DocumentAgent, because the
most recently uploaded statement in that conversation stays implicitly
active (see app.ai.context.Context.statement_id's docstring) - the
statement equivalent of test_chat_with_document.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.ai.context import build_context_for_user
from app.ai.providers.mock_provider import MockProvider
from app.ai.schemas import ModelResponse, ToolCall
from app.ai.tools.insights import (
    CategorizeTransactionsTool,
    GetTransactionsInRangeTool,
)
from app.ai.tools.insights.compare_statement_to_ledger import CompareStatementToLedgerTool


def _call(name: str, call_id: str = "c1", **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).date().isoformat()


# ---------------------------------------------------------------------------
# load_rows branches to statement_rows when context.statement_id is set
# ---------------------------------------------------------------------------


async def test_get_transactions_in_range_reads_statement_rows_when_active(
    supabase, user_factory, account_factory, statement_factory, statement_row_factory
):
    user = await user_factory()
    # A real account/ledger entry exists too - proves the branch reads the
    # statement, not the ledger, when a statement is active.
    account = await account_factory(user)
    statement = await statement_factory(user)
    await statement_row_factory(
        statement["id"],
        description="Kaufland",
        amount=-45.5,
        posted_date=_iso(2),
        row_index=0,
    )
    await statement_row_factory(
        statement["id"],
        description="Salariu",
        amount=3000.0,
        posted_date=_iso(1),
        row_index=1,
    )

    context = await build_context_for_user(user, supabase, statement_id=statement["id"])
    result = await GetTransactionsInRangeTool(supabase).execute(
        _call("get_transactions_in_range", start_date=_iso(10), end_date=_iso(0)), context
    )

    assert result.ok, result.error
    transactions = result.data["transactions"]
    assert len(transactions) == 2
    assert {t["description"] for t in transactions} == {"Kaufland", "Salariu"}
    kaufland = next(t for t in transactions if t["description"] == "Kaufland")
    assert kaufland["direction"] == "debit"
    assert kaufland["amount_minor"] == 4550
    salariu = next(t for t in transactions if t["description"] == "Salariu")
    assert salariu["direction"] == "credit"
    assert salariu["amount_minor"] == 300000
    assert account["id"]  # the real account/ledger entries were untouched


async def test_get_transactions_in_range_ignores_statement_rows_outside_the_date_range(
    supabase, user_factory, statement_factory, statement_row_factory
):
    user = await user_factory()
    statement = await statement_factory(user)
    await statement_row_factory(
        statement["id"], description="TooOld", amount=-1.0, posted_date=_iso(40), row_index=0
    )
    await statement_row_factory(
        statement["id"], description="InRange", amount=-2.0, posted_date=_iso(5), row_index=1
    )

    context = await build_context_for_user(user, supabase, statement_id=statement["id"])
    result = await GetTransactionsInRangeTool(supabase).execute(
        _call("get_transactions_in_range", start_date=_iso(20), end_date=_iso(0)), context
    )

    assert result.ok, result.error
    assert [t["description"] for t in result.data["transactions"]] == ["InRange"]


async def test_categorize_transactions_persists_category_onto_statement_rows(
    supabase, user_factory, statement_factory, statement_row_factory
):
    """The one write this branch is allowed to make: extracted_category on
    statement_rows - never the ledger (see categorize_transactions.py)."""
    user = await user_factory()
    statement = await statement_factory(user)
    row = await statement_row_factory(
        statement["id"],
        description="Kaufland",
        amount=-45.5,
        posted_date=_iso(1),
        row_index=0,
    )

    context = await build_context_for_user(user, supabase, statement_id=statement["id"])
    provider = MockProvider([ModelResponse(text="not json")])
    result = await CategorizeTransactionsTool(supabase, provider).execute(
        _call("categorize_transactions", start_date=_iso(10), end_date=_iso(0)), context
    )

    assert result.ok, result.error
    categories = {c["name"] for c in result.data["categories"]}
    assert "Cumpărături alimentare" in categories

    persisted = (
        await supabase.table("statement_rows").select("*").eq("id", row["id"]).execute()
    ).data[0]
    assert persisted["extracted_category"] == "Cumpărături alimentare"


# ---------------------------------------------------------------------------
# compare_statement_to_ledger - aggregate diff, not a row match
# ---------------------------------------------------------------------------


async def _seed_ledger_entry(supabase, account_id, amount_minor, *, direction, days_ago):
    journal = (
        await supabase.table("journal_transactions")
        .insert(
            {
                "reference": "TEST-COMPARE",
                "idempotency_key": f"test-compare-{account_id}-{days_ago}-{direction}",
                "description": "Test ledger entry",
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
            "currency": "RON",
            "created_at": (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(),
        }
    ).execute()


async def test_compare_statement_to_ledger_reports_a_mismatch(
    supabase, user_factory, account_factory, statement_factory, statement_row_factory
):
    user = await user_factory()
    account = await account_factory(user)
    # Statement says 100.00 out today; the ledger only has 60.00 out - a
    # deliberate mismatch to prove the diff surfaces it.
    statement = await statement_factory(user, period_start=_iso(3), period_end=_iso(0))
    await statement_row_factory(
        statement["id"], description="Chirie", amount=-100.0, posted_date=_iso(0), row_index=0
    )
    await _seed_ledger_entry(supabase, account["id"], 6000, direction="debit", days_ago=0)

    context = await build_context_for_user(user, supabase, statement_id=statement["id"])
    result = await CompareStatementToLedgerTool(supabase).execute(
        _call("compare_statement_to_ledger"), context
    )

    assert result.ok, result.error
    assert result.data["statement_totals"]["out_minor"] == 10000
    assert result.data["ledger_totals"]["out_minor"] == 6000
    assert result.data["difference"]["out_minor"] == 4000
    assert len(result.data["daily_mismatches"]) == 1


async def test_compare_statement_to_ledger_fails_cleanly_with_no_active_statement(
    supabase, user_factory
):
    user = await user_factory()
    context = await build_context_for_user(user, supabase)

    result = await CompareStatementToLedgerTool(supabase).execute(
        _call("compare_statement_to_ledger"), context
    )

    assert result.ok is False
    assert result.error


# ---------------------------------------------------------------------------
# /chat end-to-end: implicit active statement, mirroring test_chat_with_document.py
# ---------------------------------------------------------------------------


async def test_chat_after_statement_upload_routes_to_document_agent_implicitly(
    authed_client, scripted_provider, statement_factory, statement_row_factory
):
    """No statement_id sent by the frontend on the follow-up turn - the
    most recently uploaded statement for this conversation stays active on
    its own (see Context.statement_id's docstring)."""
    client, user = authed_client
    scripted_provider(ModelResponse(text="Salut!"))
    conversation = (await client.post("/api/v1/chat", json={"message": "salut"})).json()
    conversation_id = conversation["conversation_id"]

    statement = await statement_factory(user, conversation_id=conversation_id)
    await statement_row_factory(statement["id"])

    scripted_provider(ModelResponse(text="Extrasul are o singură tranzacție."))
    resp = await client.post(
        "/api/v1/chat",
        json={"message": "ce contine extrasul?", "conversation_id": conversation_id},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["routing"]["agent_name"] == "documents"


async def test_chat_rejects_a_statement_id_the_caller_does_not_own(
    authed_client_factory, scripted_provider, statement_factory
):
    alice_client, alice = await authed_client_factory()
    bob_client, _bob = await authed_client_factory()
    alice_statement = await statement_factory(alice)

    scripted_provider(ModelResponse(text="unused"))

    resp = await bob_client.post(
        "/api/v1/chat",
        json={"message": "ce scrie in extras?", "statement_id": alice_statement["id"]},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
