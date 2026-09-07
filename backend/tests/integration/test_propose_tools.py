"""POST /api/v1/chat driving the five propose_* tools (Step 11).

Each test scripts the model to call exactly one propose_* tool, then asserts
on what actually landed in the `proposals` table - never that money moved,
an account opened/closed, or a card cancelled, since none of that is allowed
to happen from here (see app/ai/tools/propose_tools.py's module docstring).
The confirm/reject/execute half of the flow is covered in
test_proposals_confirm.py.
"""

import json

from app.ai.schemas import ModelResponse, ToolCall


async def _open_account(client, name="Checking", currency="RON"):
    resp = await client.post("/api/v1/accounts", json={"name": name, "currency": currency})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _tool_result(provider, index=1):
    tool_messages = [m for m in provider.calls[index] if m.role == "tool"]
    assert len(tool_messages) == 1
    return json.loads(tool_messages[0].content or "{}")


async def test_propose_transfer_creates_pending_proposal(
    authed_client, scripted_provider, supabase
):
    client, user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")

    provider = scripted_provider(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="propose_transfer",
                    arguments={
                        "from_account_id": from_account["id"],
                        "to_account_id": to_account["id"],
                        "amount_minor": 10_000,
                        "currency": "RON",
                    },
                )
            ]
        ),
        ModelResponse(text="Am pregătit o propunere de transfer."),
    )

    resp = await client.post("/api/v1/chat", json={"message": "transferă 100 RON în economii"})
    assert resp.status_code == 200, resp.text

    payload = await _tool_result(provider)
    assert payload["ok"] is True
    proposal_id = payload["result"]["proposal_id"]

    body = resp.json()
    assert body["proposal"] is not None
    assert body["proposal"]["id"] == proposal_id
    assert body["proposal"]["status"] == "pending"
    assert body["proposal"]["proposal_type"] == "transfer"

    row = (
        await supabase.table("proposals").select("*").eq("id", proposal_id).maybe_single().execute()
    ).data
    assert row["status"] == "pending"
    assert row["user_id"] == str(user.id)
    assert row["payload"]["from_account_id"] == from_account["id"]
    assert row["payload"]["to_account_id"] == to_account["id"]
    assert row["payload"]["amount_minor"] == 10_000
    assert row["result"] is None
    assert row["confirmed_at"] is None


async def test_propose_transfer_does_not_move_money(authed_client, scripted_provider):
    client, _user = authed_client
    from_account = await _open_account(client, "Cont Curent")
    to_account = await _open_account(client, "Economii")

    scripted_provider(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="propose_transfer",
                    arguments={
                        "from_account_id": from_account["id"],
                        "to_account_id": to_account["id"],
                        "amount_minor": 10_000,
                        "currency": "RON",
                    },
                )
            ]
        ),
        ModelResponse(text="Am pregătit o propunere de transfer."),
    )

    resp = await client.post("/api/v1/chat", json={"message": "transferă 100 RON în economii"})
    assert resp.status_code == 200, resp.text

    from_after = (await client.get(f"/api/v1/accounts/{from_account['id']}")).json()
    to_after = (await client.get(f"/api/v1/accounts/{to_account['id']}")).json()
    assert from_after["balance_minor"] == from_account["balance_minor"]
    assert to_after["balance_minor"] == to_account["balance_minor"]


async def test_propose_transfer_rejects_unowned_account(
    authed_client, authed_client_factory, scripted_provider
):
    client, _user = authed_client
    from_account = await _open_account(client)

    other_client, _other_user = await authed_client_factory()
    other_account = await _open_account(other_client, "Not Mine")

    provider = scripted_provider(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="propose_transfer",
                    arguments={
                        "from_account_id": from_account["id"],
                        "to_account_id": other_account["id"],
                        "amount_minor": 10_000,
                        "currency": "RON",
                    },
                )
            ]
        ),
        ModelResponse(text="Nu pot face asta."),
    )

    resp = await client.post("/api/v1/chat", json={"message": "transferă 100 RON către alt cont"})
    assert resp.status_code == 200, resp.text

    payload = await _tool_result(provider)
    assert payload["ok"] is False
    assert "access denied" in payload["error"]
    assert other_account["id"] not in payload["error"]
    assert resp.json()["proposal"] is None


