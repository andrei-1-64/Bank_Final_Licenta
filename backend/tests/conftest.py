"""Shared fixtures.

Tests run directly against the REAL hosted Supabase project, over HTTPS -
this machine's network blocks direct Postgres ports (5432/6543), and there
is no local database anywhere in this app anymore (see the Supabase REST
migration notes in docs/AUTH_HANDOFF.md). The concurrency tests still prove
real row-locking: Postgres still applies `FOR UPDATE` for real inside the
post_transaction/create_transfer RPC functions, just reached over
REST/RPC instead of a direct connection - the guarantee itself doesn't
depend on how the app talks to Postgres.

WARNING: `clean_db` deletes every row from every table before each test,
against whatever project SUPABASE_URL/SUPABASE_KEY point at. Never point
this at a project you want to keep data in.

Auth note: tests authenticate by writing a `sessions` row directly and
setting the cookie by hand, using exactly the primitives core/security.py +
core/dependencies.py define - proving those primitives are a self-contained
contract that doesn't require going through the login endpoint.
"""

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient

from app.ai.providers.mock_provider import MockProvider
from app.config import settings
from app.core.security import generate_session_token, hash_password, hash_session_token
from app.db.supabase_client import get_supabase
from app.main import create_app
from app.modules.chat.router import get_model_provider
from app.modules.users.schemas import UserRead
from supabase import AsyncClient, acreate_client

# Children-before-parents, respects the ON DELETE RESTRICT constraints.
_TABLES_IN_FK_ORDER = (
    "sessions",
    "login_attempts",
    "card_orders",
    "payments",
    "beneficiaries",
    "cards",
    "transfers",
    "ledger_entries",
    "journal_transactions",
    "audit_log",
    "messages",
    "proposals",
    "statement_rows",
    "statements",
    "documents",
    "conversations",
    "accounts",
    "users",
)

# PostgREST rejects an unconditional DELETE; this filter matches every real
# row since no id is ever the nil UUID.
_NIL_UUID = "00000000-0000-0000-0000-000000000000"


# Optional, separate Supabase project reserved for tests (see
# backend/.env.example). When set, clean_db's wipe is provably scoped to
# disposable test data instead of whatever the app's own SUPABASE_URL points
# at - this is what actually happened once already: an ordinary `pytest` run
# wiped the shared dev project because no separate test project existed yet.
_TEST_SUPABASE_URL = os.environ.get("TEST_SUPABASE_URL")
_TEST_SUPABASE_KEY = os.environ.get("TEST_SUPABASE_KEY")


@pytest_asyncio.fixture
async def supabase() -> AsyncIterator[AsyncClient]:
    """Function-scoped (not session-scoped): pytest-asyncio gives each test
    function its own event loop by default, and an AsyncClient created
    against one loop breaks ("Event loop is closed") once reused from a
    later test's different loop. A fresh client per test avoids that."""
    url = _TEST_SUPABASE_URL or settings.SUPABASE_URL
    key = _TEST_SUPABASE_KEY or settings.SUPABASE_KEY
    client = await acreate_client(url, key)
    yield client


@pytest_asyncio.fixture(autouse=True)
async def clean_db(supabase: AsyncClient) -> None:
    """Runs before every test: empties every table (child tables first).

    If TEST_SUPABASE_URL isn't configured, this is the ONLY thing standing
    between a routine test run and wiping every row in the DEV project - so
    require an explicit opt-in env var in that case, to make sure that can
    never happen again by accident (e.g. an IDE auto-running tests, or a CI
    job with the wrong .env wired in).
    """
    if not _TEST_SUPABASE_URL and os.environ.get("ALLOW_TEST_DB_WIPE") != "1":
        pytest.fail(
            "Refusing to run: no TEST_SUPABASE_URL/TEST_SUPABASE_KEY configured "
            "(see backend/.env.example), so this suite would wipe every row in "
            f"the DEV project at {settings.SUPABASE_URL!r} before each test (see "
            "clean_db in conftest.py). Set up a separate Supabase project for "
            "tests, or set ALLOW_TEST_DB_WIPE=1 only if you are sure that "
            "project's data is disposable.",
            pytrace=False,
        )
    for table in _TABLES_IN_FK_ORDER:
        await supabase.table(table).delete().neq("id", _NIL_UUID).execute()


