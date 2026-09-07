"""POST/GET/DELETE /api/v1/statements - upload, list, detail, soft delete.

AzDI extraction itself is mocked at the module boundary
(app.modules.statements.router.extract_statement) for every test here: no
real bank statement PDF is available, and statement_extractor.py's own
parsing logic is covered separately in tests/unit/test_statement_extractor.py
against a hand-built fake AnalyzeResult. These tests exercise everything
AROUND extraction - upload plumbing, ownership, persistence, and the
"extracted, unverified" framing - the same split test_documents_api.py uses
between real pymupdf extraction and the endpoint around it, except here
extraction is mocked rather than exercised for real, since there is no
equivalent lightweight way to build a fake PDF AzDI would parse.
"""

from __future__ import annotations

import pytest

from app.modules.documents.statement_extractor import ExtractedRow, ExtractedStatement

_FAKE_EXTRACTED = ExtractedStatement(
    bank_name="Banca Test",
    period_start=None,
    period_end=None,
    currency="RON",
    rows=[
        ExtractedRow(
            posted_date=None,
            description="Kaufland",
            amount=-150.0,
            balance_after=None,
            row_index=0,
        ),
        ExtractedRow(
            posted_date=None,
            description="Salariu",
            amount=5000.0,
            balance_after=None,
            row_index=1,
        ),
    ],
)


@pytest.fixture(autouse=True)
def _mock_extraction(monkeypatch):
    """Every test in this file uploads through the real endpoint, but never
    touches the real Azure Document Intelligence service - nor requires
    AZURE_DOCUMENT_INTELLIGENCE_* to actually be configured for this test
    run, since require_document_intelligence() is stubbed too."""

    async def fake_extract_statement(_pdf_bytes, _config):
        return _FAKE_EXTRACTED

    monkeypatch.setattr(
        "app.modules.statements.router.extract_statement", fake_extract_statement
    )

    from app.config import DocumentIntelligenceConfig, Settings

    monkeypatch.setattr(
        Settings,
        "require_document_intelligence",
        lambda self: DocumentIntelligenceConfig(endpoint="https://fake.test", key="fake-key"),
    )


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4\n%fake statement pdf for tests\n"


async def _upload(client, **form) -> dict:
    resp = await client.post(
        "/api/v1/statements/upload",
        files={"file": ("statement.pdf", _pdf_bytes(), "application/pdf")},
        data=form,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_upload_requires_authentication(client):
    resp = await client.post(
        "/api/v1/statements/upload",
        files={"file": ("statement.pdf", _pdf_bytes(), "application/pdf")},
    )
    assert resp.status_code == 401


async def test_upload_persists_extracted_rows_and_creates_a_conversation(authed_client):
    client, _user = authed_client

    body = await _upload(client)

    statement = body["statement"]
    assert statement["bank_name"] == "Banca Test"
    assert statement["row_count"] == 2
    assert body["row_count"] == 2
    assert body["conversation_id"] == statement["conversation_id"]
    assert "neverificate" in body["note"].lower() or "NEVERIFICATE" in body["note"]


async def test_upload_response_never_includes_row_level_data(authed_client):
    """StatementSummary is metadata only - row content rides on the detail
    endpoint, not the upload response, same split DocumentRead uses for
    extracted_text."""
    client, _user = authed_client

    body = await _upload(client)

    assert "rows" not in body["statement"]


async def test_upload_attaches_to_a_conversation_the_caller_owns(
    authed_client, conversation_factory
):
    client, user = authed_client
    conversation = await conversation_factory(user)

    body = await _upload(client, conversation_id=conversation["id"])

    assert body["conversation_id"] == conversation["id"]


async def test_upload_rejects_a_conversation_the_caller_does_not_own(
    authed_client_factory, conversation_factory
):
    alice_client, _alice = await authed_client_factory()
    _bob_client, bob = await authed_client_factory()
    bob_conversation = await conversation_factory(bob)

    resp = await alice_client.post(
        "/api/v1/statements/upload",
        files={"file": ("statement.pdf", _pdf_bytes(), "application/pdf")},
        data={"conversation_id": bob_conversation["id"]},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "conversation_not_found"


async def test_upload_rejects_non_pdf_content_type(authed_client):
    client, _user = authed_client

    resp = await client.post(
        "/api/v1/statements/upload",
        files={"file": ("statement.txt", b"just text", "text/plain")},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_list_statements_isolates_users(authed_client_factory):
    alice_client, _alice = await authed_client_factory()
    bob_client, _bob = await authed_client_factory()
    await _upload(alice_client)
    await _upload(bob_client)

    resp = await alice_client.get("/api/v1/statements")

    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1


async def test_get_statement_detail_includes_rows(authed_client):
    client, _user = authed_client
    uploaded = await _upload(client)

    resp = await client.get(f"/api/v1/statements/{uploaded['statement']['id']}")

    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    assert len(rows) == 2
    assert {r["description"] for r in rows} == {"Kaufland", "Salariu"}


async def test_get_statement_detail_rejects_a_foreign_statement(authed_client_factory):
    alice_client, _alice = await authed_client_factory()
    bob_client, _bob = await authed_client_factory()
    alice_statement = await _upload(alice_client)

    resp = await bob_client.get(f"/api/v1/statements/{alice_statement['statement']['id']}")

    assert resp.status_code == 404


async def test_delete_statement_soft_deletes(authed_client):
    client, _user = authed_client
    uploaded = await _upload(client)
    statement_id = uploaded["statement"]["id"]

    resp = await client.delete(f"/api/v1/statements/{statement_id}")
    assert resp.status_code == 200, resp.text

    get_resp = await client.get(f"/api/v1/statements/{statement_id}")
    assert get_resp.status_code == 404

    list_resp = await client.get("/api/v1/statements")
    assert statement_id not in [s["id"] for s in list_resp.json()]


async def test_delete_statement_rejects_a_foreign_statement(authed_client_factory):
    alice_client, _alice = await authed_client_factory()
    bob_client, _bob = await authed_client_factory()
    alice_statement = await _upload(alice_client)

    resp = await bob_client.delete(f"/api/v1/statements/{alice_statement['statement']['id']}")

    assert resp.status_code == 404
