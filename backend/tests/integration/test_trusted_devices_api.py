"""POST/GET /api/v1/trusted-devices/* - device-trust enrollment and the
password-only login path for an already-enrolled browser.

generate_otp_code and send_teams_message are monkeypatched throughout so
tests never depend on guessing a random 6-digit code or hitting the real
Teams webhook (TEAMS_WEBHOOK_URL is configured in this environment's
.env, so an un-stubbed test would otherwise post a real message).
"""

from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient

DEVICE_COOKIE = "trusted_device_token"


def _stub_teams_and_code(monkeypatch, code: str = "123456") -> None:
    async def _fake_send(text: str) -> bool:
        return True

    monkeypatch.setattr("app.modules.trusted_devices.service.send_teams_message", _fake_send)
    monkeypatch.setattr("app.modules.trusted_devices.service.generate_otp_code", lambda: code)


async def test_enroll_request_requires_authentication(client):
    resp = await client.post("/api/v1/trusted-devices/enroll/request")
    assert resp.status_code == 401


async def test_check_device_not_trusted_without_cookie(client):
    resp = await client.get("/api/v1/trusted-devices/check")
    assert resp.status_code == 200
    assert resp.json() == {"trusted": False, "email": None, "face_enrolled": False}


async def test_device_login_without_cookie_is_rejected(client):
    resp = await client.post("/api/v1/trusted-devices/login", json={"password": "password123"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "device_not_trusted"


async def test_enroll_verify_rejects_wrong_code(authed_client, monkeypatch):
    _stub_teams_and_code(monkeypatch, code="123456")
    client, _user = authed_client

    await client.post("/api/v1/trusted-devices/enroll/request")
    resp = await client.post("/api/v1/trusted-devices/enroll/verify", json={"code": "000000"})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_enrollment_code"
    assert DEVICE_COOKIE not in resp.cookies


async def test_full_enrollment_and_device_login_flow(authed_client, app, monkeypatch):
    _stub_teams_and_code(monkeypatch, code="123456")
    client, user = authed_client

    resp = await client.post("/api/v1/trusted-devices/enroll/request")
    assert resp.status_code == 204

    resp = await client.post("/api/v1/trusted-devices/enroll/verify", json={"code": "123456"})
    assert resp.status_code == 204
    device_token = resp.cookies.get(DEVICE_COOKIE)
    assert device_token

    # A fresh, otherwise-unauthenticated client - only the device cookie is
    # set, no session cookie at all, proving the trust lives in the device
    # cookie, not in the caller already being logged in.
    transport = ASGITransport(app=app)
    async with HTTPXAsyncClient(transport=transport, base_url="http://test") as device_client:
        device_client.cookies.set(DEVICE_COOKIE, device_token)

        check_resp = await device_client.get("/api/v1/trusted-devices/check")
        assert check_resp.status_code == 200
        assert check_resp.json() == {
            "trusted": True,
            "email": user.email,
            "face_enrolled": False,
        }

        login_resp = await device_client.post(
            "/api/v1/trusted-devices/login", json={"password": "password123"}
        )
        assert login_resp.status_code == 200, login_resp.text
        assert login_resp.json()["email"] == user.email
        assert "session_token" in login_resp.cookies


async def test_check_device_reports_face_enrolled(authed_client, app, monkeypatch, supabase):
    _stub_teams_and_code(monkeypatch, code="123456")
    client, user = authed_client

    # Bypasses the real dlib face-detection pipeline (see face_auth/service.py)
    # - this test is only about /trusted-devices/check reporting enrollment
    # status correctly, not about face detection itself.
    await supabase.table("face_credentials").insert(
        {"user_id": str(user.id), "embedding": [0.0] * 128}
    ).execute()

    await client.post("/api/v1/trusted-devices/enroll/request")
    enroll_resp = await client.post(
        "/api/v1/trusted-devices/enroll/verify", json={"code": "123456"}
    )
    device_token = enroll_resp.cookies.get(DEVICE_COOKIE)

    transport = ASGITransport(app=app)
    async with HTTPXAsyncClient(transport=transport, base_url="http://test") as device_client:
        device_client.cookies.set(DEVICE_COOKIE, device_token)
        check_resp = await device_client.get("/api/v1/trusted-devices/check")
        assert check_resp.status_code == 200
        assert check_resp.json() == {
            "trusted": True,
            "email": user.email,
            "face_enrolled": True,
        }


async def test_device_login_rejects_wrong_password(authed_client, app, monkeypatch):
    _stub_teams_and_code(monkeypatch, code="123456")
    client, _user = authed_client

    await client.post("/api/v1/trusted-devices/enroll/request")
    enroll_resp = await client.post(
        "/api/v1/trusted-devices/enroll/verify", json={"code": "123456"}
    )
    device_token = enroll_resp.cookies.get(DEVICE_COOKIE)

    transport = ASGITransport(app=app)
    async with HTTPXAsyncClient(transport=transport, base_url="http://test") as device_client:
        device_client.cookies.set(DEVICE_COOKIE, device_token)
        resp = await device_client.post(
            "/api/v1/trusted-devices/login", json={"password": "wrong password"}
        )
        assert resp.status_code == 401
        assert "session_token" not in resp.cookies
