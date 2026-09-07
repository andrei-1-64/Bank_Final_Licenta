# BanK frontend

Plain HTML, CSS and JavaScript — no framework, no build step. nginx serves
these files as a container; everything dynamic happens in the browser by
calling the backend's JSON API.

```
index.html      dashboard (accounts, transactions, transfers, cards)
login.html      /register.html    auth pages
api.js          apiFetch() wrapper + money formatting + session guard
app.js          all dashboard behaviour
language.js     shared Romanian / Ukrainian / English page translator
style.css       styles
```

## Language selector

Every screen includes a top-right language selector. Romanian is the default;
the selected Romanian, English, Ukrainian, Hungarian, Turkish, Italian,
Spanish, French, or German option is saved in browser
`localStorage` under `bank_preferred_language`. `language.js` loads a local
JSON bundle from `i18n/`, translates the page in place, and caches bundles in
browser storage for instant return visits. No translation service is used.

## Running it

From the repo root — this starts the database, the API *and* this frontend:

```bash
./run.sh          # or  .\run.ps1  on PowerShell
```

Then open **http://localhost:8080**. `./run.sh stop` shuts it down,
`./run.sh logs` follows the logs.

Editing an `.html`/`.js`/`.css` file here needs a rebuild to show up, since
the files are baked into the image:

```bash
docker compose up -d --build frontend
```

## How auth works (read this first)

The backend uses **cookie-based sessions**: after logging in the browser
holds an `HttpOnly` cookie and sends it automatically. Two consequences:

1. **Every API call needs `credentials: "include"`.** The API is a
   different origin (`:8000`) from this app (`:8080`), so without it the
   browser silently omits the cookie and every request looks logged-out.
   `api.js::apiFetch` already does this — use it rather than raw `fetch`.
2. **The backend's `CORS_ORIGINS` must list this app's origin.**
   `http://localhost:8080` is already there (`backend/.env.example`). If
   you serve this from a different port, add it there too, or requests
   fail with an opaque CORS error before reaching the API.

There's deliberately no server-side code here: nothing proxies the API and
nothing touches the session cookie except the browser itself.

## What's connected vs. what's still a mockup

| Area | Status |
|---|---|
| Register / log in / log out | **Live** |
| Accounts (list, balances, open, close) | **Live** |
| Transfers between your own accounts | **Live** |
| Transactions list | **Live** |
| Cards (issue, list, cancel) | **Live** |
| AI chat | Mockup — the backend's AI module has no HTTP endpoint yet |
| Investments / budgeting | Mockup — no backend feature exists |

Mockup sections are labelled "Demo" in the UI so nobody mistakes them for
working features.

Registration requires a valid Romanian national ID (CNP) — the backend
verifies the real checksum, not just that it's 13 digits.

## API reference

Base URL: `http://localhost:8000/api/v1` (interactive, always-current docs
at `http://localhost:8000/docs`). All money amounts are integer **minor
units** (cents) — `1250` means `$12.50`, never a float.

Every error response has the same shape:
```json
{ "error": { "code": "insufficient_funds", "message": "..." } }
```

### Accounts

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/accounts` | `{name, currency}` | `201` account |
| GET | `/accounts` | — | `200` list of accounts |
| GET | `/accounts/{id}` | — | `200` account |
| POST | `/accounts/{id}/close` | — | `200` account (fails `409` if balance isn't zero) |
| GET | `/accounts/{id}/transactions` | query: `date_from`, `date_to`, `limit`, `offset` | `200` list of ledger entries |

Account shape:
```json
{
  "id": "uuid", "name": "Checking", "currency": "USD",
  "status": "active", "balance_minor": 12500,
  "created_at": "2026-01-01T00:00:00Z"
}
```

### Transfers

Between two accounts **owned by the same logged-in user** (moving your own
money between your own accounts — sending to someone else is a different,
not-yet-built feature).

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/transfers` | see below, **requires `Idempotency-Key` header** | `201` transfer |
| GET | `/transfers` | — | `200` list |
| GET | `/transfers/{id}` | — | `200` transfer |

```json
// POST /transfers request body
{
  "from_account_id": "uuid", "to_account_id": "uuid",
  "amount_minor": 2500, "currency": "USD", "description": "optional"
}
```

The `Idempotency-Key` header must be a unique string per logical transfer
attempt (a `crypto.randomUUID()` generated once per form submission, and
**reused on retry** of that same submission — never a new one per retry,
or the whole point of it is lost). Submitting the same key twice returns
the original result instead of moving money again — that's what makes it
safe to retry a failed request without double-charging.

### Users

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/users/me` | — | `200` current user |
| PATCH | `/users/me` | `{first_name?, last_name?, phone?, address?}` | `200` updated user |

User shape:
```json
{
  "id": "uuid", "email": "jane@example.com",
  "first_name": "Jane", "last_name": "Doe", "email_verified": false,
  "national_id": "1234567890123", "gender": "F", "date_of_birth": "1990-01-01",
  "phone": null, "address": null, "created_at": "2026-01-01T00:00:00Z"
}
```

### Auth

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/auth/register` | see below | `201` user, sets session cookie (auto-login) |
| POST | `/auth/login` | `{email, password}` | `200` user, sets session cookie |
| POST | `/auth/logout` | — | `204`, clears session cookie |

```json
// POST /auth/register request body
{
  "email": "jane@example.com", "password": "at least 8 chars",
  "first_name": "Jane", "last_name": "Doe",
  "national_id": "1234567890123",
  "phone": "optional", "address": "optional"
}
```

`national_id` must be a structurally valid Romanian CNP (13 digits,
correct checksum) — `gender` and `date_of_birth` are derived from it
automatically, don't send them separately. Login failures always return
the same generic `401` message regardless of whether the email exists or
the password is wrong (avoids leaking which accounts exist); after 5
failed attempts for the same email within 15 minutes, login returns `429`
(`login_rate_limited`) even for the correct password until the window
passes.
