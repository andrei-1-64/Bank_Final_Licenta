# Demo walkthrough

A script for the presenter to read while operating the app - not an
architecture doc. Exact clicks, exact things to say are up to you; this
gives the path and the expected output at each step so you know if
something has gone wrong.

Two demo users, seeded by `scripts/seed_demo_data.py`:

- **Ana Popescu** (`ana@demo.local`) - clean, typical profile. No face
  enrolled.
- **Bogdan Ionescu** (`bogdan@demo.local`) - recurring subscriptions, one
  spending anomaly, face enrolled, a pre-loaded bank statement.

Password for both: `demo1234`.

---

## 1. Pre-flight (once, before any demo)

Do this the morning of the demo, not five minutes before.

1. Confirm `backend/.env`'s `SUPABASE_URL` points at the **dev** project
   (id `yksdhyyekltebuwrmtjj`), never the test project. `scripts/seed_demo_data.py`
   will refuse to run otherwise - that refusal is the check, don't skip it
   by adding `--confirm` out of habit.
2. From `backend/`, seed the demo data:
   ```
   python -m scripts.seed_demo_data
   ```
   Expect the last line of output to read:
   ```
   Seeded DEMO_USER_A (email: ana@demo.local) and DEMO_USER_B (email: bogdan@demo.local). Password for both: demo1234.
   ```
   If you want Bogdan's face-login step in the demo, drop a photo at
   `scripts/fixtures/demo_face.jpg` before this step - see
   `scripts/README.md`.
3. From the repo root, build and start the stack:
   ```
   docker compose up -d --build
   ```
   The frontend has no live-reload volume mount - it is a plain static
   site baked into its image at build time - so `--build` matters even if
   you think nothing changed. If you only touched frontend files:
   ```
   docker compose up -d --build frontend
   ```
4. Open `http://localhost:8080` in a browser. If a session is already
   active from earlier testing, click the logout icon (top right, the
   door/arrow icon) before you start - the demo script below assumes you
   begin logged out.
5. Have a second browser tab on `http://localhost:8000/docs` (FastAPI's
   own Swagger UI) open but hidden - useful if you need to sanity-check a
   response shape live without narrating a `curl` command.

## 2. Reset (before each demo run)

Data drifts as you click through it (new proposals, new conversations) -
reset before every run-through, not just the first one of the day.

```
python -m scripts.seed_demo_data
```

Then refresh the browser tab (`F5`). No need to restart the containers -
the seed script only touches Supabase, and the frontend is stateless.

## 3. Demo path (~10 minutes)

### 3.1 Login as Ana - a normal balance question

1. Go to `http://localhost:8080/login.html`, log in as `ana@demo.local` /
   `demo1234`.
2. Ask: **"Care este soldul meu?"**
   Expect a factual balance answer, and the tag **"→ Bancar"** above the
   reply bubble - this is BankingAgent answering a plain factual question,
   no interpretation.

### 3.2 A transfer proposal - and the password step-up

3. Ask: **"Vreau să transfer 100 RON către contul meu de economii."**
   Expect BankingAgent to come back with a proposal card (confirm/reject),
   not an executed transfer - the AI layer never executes money movement
   directly.
4. Click **Confirm**. The step-up modal opens with a password field.
   **Ana has no face enrolled**, so the face option is unavailable here -
   this is the `require_enrolled` behavior from Step 16 Priority 1: the
   app must not silently fall back to "no confirmation needed" just
   because a user has no face on file. Enter `demo1234` and submit.
5. Expect the proposal card to resolve to "confirmed" and the transfer to
   show up if you check the accounts view.
6. Log out (top-right icon).

### 3.3 Login as Bogdan - recurring expenses

7. Log in as `bogdan@demo.local` / `demo1234`.
8. Ask: **"Am cheltuieli recurente?"** (or: "ce abonamente am?")
   Expect the tag **"→ Analiză"** (InsightsAgent), and an answer naming
   **Netflix** and **Spotify** as recurring monthly charges on the same
   card - this is `detect_recurring_payments` finding the two-month,
   stable-amount pattern the seed data built in on purpose.

### 3.4 Unusual expenses - the anomaly

9. Ask: **"Am cheltuieli neobișnuite în ultima vreme?"**
   Expect InsightsAgent to flag the one large **EMAG.RO** charge from the
   last few days as unusual - `detect_anomalies` comparing it against
   Bogdan's normal spending range.

