# Banking App — Backend Specification (for Claude Code)

## 1. What this project is

A **fully functional web banking application** (backend only in this spec).
The money is **fictive** — this is a serious engineering exercise, not a real
financial product. There is **no** KYC/AML, licensing, PCI, or real payment
provider. But the money must **behave like real money**: correct, auditable,
and impossible to accidentally create or destroy.

The app must let a logged-in user:
- Open and view accounts
- Transfer money between accounts
- Send money to saved payees (beneficiaries)
- Pay merchants / bills
- Issue and manage virtual cards (freeze, limit)
- Use a set of **unique features** (money pots, scheduled transfers, round-ups)
- Talk to an **AI agent** that can read their data and *propose* actions

**Team size: 2 people (A and B).** Ownership is marked throughout.

---

## 2. Non-negotiable rules (apply everywhere)

1. **No floats for money.** Store amounts as integer **minor units** (`BIGINT`,
   e.g. cents). Never `float`.
2. **Immutable double-entry ledger.** Balances are **never** stored or edited.
   A balance is the **sum of ledger entries**. Corrections = new reversing
   entries, never deletes.
3. **One money-writer.** Every money movement (transfer, payment, card spend,
   scheduled fire, round-up) goes through **`ledger.post_transaction()`** and
   nothing else.
4. **Atomic + concurrency-safe.** A movement is all-or-nothing in one DB
   transaction, with row locking so concurrent transfers cannot double-spend.
5. **Idempotency.** Every money-moving endpoint requires an `Idempotency-Key`
   header; a repeated key returns the original result, never a second effect.
6. **AI is untrusted.** The agent may **read** freely but may only **propose**
   writes. Money moves only after an explicit user confirmation through the
   normal validated endpoint. The agent never gets raw DB access.
7. **Secrets from env only.** No hard-coded keys. `.env` local, `.env.example`
   committed.

---

## 3. Tech stack

- **Language/framework:** Python + FastAPI
- **ORM/migrations:** SQLAlchemy 2.0 + Alembic
- **Database:** PostgreSQL (required — needs real ACID, row locking, `NUMERIC`)
- **Validation:** Pydantic
- **Auth:** cookie-based session (HttpOnly, SameSite), server-side sessions
- **AI model:** OpenAI `gpt-5-mini` via a swappable provider interface
- **Tests:** pytest
- **Runtime:** Docker + docker-compose, one-command start

---

## 4. Database schema

Money stored as `amount_minor BIGINT` (minor units) + `currency CHAR(3)`.
`id` = UUID everywhere. Timestamps `TIMESTAMPTZ`.

```
users
  id, email (unique), password_hash, full_name, created_at

sessions                         # cookie-based server session
  id, user_id -> users, token_hash, expires_at, created_at

accounts                         # NO balance column — balance is derived
  id, user_id -> users, name, currency, status(active|closed), created_at

journal_transactions             # groups the legs of ONE money movement
  id, reference, idempotency_key(unique), description, created_at

ledger_entries                   # IMMUTABLE, append-only. balance = SUM(entries)
  id, journal_id -> journal_transactions,
  account_id -> accounts, direction(debit|credit),
  amount_minor, currency, created_at
  # invariant: per journal, SUM(debits) == SUM(credits)

transfers                        # account -> account (internal)
  id, journal_id -> journal_transactions, from_account_id, to_account_id,
  amount_minor, currency, status(completed|failed), idempotency_key, created_at

beneficiaries                    # saved payees for "send money"
  id, user_id -> users, name, account_ref, created_at

payments                         # money OUT to a merchant/bill/payee
  id, user_id -> users, from_account_id -> accounts, payee,
  amount_minor, currency, status, journal_id, idempotency_key, created_at

cards                            # virtual cards
  id, account_id -> accounts, last4, status(active|frozen|cancelled),
  spending_limit_minor, created_at

pots                             # FEATURE: envelope budgeting (internal sub-accounts)
  id, account_id -> accounts, name, target_minor, created_at
  # invariant: SUM(pot balances) <= parent account balance

scheduled_transfers             # FEATURE: recurring standing orders (needs worker)
  id, user_id, from_account_id, to_account_id, amount_minor,
  frequency(daily|weekly|monthly), next_run_at, status(active|paused), created_at

conversations                    # AI chat
  id, user_id -> users, created_at

messages
  id, conversation_id -> conversations, role(user|assistant|tool),
  content, tool_calls_json, created_at

audit_log                        # append-only; who did what
  id, user_id, action, entity, metadata_json, created_at
```

---

## 5. Backend structure & ownership

`[A]` = Person A, `[B]` = Person B.