@pytest.fixture
def app(supabase: AsyncClient):
    async def override_get_supabase() -> AsyncIterator[AsyncClient]:
        yield supabase

    application = create_app()
    application.dependency_overrides[get_supabase] = override_get_supabase
    return application


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[HTTPXAsyncClient]:
    transport = ASGITransport(app=app)
    async with HTTPXAsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def user_factory(supabase: AsyncClient) -> Callable[..., Awaitable[UserRead]]:
    async def _factory(
        email: str | None = None,
        first_name: str = "Test",
        last_name: str = "User",
        referral_bonus_eligible: bool = True,
    ) -> UserRead:
        # Defaults to eligible so the many pre-existing tests that assume a
        # funded account (via OPENING_BALANCE_MINOR) don't all need updating
        # for a feature they aren't testing - pass False explicitly in tests
        # that exercise the referral gating itself.
        email = email or f"user-{uuid.uuid4().hex[:8]}@example.com"
        resp = (
            await supabase.table("users")
            .insert(
                {
                    "email": email,
                    "password_hash": hash_password("password123"),
                    "first_name": first_name,
                    "last_name": last_name,
                    "referral_bonus_eligible": referral_bonus_eligible,
                }
            )
            .execute()
        )
        return UserRead.model_validate(resp.data[0])

    return _factory


@pytest.fixture
def session_token_factory(supabase: AsyncClient) -> Callable[..., Awaitable[str]]:
    async def _factory(user: UserRead, *, expired: bool = False) -> str:
        token = generate_session_token()
        delta = timedelta(days=-1) if expired else timedelta(seconds=settings.SESSION_TTL_SECONDS)
        await supabase.table("sessions").insert(
            {
                "user_id": str(user.id),
                "token_hash": hash_session_token(token),
                "expires_at": (datetime.now(UTC) + delta).isoformat(),
            }
        ).execute()
        return token

    return _factory


@pytest.fixture
def account_factory(supabase: AsyncClient) -> Callable[..., Awaitable[dict]]:
    async def _factory(user: UserRead, name: str = "Account", currency: str = "USD") -> dict:
        resp = (
            await supabase.table("accounts")
            .insert(
                {
                    "user_id": str(user.id),
                    "name": name,
                    "currency": currency,
                    "status": "active",
                }
            )
            .execute()
        )
        return resp.data[0]

    return _factory


@pytest.fixture
def conversation_factory(supabase: AsyncClient) -> Callable[..., Awaitable[dict]]:
    """Inserts a bare conversation row with no messages - the only way to get
    one via the DB directly, since the /chat endpoint always writes at least
    one message alongside the conversation it creates."""

    async def _factory(user: UserRead, title: str | None = None) -> dict:
        resp = (
            await supabase.table("conversations")
            .insert({"user_id": str(user.id), "title": title})
            .execute()
        )
        return resp.data[0]

    return _factory


@pytest.fixture
def statement_factory(supabase: AsyncClient) -> Callable[..., Awaitable[dict]]:
    """Inserts a bare statement row directly - the AzDI-backed upload
    endpoint isn't exercised by every test that just needs a statement to
    exist (see statements/service.py and the InsightsAgent/DocumentAgent
    tests, which mock AzDI at the boundary instead of calling this)."""

    async def _factory(
        user: UserRead, conversation_id: str | None = None, **overrides: object
    ) -> dict:
        payload = {
            "user_id": str(user.id),
            "conversation_id": conversation_id,
            "bank_name": "Banca Test",
            "currency": "RON",
            "row_count": 0,
        }
        payload.update(overrides)
        resp = await supabase.table("statements").insert(payload).execute()
        return resp.data[0]

    return _factory


@pytest.fixture
def statement_row_factory(supabase: AsyncClient) -> Callable[..., Awaitable[dict]]:
    async def _factory(statement_id: str, **overrides: object) -> dict:
        payload = {
            "statement_id": statement_id,
            "description": "Test row",
            "amount": -10.0,
            "currency": "RON",
            "row_index": 0,
        }
        payload.update(overrides)
        resp = await supabase.table("statement_rows").insert(payload).execute()
        return resp.data[0]

    return _factory


