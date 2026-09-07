"""GET/POST /api/v1/beneficiaries, DELETE /api/v1/beneficiaries/{id} - the
standalone contact management endpoints, as opposed to the automatic
upsert-on-payment path (see payments/service.py)."""

VALID_IBAN = "RO49AAAA1B31007593840000"


async def test_beneficiaries_require_authentication(client):
    resp = await client.get("/api/v1/beneficiaries")
    assert resp.status_code == 401


async def test_add_and_list_beneficiary(authed_client):
    client, _user = authed_client

    resp = await client.post(
        "/api/v1/beneficiaries", json={"iban": VALID_IBAN, "display_name": "Ana Pop"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["iban"] == VALID_IBAN
    assert body["display_name"] == "Ana Pop"

    listed = (await client.get("/api/v1/beneficiaries")).json()
    assert len(listed) == 1
    assert listed[0]["iban"] == VALID_IBAN
    assert listed[0]["is_subscription"] is False


async def test_add_beneficiary_as_subscription(authed_client):
    client, _user = authed_client
    resp = await client.post(
        "/api/v1/beneficiaries",
        json={"iban": VALID_IBAN, "display_name": "Netflix", "is_subscription": True},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_subscription"] is True

    listed = (await client.get("/api/v1/beneficiaries")).json()
    assert listed[0]["is_subscription"] is True


async def test_add_beneficiary_with_website(authed_client):
    client, _user = authed_client
    resp = await client.post(
        "/api/v1/beneficiaries",
        json={"iban": VALID_IBAN, "display_name": "Netflix", "website": "https://netflix.com"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["website"] == "https://netflix.com"

    listed = (await client.get("/api/v1/beneficiaries")).json()
    assert listed[0]["website"] == "https://netflix.com"


async def test_readding_beneficiary_without_website_does_not_wipe_it(authed_client):
    """A plain payment's beneficiary-save never sends a website - re-saving
    an existing contact that way must not silently erase one set earlier
    via the standalone add-beneficiary flow (see upsert_beneficiary)."""
    client, _user = authed_client
    await client.post(
        "/api/v1/beneficiaries",
        json={"iban": VALID_IBAN, "display_name": "Netflix", "website": "https://netflix.com"},
    )
    resp = await client.post(
        "/api/v1/beneficiaries", json={"iban": VALID_IBAN, "display_name": "Netflix Updated"}
    )
    assert resp.status_code == 201, resp.text

    listed = (await client.get("/api/v1/beneficiaries")).json()
    assert listed[0]["display_name"] == "Netflix Updated"
    assert listed[0]["website"] == "https://netflix.com"


async def test_add_beneficiary_rejects_invalid_iban(authed_client):
    client, _user = authed_client
    resp = await client.post(
        "/api/v1/beneficiaries", json={"iban": "not an iban at all", "display_name": "X"}
    )
    assert resp.status_code == 422


async def test_add_beneficiary_twice_updates_display_name(authed_client):
    """Same shape as the payment side-effect path (upsert_beneficiary) -
    re-adding an existing IBAN updates the name rather than erroring or
    duplicating the row."""
    client, _user = authed_client

    await client.post("/api/v1/beneficiaries", json={"iban": VALID_IBAN, "display_name": "Old"})
    resp = await client.post(
        "/api/v1/beneficiaries", json={"iban": VALID_IBAN, "display_name": "New Name"}
    )
    assert resp.status_code == 201

    listed = (await client.get("/api/v1/beneficiaries")).json()
    assert len(listed) == 1
    assert listed[0]["display_name"] == "New Name"


async def test_remove_beneficiary(authed_client):
    client, _user = authed_client
    added = (
        await client.post(
            "/api/v1/beneficiaries", json={"iban": VALID_IBAN, "display_name": "Ana Pop"}
        )
    ).json()

    resp = await client.delete(f"/api/v1/beneficiaries/{added['id']}")
    assert resp.status_code == 204

    listed = (await client.get("/api/v1/beneficiaries")).json()
    assert listed == []


async def test_remove_unknown_beneficiary_is_404(authed_client):
    client, _user = authed_client
    resp = await client.delete("/api/v1/beneficiaries/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_remove_unowned_beneficiary_is_404(authed_client, authed_client_factory):
    owner_client, _owner = authed_client
    added = (
        await owner_client.post(
            "/api/v1/beneficiaries", json={"iban": VALID_IBAN, "display_name": "Ana Pop"}
        )
    ).json()

    other_client, _other = await authed_client_factory()
    resp = await other_client.delete(f"/api/v1/beneficiaries/{added['id']}")
    assert resp.status_code == 404
