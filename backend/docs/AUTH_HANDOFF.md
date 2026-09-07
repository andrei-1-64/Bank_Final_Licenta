# Auth — how it's built

This originally was a handoff doc for whoever would build
`app/modules/auth`. That's done now (2026-08-18) — this describes what's
actually there, so anyone touching it (or the frontend consuming it)
doesn't have to reverse-engineer it from the diff.

The registration/login business logic (national ID validation, login
rate-limiting) was ported from a teammate's standalone Flask + Supabase
prototype into this FastAPI backend, translating field names to English
and moving the data onto the existing Postgres/SQLAlchemy setup rather
than a second database. See `app/modules/auth/validation.py`'s docstring
and `app/modules/auth/service.py` for that history.

Read `flow.md` at the repo root first for the overall project spec. This
document only covers the auth boundary.

## The contract

### Database

```
users
  id (UUID pk), email (unique), password_hash,
  first_name, last_name, email_verified (default false),
  national_id (unique, nullable), gender, date_of_birth,
  phone (nullable), address (nullable), created_at

sessions   -- ORM class name is UserSession, not Session (see why below)
  id (UUID pk), user_id -> users.id (CASCADE), token_hash (unique),
  expires_at (TIMESTAMPTZ), created_at

login_attempts   -- append-only, used only for login rate-limiting
  id (UUID pk), email, success (bool), created_at
```

Models: `app/modules/users/models.py::User`,
`app/modules/auth/models.py::UserSession` and `LoginAttempt`. `UserSession`
(table is still `sessions`) avoids colliding with `sqlalchemy.orm.Session`
/ `AsyncSession` in files that import both.

