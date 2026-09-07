async def test_get_my_profile(authed_client):
    client, user = authed_client
    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(user.id)
    assert body["email"] == user.email
    assert body["first_name"] == user.first_name
    assert body["last_name"] == user.last_name


async def test_update_my_profile(authed_client):
    client, _user = authed_client
    resp = await client.patch(
        "/api/v1/users/me", json={"first_name": "New", "last_name": "Name", "phone": "0712345678"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["first_name"] == "New"
    assert body["last_name"] == "Name"
    assert body["phone"] == "0712345678"

    resp = await client.get("/api/v1/users/me")
    assert resp.json()["first_name"] == "New"


async def test_users_me_requires_authentication(client):
    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 401
