from app.modules.auth.validation import generate_test_national_id


def _register_payload(**overrides) -> dict:
    payload = {
        "email": "jane.doe@example.com",
        "password": "correct horse",
        "first_name": "Jane",
        "last_name": "Doe",
        "national_id": generate_test_national_id(1995, 6, 15, gender="F"),
        "phone": "0712345678",
        "address": "Str. Exemplu nr. 1",
    }
    payload.update(overrides)
    return payload


async def test_register_creates_user_and_starts_session(client):
    resp = await client.post("/api/v1/auth/register", json=_register_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == "jane.doe@example.com"
    assert body["first_name"] == "Jane"
    assert body["gender"] == "F"
    assert body["date_of_birth"] == "1995-06-15"
    assert "session_token" in resp.cookies

    # Registration also logs the user in - the cookie it set should
    # authenticate a normal protected request.
    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 200
    assert resp.json()["email"] == "jane.doe@example.com"


async def test_register_without_referral_code_is_not_bonus_eligible(client):
    resp = await client.post("/api/v1/auth/register", json=_register_payload())
    assert resp.status_code == 201, resp.text
    assert resp.json()["referral_bonus_eligible"] is False


async def test_register_with_correct_referral_code_is_bonus_eligible(client):
    resp = await client.post(
        "/api/v1/auth/register", json=_register_payload(referral_code="BanKTHA")
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["referral_bonus_eligible"] is True


async def test_register_referral_code_is_case_insensitive(client):
    resp = await client.post(
        "/api/v1/auth/register", json=_register_payload(referral_code="banktha")
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["referral_bonus_eligible"] is True


async def test_register_with_wrong_referral_code_is_not_bonus_eligible(client):
    resp = await client.post(
        "/api/v1/auth/register", json=_register_payload(referral_code="WRONGCODE")
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["referral_bonus_eligible"] is False


async def test_register_rejects_invalid_national_id(client):
    # 13 digits (passes the schema's length check) but month=13, which
    # validate_national_id still rejects - the check digit alone is NOT
    # enforced (see validate_national_id's docstring), so this can't just
    # be a bad checksum or it would wrongly pass.
    resp = await client.post(
        "/api/v1/auth/register", json=_register_payload(national_id="1991301010000")
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_register_rejects_short_password(client):
    resp = await client.post("/api/v1/auth/register", json=_register_payload(password="short"))
    assert resp.status_code == 422


async def test_register_rejects_duplicate_email(client):
    first = _register_payload()
    second = _register_payload(national_id=generate_test_national_id(1990, 1, 1, gender="M"))

    resp1 = await client.post("/api/v1/auth/register", json=first)
    assert resp1.status_code == 201

    resp2 = await client.post("/api/v1/auth/register", json=second)
    assert resp2.status_code == 409
    assert resp2.json()["error"]["code"] == "email_already_registered"


async def test_register_rejects_duplicate_national_id(client):
    shared_national_id = generate_test_national_id(1990, 1, 1, gender="M")
    first = _register_payload(national_id=shared_national_id)
    second = _register_payload(email="other@example.com", national_id=shared_national_id)

    resp1 = await client.post("/api/v1/auth/register", json=first)
    assert resp1.status_code == 201

    resp2 = await client.post("/api/v1/auth/register", json=second)
    assert resp2.status_code == 409
    assert resp2.json()["error"]["code"] == "national_id_already_registered"


async def test_login_success_sets_cookie(client):
    await client.post("/api/v1/auth/register", json=_register_payload())
    client.cookies.clear()

    resp = await client.post(
        "/api/v1/auth/login", json={"email": "jane.doe@example.com", "password": "correct horse"}
    )
    assert resp.status_code == 200
    assert "session_token" in resp.cookies

    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 200


async def test_login_wrong_password_and_unknown_email_give_same_error(client):
    await client.post("/api/v1/auth/register", json=_register_payload())
    client.cookies.clear()

    wrong_password = await client.post(
        "/api/v1/auth/login", json={"email": "jane.doe@example.com", "password": "wrong password"}
    )
    unknown_email = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "whatever12"}
    )

    assert wrong_password.status_code == 401
    assert unknown_email.status_code == 401
    assert wrong_password.json()["error"]["message"] == unknown_email.json()["error"]["message"]


async def test_login_rate_limited_after_repeated_failures(client):
    await client.post("/api/v1/auth/register", json=_register_payload())
    client.cookies.clear()

    for _ in range(5):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "jane.doe@example.com", "password": "wrong password"},
        )
        assert resp.status_code == 401

    resp = await client.post(
        "/api/v1/auth/login", json={"email": "jane.doe@example.com", "password": "wrong password"}
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "login_rate_limited"

    # Even the CORRECT password is locked out during the window.
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "jane.doe@example.com", "password": "correct horse"}
    )
    assert resp.status_code == 429


async def test_logout_clears_session(client):
    await client.post("/api/v1/auth/register", json=_register_payload())

    resp = await client.post("/api/v1/auth/logout")
    assert resp.status_code == 204

    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 401