`national_id`/`gender`/`date_of_birth`/`phone`/`address` are nullable at
the DB level (not every `User` row necessarily comes through
self-registration - test fixtures don't set them) but the `/auth/register`
endpoint always requires and validates `national_id`, `email`, `password`,
`first_name`, `last_name`.

### `app/core/security.py` — primitives, don't reimplement

```python
hash_password(password: str) -> str
verify_password(password: str, password_hash: str) -> bool
generate_session_token() -> str      # raw token -> goes in the cookie
hash_session_token(token: str) -> str  # sha256 hex -> goes in sessions.token_hash
```

Session design: the cookie carries one opaque random token. It is **never**
stored raw — only `hash_session_token(token)` goes in the DB, the same
"store a hash, not the secret" pattern as passwords (SHA-256 here, not
bcrypt, because the token already has 256 bits of entropy — there's nothing
for a slow KDF to protect against).

### `app/core/dependencies.py::get_current_user` — the read side

```python
async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
```

Reads `request.cookies[settings.SESSION_COOKIE_NAME]`, hashes it, looks up
`sessions.token_hash`, checks `expires_at`, returns the `User` (via an
eager-loaded relationship, so no extra query). Raises `UnauthorizedError`
(401) for: no cookie, unknown token, expired session. Every protected route
depends on this; it never writes to `sessions`, only `app/modules/auth/service.py`
does. This is also why the test suite could authenticate before this module
existed: `tests/conftest.py`'s `user_factory`/`session_token_factory`
fixtures insert directly using these same primitives, and still do — other
modules' tests use them for cheap setup rather than exercising the real
endpoints.

### Config (`app/config.py`)

```python
SESSION_COOKIE_NAME: str = "session_token"
SESSION_TTL_SECONDS: int = 60 * 60 * 24 * 7   # 7 days
```

## The endpoints

`app/modules/auth/router.py` (mounted at `/api/v1/auth` in `app/api/v1.py`):

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/auth/register` | see below | `201` `UserRead`, sets session cookie (auto-login) |
| POST | `/auth/login` | `{email, password}` | `200` `UserRead`, sets session cookie |
| POST | `/auth/logout` | — | `204`, clears session cookie (requires being logged in) |

```json
// POST /auth/register
{
  "email": "jane@example.com", "password": "at least 8 chars",
  "first_name": "Jane", "last_name": "Doe",
  "national_id": "1234567890123",
  "phone": "optional", "address": "optional"
}
```

`national_id` must be a structurally valid Romanian CNP - checked in
`app/modules/auth/validation.py::validate_national_id` (length, digit
ranges, and the official MOD-11 check digit, not just "13 digits").
`gender` and `date_of_birth` are derived from it automatically
(`extract_gender`, `extract_date_of_birth`) - don't accept them as
separate input. That file also has `validate_iban` (MOD-97), unused so
far but there for whenever a beneficiaries/payments feature needs to
validate external account numbers.

None of these are money-moving endpoints, so no `Idempotency-Key` header
is needed (that mechanism is specific to transfers/payments/cards/etc.
per flow.md rule #5).

### Errors

Same shape as everywhere else - `{"error": {"code", "message"}}`, via
`app/core/exceptions.py`:

- `validation_error` (422) — bad national ID, password too short, etc.
- `email_already_registered` / `national_id_already_registered` (409) —
  distinguished by inspecting the Postgres unique-constraint name that
  fired (see `_duplicate_registration_error` in service.py).
- `unauthorized` (401) — bad login. **Deliberately the same message**
  whether the email doesn't exist or the password is wrong, to avoid
  leaking which accounts exist.
- `login_rate_limited` (429) — 5 failed attempts for the same email within
  15 minutes (`LOGIN_MAX_FAILED_ATTEMPTS` / `LOGIN_LOCKOUT_WINDOW_MINUTES`
  in service.py), tracked via the append-only `login_attempts` table.
  Applies even to the correct password until the window passes.

A subtlety worth knowing if you touch `login_user`: recording a *failed*
attempt has to `await db.commit()` immediately, before raising
`UnauthorizedError` - otherwise `get_db`'s rollback-on-exception wraps the
whole request, including the attempt row that the rate limit depends on,
and 5 failed logins would never actually trip the limit. (Money-writing
code elsewhere wants the opposite - a failed transfer should roll back
everything - so this pattern is specific to attempt-logging, not a general
rule.)

## Dev workflow

Data layer moved to Supabase REST (`supabase-py`) — see
`backend/supabase/migrations/*.sql` for the schema and the ledger's RPC
functions, and `db/supabase_client.py` for the client + error mapping. No
local Postgres, no Alembic: schema changes are applied by pasting the
relevant `.sql` file into the Supabase SQL Editor (this was built on a
network that blocks direct Postgres ports 5432/6543 — only 443/HTTPS is
open, which is all PostgREST needs).

```bash
cd backend
cp .env.example .env        # fill in SUPABASE_URL / SUPABASE_KEY (service_role)
docker compose up -d --build
docker compose exec backend pytest -q          # full suite (127 tests)
docker compose exec backend ruff check .        # lint
docker compose exec backend mypy app            # types
```

`./app` and `./tests` are bind-mounted into the container, so edits on the
host show up immediately — no rebuild needed unless `pyproject.toml` (new
dependency) or the Dockerfile changes.

Tests: `tests/conftest.py` runs everything against the REAL Supabase
project pointed at by `SUPABASE_URL`/`SUPABASE_KEY` (not a local database —
none exists). `clean_db` deletes every row from every table before each
test, so never point this at a project you want to keep data in. The
concurrency tests still exercise real Postgres row locking (`FOR UPDATE`
inside the `post_transaction` RPC function) — reached over REST instead of
a direct connection, but the guarantee itself is unaffected by that.
`tests/integration/test_auth_api.py` covers register/login/logout/
rate-limiting/duplicates end-to-end through the real endpoints;
`tests/unit/test_national_id_validation.py` covers the CNP/IBAN math
directly.

## Known gaps worth knowing about

- **No password reset / email verification / "remember me"** — not in
  flow.md's spec; `email_verified` exists as a column but nothing sets it
  to true anywhere.
- **General rate limiting** (`core/middleware.py`) is global, in-process,
  per-IP — separate from and in addition to the per-email login limiter
  above. Neither survives a restart or scales past one process; would need
  Redis for anything real.
- **CNP → gender/date_of_birth derivation** trusts the submitted national
  ID's structure; there's no cross-check against a real government
  registry (out of scope for this exercise, same as flow.md's "no
  KYC/AML" rule).