```
backend/
├── app/
│   ├── main.py                 [A] app factory: routers, middleware, lifespan
│   ├── config.py               [A] pydantic-settings; env only
│   │
│   ├── core/                   [A] SHARED SPINE — build first, then freeze
│   │   ├── money.py            [A]   minor-units money helpers ⭐
│   │   ├── idempotency.py      [A]   idempotency-key guard
│   │   ├── security.py         [A]   hashing, session-cookie signing
│   │   ├── exceptions.py       [A]   typed errors (InsufficientFunds, NotFound)
│   │   ├── audit.py            [A]   append-only audit writer
│   │   ├── middleware.py       [A]   CORS, request-id, error mapping, rate limit
│   │   └── dependencies.py     [A]   get_db, get_current_user  ← CONTRACT for B
│   │
│   ├── db/                     [A]  base, session, Alembic migrations/
│   │
│   ├── modules/
│   │   ├── ledger/             [A] ⭐ post_transaction() ← CONTRACT for B
│   │   ├── accounts/           [A]   open/close/list; balance derived
│   │   ├── transfers/          [A]   account -> account
│   │   ├── auth/               [A]   login, logout, session
│   │   ├── users/              [A]   profiles
│   │   ├── transactions/       [A]   read models: statements, history, filters
│   │   │
│   │   ├── beneficiaries/      [B]   saved payees
│   │   ├── payments/           [B]   pay merchant/bill (money out)
│   │   └── cards/              [B]   issue/freeze/limit; card state machine
│   │
│   ├── features/               [B]  UNIQUE features
│   │   ├── pots/               [B]   envelope budgeting (sum-invariant)
│   │   ├── scheduled_transfers/[B]   recurring; scheduler.py worker, idempotent
│   │   └── round_ups/          [B]   round card spend up → savings
│   │
│   ├── ai/                     [B]  self-contained agent module
│   │   ├── service.py          [B]   entry point /chat calls
│   │   ├── agent.py            [B]   tool loop: read freely, PROPOSE writes
│   │   ├── context.py          [B]   injects authed user identity (not model-supplied)
│   │   ├── prompts.py          [B]   system instructions + guardrails
│   │   ├── providers/          [B]   base.py, openai_provider.py, mock_provider.py
│   │   ├── tools/              [B]   base, registry, read_tools (get_balance, ...)
│   │   └── schemas.py          [B]
│   │
│   └── api/
│       └── v1.py               [A]  aggregates all routers under /api/v1
│
├── tests/                      each owner tests their own modules
│   ├── unit/                       money math, ledger invariants, card FSM
│   ├── integration/                concurrent transfers, idempotency, /chat (mock)
│   └── conftest.py             [A] shared fixtures
│
├── Dockerfile                  [A]
├── docker-compose.yml          [A] backend + Postgres
├── start.sh                    [A] one-command startup
├── .env.example                [A]
├── pyproject.toml              [A]
└── alembic.ini                 [A]
```

**Two contracts A must publish on day 1 so B is unblocked:**
1. `ledger.post_transaction(legs, idempotency_key, description) -> Journal`
2. `get_current_user` dependency (returns the authed user; scopes all data).

B builds `payments`, `cards`, `features`, and `ai` against these signatures
even before A finishes the internals.

---

## 6. The one flow that matters (every money movement)

```
transfers | payments | cards | scheduled_transfers | round_ups
                        │
                        ▼
           ledger.post_transaction()   ← atomic, double-entry, ONLY money-writer
             ├─ open one DB transaction
             ├─ lock affected accounts
             ├─ check available balance under lock
             ├─ append debit entry + credit entry
             ├─ assert SUM(debits) == SUM(credits)
             ├─ write audit_log
             └─ commit or roll back everything
```

The AI `/chat` flow:

```
POST /api/v1/chat  { conversation_id, message }
  → get_current_user            (agent acts AS this user only)
  → load history, append message
  → agent loop with gpt-5-mini:
       read tool  → execute, feed result back
       write tool → STOP, return a PROPOSAL for user to confirm
       text       → done
  → persist assistant message → return { reply, proposal? }
```

---

## 7. Build order

**Phase 0 (parallel):**
- A: Docker + start.sh + Postgres + health endpoint + CI + `core/money` +
  ledger + auth scaffold. Publish the two contracts.
- B: `ai/` provider interface + `mock_provider` + agent loop (against a stub
  `get_balance`); scaffold `payments`/`cards` routers against A's contracts.

**Sprint 1 — Definition of Done (proves the whole spine):**
> A logged-in user opens two accounts and transfers money between them,
> atomically, with tests proving no money is created or destroyed under
> **concurrent** transfers, and idempotency keys prevent double-spend.
> `./start.sh` brings everything up with one command.

**Then, incrementally:** beneficiaries → payments → cards → transactions view
→ pots → scheduled_transfers (worker) → round_ups → AI read tools →
AI propose-transfer confirmation flow → persisted conversations.

---

## 8. Instructions for Claude Code (per task)

For every task you (Claude Code) implement:
1. **Inspect** the existing repo, structure, tests, and config first.
2. **Plan** a short list of changes before writing code.
3. Make the **smallest clean change** that satisfies the task; do **not** modify
   unrelated functionality.
4. Follow existing project conventions.
5. **Add tests** for the behavior (not for coverage numbers).
6. **Run** tests, lint, and type checks where applicable.
7. Keep the ledger the **only** money-writer; keep the AI **read/propose only**.
8. Never hard-code secrets; read from env.
9. **Report** what changed, files touched, tests run + results, and any known
   limitations or unresolved issues.

**Git:** one branch per task (e.g. `feat/ledger-core`, `feat/auth-session`,
`feat/ai-agent-loop`). Small, meaningful commits. Keep `main` green.

---

## 9. Frontend

The frontend is a **separate project** at `frontend/` (sibling to
`backend/`), consuming `backend/`'s JSON API — this spec above is backend
only, per §1. `frontend/` is a plain HTML/CSS/JS app (no framework, no
build step), served by nginx as its own container; the browser calls the
backend directly, never through a server-side proxy. `./run.sh` (repo
root) starts the whole stack - database, API, and frontend - in Docker.
See `frontend/README.md` for the full API reference, the auth/cookie
contract, and what's live vs. still a mockup.
