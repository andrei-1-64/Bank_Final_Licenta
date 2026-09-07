"""POST /api/v1/chat with `document_id` set - the end-to-end path from an
uploaded document through Orchestrator's context-first override
(app/ai/orchestrator.py) to DocumentAgent actually answering from the
document's real, pymupdf-extracted content.

`scripted_provider` (see tests/conftest.py) installs one MockProvider shared
by every agent the orchestrator registers, exactly as in production - so
these tests also prove the context override reaches DocumentAgent's tool
loop without ever asking the model to classify the message.
"""

from __future__ import annotations

import pymupdf

from app.ai.schemas import ModelResponse, ToolCall


def _build_pdf_bytes(text: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    pdf_bytes = document.tobytes()
    document.close()
    return pdf_bytes


async def _upload(client, text: str) -> dict:
    resp = await client.post(
        "/api/v1/documents/upload",
        files={"file": ("contract.pdf", _build_pdf_bytes(text), "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_chat_with_document_id_routes_to_document_agent(
    authed_client, scripted_provider
):
    client, _user = authed_client
    uploaded = await _upload(client, "Chiria lunara este 500 EUR.")

    scripted_provider(ModelResponse(text="Nu pot ajuta cu asta fara sa citesc documentul."))

    resp = await client.post(
        "/api/v1/chat",
        json={
            "message": "ce spune documentul?",
            "conversation_id": uploaded["conversation_id"],
            "document_id": uploaded["document"]["id"],
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["routing"]["agent_name"] == "documents"


async def test_chat_with_document_id_answers_from_the_actual_document_content(
    authed_client, scripted_provider
):
    """Not a canned reply: the model is scripted to call read_document first,
    and the final answer is grounded in what pymupdf actually extracted from
    the uploaded PDF."""
    client, _user = authed_client
    uploaded = await _upload(client, "Chiria lunara este 500 EUR.")

    scripted_provider(
        ModelResponse(
            tool_calls=[ToolCall(id="call-1", name="read_document", arguments={})]
        ),
        ModelResponse(text="Conform documentului, chiria lunara este 500 EUR."),
    )

    resp = await client.post(
        "/api/v1/chat",
        json={
            "message": "cat este chiria?",
            "conversation_id": uploaded["conversation_id"],
            "document_id": uploaded["document"]["id"],
        },
    )

    assert resp.status_code == 200, resp.text
    assert "500 EUR" in resp.json()["reply"]


async def test_chat_rejects_a_document_id_the_caller_does_not_own(
    authed_client_factory, scripted_provider
):
    alice_client, _alice = await authed_client_factory()
    bob_client, _bob = await authed_client_factory()
    alice_document = await _upload(alice_client, "Document confidential al lui Alice.")

    scripted_provider(ModelResponse(text="unused"))

    resp = await bob_client.post(
        "/api/v1/chat",
        json={
            "message": "ce scrie in document?",
            "document_id": alice_document["document"]["id"],
        },
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_chat_without_document_id_still_routes_banking_queries_normally(
    authed_client, scripted_provider
):
    """Regression check: a normal banking question, with no document
    attached, is unaffected by Step 12's context-first routing check."""
    client, _user = authed_client
    scripted_provider(ModelResponse(text="Soldul tau este 100 USD."))

    resp = await client.post(
        "/api/v1/chat",
        json={"message": "care este soldul meu?"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["routing"]["agent_name"] == "banking"