@pytest.fixture
def scripted_provider(app):
    """Override the AI endpoints' provider with a script the test controls.

    Shared by test_chat_api.py and test_conversations_api.py; returns a
    callable so a test can install its own script, and hands back the
    provider instance for assertions about what the model was shown.
    """

    def _install(*responses) -> MockProvider:
        provider = MockProvider(list(responses), repeat_last=True)
        app.dependency_overrides[get_model_provider] = lambda: provider
        return provider

    yield _install
    app.dependency_overrides.pop(get_model_provider, None)


@pytest.fixture
def seed_balance_factory(supabase: AsyncClient) -> Callable[..., Awaitable[None]]:
    """TEST-ONLY: credits an account directly via a single-sided ledger
    entry, bypassing post_transaction's debit/credit balance check.

    There is currently no deposit/external-funding concept anywhere in
    flow.md's schema or module list - the spec only covers MOVEMENT of
    money between accounts, not how it enters the system - and
    post_transaction would reject debiting a zero-balance "funding"
    account anyway (insufficient funds). So this is the only way to give a
    test account an opening balance. It is never used by application code.
    """

    async def _seed(account_id: uuid.UUID | str, amount_minor: int, currency: str = "USD") -> None:
        journal = (
            await supabase.table("journal_transactions")
            .insert(
                {
                    "reference": "TEST-SEED",
                    "idempotency_key": f"test-seed-{uuid.uuid4()}",
                    "description": "Test fixture seed balance",
                }
            )
            .execute()
        ).data[0]
        await supabase.table("ledger_entries").insert(
            {
                "journal_id": journal["id"],
                "account_id": str(account_id),
                "direction": "credit",
                "amount_minor": amount_minor,
                "currency": currency,
            }
        ).execute()

    return _seed


@pytest_asyncio.fixture
async def authed_client_factory(app, user_factory, session_token_factory):
    """For tests that need more than one identity (e.g. proving user A
    can't see user B's accounts). Each call spins up its own AsyncClient
    (own cookie jar) logged in as a fresh user, unless one is passed in."""
    clients: list[HTTPXAsyncClient] = []

    async def _make(user: UserRead | None = None) -> tuple[HTTPXAsyncClient, UserRead]:
        if user is None:
            user = await user_factory()
        token = await session_token_factory(user)
        transport = ASGITransport(app=app)
        ac = HTTPXAsyncClient(transport=transport, base_url="http://test")
        ac.cookies.set(settings.SESSION_COOKIE_NAME, token)
        clients.append(ac)
        return ac, user

    yield _make

    for ac in clients:
        await ac.aclose()


@pytest_asyncio.fixture
async def authed_client(authed_client_factory) -> tuple[HTTPXAsyncClient, UserRead]:
    return await authed_client_factory()


@pytest_asyncio.fixture
def enroll_face(supabase: AsyncClient) -> Callable[[uuid.UUID], Awaitable[str]]:
    """Test-only shortcut for the mandatory face-confirmation step-up (see
    face_auth/service.py::enforce_face_confirmation): seeds a
    face_credentials row directly (a fake embedding - real face matching is
    vision-service's own concern, not this test's) and a face_confirmations
    row already eligible to be consumed, returning its plaintext token so a
    test can pass it straight as X-Face-Confirmation. Mirrors
    create_face_confirmation's own token_hash scheme exactly, since that
    function needs a real photo an integration test can't produce."""

    async def _enroll(user_id: uuid.UUID) -> str:
        import hashlib
        import secrets

        await supabase.table("face_credentials").insert(
            {"user_id": str(user_id), "embedding": [0.0] * 128}
        ).execute()

        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires_at = datetime.now(UTC) + timedelta(minutes=3)
        await supabase.table("face_confirmations").insert(
            {
                "user_id": str(user_id),
                "token_hash": token_hash,
                "expires_at": expires_at.isoformat(),
            }
        ).execute()
        return token

    return _enroll
