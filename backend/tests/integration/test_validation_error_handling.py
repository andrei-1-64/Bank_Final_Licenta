"""The 422 handler in core/middleware.py must survive any validation error.

Regression guard: Pydantic v2 puts the original exception object in each
error's `ctx` when a `@field_validator` raises, and JSONResponse cannot
serialise that. Without `jsonable_encoder` the handler itself blew up and the
global 500 handler answered instead - turning every such 422 into a 500.

The offending validator lives on a test-only model and a route registered on
the app fixture, so no production schema has to carry it.
"""

import pytest
from pydantic import BaseModel, field_validator


class _PayloadWithRaisingValidator(BaseModel):
    """A field_validator that raises ValueError - the shape that broke it."""

    name: str

    @field_validator("name")
    @classmethod
    def _reject_everything(cls, value: str) -> str:
        raise ValueError("boom")


@pytest.fixture
def app_with_raising_route(app):
    """Register a throwaway route whose body validation always fails."""

    @app.post("/_test/validation")
    async def _raise_validation(payload: _PayloadWithRaisingValidator) -> dict:
        return {"name": payload.name}  # pragma: no cover - never reached

    return app


async def test_validator_raising_value_error_returns_422_not_500(
    client, app_with_raising_route
):
    resp = await client.post("/_test/validation", json={"name": "anything"})

    # The bug made this a 500: the handler crashed encoding ctx.
    assert resp.status_code == 422, resp.text

    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["message"] == "Invalid request."

    # The details survive, and the validator's message is carried through.
    details = body["error"]["details"]
    assert isinstance(details, list) and details
    assert "boom" in details[0]["msg"]
    # The ctx that used to be unserialisable is now JSON-safe.
    assert "ValueError" not in str(details[0].get("ctx", {}).get("error", ""))


async def test_ordinary_validation_errors_are_unchanged(client):
    """The common path (a built-in constraint failing) still behaves as before."""
    resp = await client.post("/api/v1/auth/login", json={"email": "not-an-email"})

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert isinstance(body["error"]["details"], list)
