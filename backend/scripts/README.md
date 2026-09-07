# scripts/

Dev-only one-off scripts. None of these are imported by the app itself; run
them with `python -m scripts.<name>` from `backend/`.

## Seed data

`seed_demo_data.py` seeds the two fixed demo users the presenter demo
(`docs/DEMO.md`) runs against: **Ana Popescu** (a clean, typical profile)
and **Bogdan Ionescu** (recurring subscriptions, one spending anomaly, face
login enrolled, a pre-loaded bank statement).

```
python -m scripts.seed_demo_data
```

**What it does**

- Wipes and reseeds **only** these two users' own data - never touches any
  other user, never truncates a whole table.
- Ana (`ana@demo.local`): 2 accounts (current + savings), one card, ~30
  days of ordinary transactions (groceries, transport, rent, salary). No
  face enrolled - this is the pair to Bogdan's face login, and demonstrates
  the `require_enrolled` distinction from Step 16 Priority 1.
- Bogdan (`bogdan@demo.local`): 3 accounts (current, savings, term
  deposit), one card, ~60 days of transactions including two Netflix and
  two Spotify charges (stable amount, ~30 days apart, on that one card -
  drives the `detect_recurring_payments` + Step 15 cross-agent handoff
  demo) and one anomaly (drives `detect_anomalies`). Face enrolled from
  `scripts/fixtures/demo_face.jpg` if that file is present, otherwise
  enrollment is skipped with a logged warning. One conversation pre-created
  with a statement upload attached (`scripts/fixtures/demo_statement.json`,
  run through the real statement extractor).
- Password for both users: `demo1234`.

**Which project it targets**

DEV only. The script refuses to run unless `SUPABASE_URL` looks like the
dev project, or `--confirm` is passed explicitly:

```
python -m scripts.seed_demo_data --confirm   # only if you are certain
```

It never touches the test Supabase project, and it never runs raw SQL -
every write goes through the app's own service layer (`accounts_service`,
`cards_service`, `face_auth_service`, `conversations_service`,
`statements_service`) or, for backdated transaction history where
`post_transaction` structurally cannot be used (see the module docstring),
the same direct `journal_transactions`/`ledger_entries` insert pattern
`seed_fake_purchases.py` and `seed_dev_user.py` already use.

**Re-running**

Idempotent: run it as many times as you like. Each run wipes the two demo
users' existing data first, then rebuilds it, so the end state is always
the same shape (same accounts, same transaction history, same card/face
setup) even though the underlying row ids are fresh each time. Safe to run
before every demo.

**Adding the face fixture**

`scripts/fixtures/demo_face.jpg` is not included in the repo. To enable
Bogdan's face-login step in the demo, drop a face photo at that path and
re-run the script - face enrollment is skipped (with a warning, not an
error) whenever the file is absent.

## Other scripts

- `seed_dev_user.py` - one funded user for developing the AI layer against.
- `seed_fake_purchases.py` - fake purchase history for an existing user.
- `seed_insights_test_data.py` - fixtures for exercising the insights tools.
- `ingest_knowledge_base.py` - loads the static knowledge base documents.