async def test_propose_payment_creates_pending_proposal(
    authed_client, authed_client_factory, user_factory, scripted_provider, supabase
):
    """Uses a REAL recipient account, not a placeholder IBAN - propose_payment
    re-resolves the actual account holder from the IBAN server-side (see
    ProposePaymentInput's beneficiary_name docstring) and would otherwise
    correctly reject an IBAN with no account behind it."""
    client, user = authed_client
    from_account = await _open_account(client)

    recipient_user = await user_factory(first_name="Ion", last_name="Popescu")
    recipient_client, _ = await authed_client_factory(recipient_user)
    recipient_account = await _open_account(recipient_client, "Cont Ion")

    provider = scripted_provider(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="propose_payment",
                    arguments={
                        "from_account_id": from_account["id"],
                        "to_iban": recipient_account["iban"],
                        "beneficiary_name": "Ion Popescu",
                        "amount_minor": 5_000,
                    },
                )
            ]
        ),
        ModelResponse(text="Am pregătit o propunere de plată."),
    )

    resp = await client.post("/api/v1/chat", json={"message": "plătește 50 RON lui Ion Popescu"})
    assert resp.status_code == 200, resp.text

    payload = await _tool_result(provider)
    assert payload["ok"] is True
    proposal_id = payload["result"]["proposal_id"]

    row = (
        await supabase.table("proposals").select("*").eq("id", proposal_id).maybe_single().execute()
    ).data
    assert row["status"] == "pending"
    assert row["user_id"] == str(user.id)
    assert row["proposal_type"] == "payment"
    assert row["payload"]["to_iban"] == recipient_account["iban"]
    # The re-resolved real holder name, not whatever the model claimed.
    assert row["payload"]["beneficiary_name"] == "Ion Popescu"


async def test_propose_open_account_creates_pending_proposal(
    authed_client, scripted_provider, supabase
):
    client, user = authed_client

    provider = scripted_provider(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="propose_open_account",
                    arguments={"name": "Depozit", "currency": "RON", "product_type": "savings"},
                )
            ]
        ),
        ModelResponse(text="Am pregătit o propunere de deschidere de cont."),
    )

    resp = await client.post("/api/v1/chat", json={"message": "vreau un cont de economii"})
    assert resp.status_code == 200, resp.text

    payload = await _tool_result(provider)
    assert payload["ok"] is True
    proposal_id = payload["result"]["proposal_id"]

    row = (
        await supabase.table("proposals").select("*").eq("id", proposal_id).maybe_single().execute()
    ).data
    assert row["status"] == "pending"
    assert row["user_id"] == str(user.id)
    assert row["proposal_type"] == "open_account"
    assert row["payload"]["product_type"] == "savings"

    accounts = (await client.get("/api/v1/accounts")).json()
    assert accounts == []


async def test_propose_close_account_creates_pending_proposal(
    authed_client, scripted_provider, supabase
):
    client, user = authed_client
    account = await _open_account(client)

    provider = scripted_provider(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="propose_close_account",
                    arguments={"account_id": account["id"]},
                )
            ]
        ),
        ModelResponse(text="Am pregătit o propunere de închidere de cont."),
    )

    resp = await client.post("/api/v1/chat", json={"message": "închide-mi contul"})
    assert resp.status_code == 200, resp.text

    payload = await _tool_result(provider)
    assert payload["ok"] is True
    proposal_id = payload["result"]["proposal_id"]

    row = (
        await supabase.table("proposals").select("*").eq("id", proposal_id).maybe_single().execute()
    ).data
    assert row["status"] == "pending"
    assert row["proposal_type"] == "close_account"
    assert row["payload"]["account_id"] == account["id"]

    still_open = (await client.get(f"/api/v1/accounts/{account['id']}")).json()
    assert still_open["status"] == "active"


async def test_propose_cancel_card_creates_pending_proposal(
    authed_client, scripted_provider, supabase
):
    client, user = authed_client
    account = await _open_account(client)
    card = (await client.post("/api/v1/cards", json={"account_id": account["id"]})).json()

    provider = scripted_provider(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="propose_cancel_card",
                    arguments={"card_id": card["id"]},
                )
            ]
        ),
        ModelResponse(text="Am pregătit o propunere de anulare a cardului."),
    )

    resp = await client.post("/api/v1/chat", json={"message": "anulează cardul meu"})
    assert resp.status_code == 200, resp.text

    payload = await _tool_result(provider)
    assert payload["ok"] is True
    proposal_id = payload["result"]["proposal_id"]

    row = (
        await supabase.table("proposals").select("*").eq("id", proposal_id).maybe_single().execute()
    ).data
    assert row["status"] == "pending"
    assert row["proposal_type"] == "cancel_card"
    assert row["payload"]["card_id"] == card["id"]

    still_active = (await client.get("/api/v1/cards")).json()
    assert still_active[0]["status"] == "active"


async def test_propose_cancel_card_rejects_unowned_card(
    authed_client, authed_client_factory, scripted_provider
):
    client, _user = authed_client

    other_client, _other_user = await authed_client_factory()
    other_account = await _open_account(other_client, "Not Mine")
    other_card = (
        await other_client.post("/api/v1/cards", json={"account_id": other_account["id"]})
    ).json()

    provider = scripted_provider(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="propose_cancel_card",
                    arguments={"card_id": other_card["id"]},
                )
            ]
        ),
        ModelResponse(text="Nu pot face asta."),
    )

    resp = await client.post("/api/v1/chat", json={"message": "anulează cardul altcuiva"})
    assert resp.status_code == 200, resp.text

    payload = await _tool_result(provider)
    assert payload["ok"] is False
    assert "access denied" in payload["error"]
    assert other_card["id"] not in payload["error"]
    assert resp.json()["proposal"] is None