### 3.5 THE HANDOFF DEMO - cancel the Netflix subscription

This is the centerpiece: a single user turn crossing two agents in one
reply.

10. Ask: **"Vreau să anulez abonamentul Netflix."**
    What should happen, in order:
    - InsightsAgent recognizes this as a request to stop a recurring
      payment it already identified, not just another analytics question.
    - It calls `handoff_to_agent`, naming the card the Netflix charges
      land on and the amount, and hands the rest of the turn to
      BankingAgent - same turn, no extra round-trip from the user.
    - BankingAgent proposes a `cancel_card` action on that card.
    - The reply's tag reads **"→ Analiză → Bancar"** - the chain, not just
      the last hop. Point this out; it's the whole point of Step 15.
11. Click **Confirm**. The step-up modal opens - **Bogdan has face
    enrolled**, so the face option is available. Click the face button,
    let the modal capture (or upload) a frame, confirm. This is the
    contrast with 3.2's password path: same proposal flow, different
    step-up method, driven entirely by `require_enrolled`.

### 3.6 The pre-seeded statement

12. Open the pre-existing conversation that has a statement attached (it
    was created by the seed script, not by you - look for it in the
    conversation list, or ask "arată-mi ultimul extras încărcat").
13. Ask: **"Se potrivește extrasul cu contul meu?"**
    Expect `compare_statement_to_ledger` to run and report on differences
    between the extracted (unverified) statement rows and the real ledger
    - point out the UI's own disclaimer that statement rows are
    auto-extracted and unverified, never written to the ledger.

### 3.7 DocumentAgent isolation - no handoff

14. Upload any plain PDF (not a bank statement) to a new conversation, or
    open one already attached to a document.
15. Ask a question about its contents, e.g. **"Ce spune acest document?"**
    Expect the tag to stay **"→ Documente"** for the whole exchange - no
    handoff chain, even if you follow up with a question that sounds
    banking-ish. This is deliberate: an active document pins routing to
    DocumentAgent (Step 12's context-override rule) so the model never
    mixes untrusted document content into a money-moving conversation.

## 4. Recovery - if something goes wrong mid-demo

- **Azure OpenAI 503 / timeout**: The agent's fallback reply is a plain
  Romanian apology, not a crash. Say "let's try that again" and re-send
  the same message - it's usually transient. If it persists for more than
  two tries, fall back to narrating what *should* happen from this doc
  rather than fighting the live model.
- **Supabase pool exhaustion** (requests start timing out or 500ing
  across the board): stop clicking, wait ~10 seconds, retry once. If it
  doesn't recover, `docker compose restart backend` and re-open the
  frontend tab - this doesn't lose seeded data, only in-flight requests.
- **Bad/missing seed data mid-demo** (e.g. someone else ran the seed
  script against the wrong thing, or you're not seeing what this doc
  says you should): stop, re-run `python -m scripts.seed_demo_data`,
  refresh the browser, and resume from the start of whichever numbered
  step you were on - the reset is cheap and the whole path is short.
- **A proposal gets stuck "pending" and Confirm does nothing**: refresh
  the page. If it's still stuck, reject it and re-ask the same request -
  a stuck proposal is a UI/session hiccup, not a sign the underlying data
  is broken.

## 5. What NOT to demo, and why

- **Disputing a transaction.** There is no dispute action in this app.
  Do not let a question wander there ("can I dispute this charge?") -
  redirect back to what actually exists (cancel the card, not the
  charge).
- **Handoff chains longer than two hops.** Only one handoff per turn is
  supported (`ALLOWED_HANDOFF_TARGETS` is a fixed, one-level table) -
  don't imply the system can chain three or four agents together. If
  asked, say it's a deliberate scope limit, not a bug.
- **Cross-user data.** Never show Ana's data while logged in as Bogdan or
  vice versa, and don't frame a question as if the AI could see both -
  every tool call is scoped to the logged-in user's own accounts, and
  demonstrating otherwise would undercut the exact isolation guarantee
  the app is built on.

## 6. Post-demo cleanup

Log out. That's it - the seed data is safe to leave in place; it will be
wiped and rebuilt automatically the next time `scripts/seed_demo_data.py`
runs, and it never touches any real user's data regardless of how many
times it's left sitting there.
