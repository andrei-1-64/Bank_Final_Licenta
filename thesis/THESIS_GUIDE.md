# BanK — Thesis Companion Guide

This file is written **for an AI assistant** (a Claude.ai Project set up to help
draft the bachelor's thesis — "licența" — built around this repository). If
you are that assistant: read this file, `thesis/daily-notes.md`, `README.md`
and `flow.md` before drafting or editing any thesis text. They are the
source of truth; this guide summarizes and points you to the rest.

If you are the author re-reading this later: this is also your own map back
into the project's facts and your own voice, so you don't have to re-derive
either from scratch every time you pick the thesis back up.

## 1. What BanK actually is

A fully functional fictitious-money web banking application with an AI
agent layer, built as the practical component of the thesis. Two voices
matter and should not be mixed:

- **The thesis itself** is written in academic English: third person, precise,
  past tense for what was built and done, present tense for what the system
  *is*. No "I think" — findings are stated, then supported.
- **`daily-notes.md`** is the author's own first-person working journal —
  casual, technical, present-as-it-happened. Use it to recover *why* a
  decision was made and *what it felt like to build*, then translate that
  into the thesis's academic register. Never paste a journal sentence into
  the thesis verbatim — the register is wrong for both directions.

This project is treated as the author's own ongoing, individually
maintained system for the purposes of the thesis: the codebase is described
by what it does today and by the work the author has personally done on it
(documented in `daily-notes.md`), not by a claim of having authored every
line from a blank repository. Don't invent a founding narrative beyond what
the notes actually support.

## 2. Project facts sheet (ground truth — don't contradict this)

**Architecture.** Three containers via `docker-compose.yml`, no local
database:

```
frontend (nginx, static HTML/CSS/JS, :8080)
   |  JSON over HTTPS, cookie session
   v
backend (FastAPI, :8000)
   |  HTTPS/REST + RPC             |  HTTPS
   v                                v
Supabase (hosted Postgres,     vision (FastAPI, internal-only)
 via PostgREST)                 OCR (tesseract) + face embeddings (dlib),
                                 stateless, nothing persisted
```

There is no ORM and no direct Postgres connection anywhere — the backend
talks to Supabase exclusively through its REST API (`supabase-py`), because
the network the project was built on blocks outbound Postgres ports (5432 /
6543) and only allows 443/HTTPS. This is a real architectural constraint,
not a stylistic choice, and it's worth a paragraph in the System Architecture
chapter.

**The money model (the load-bearing part of the whole system):**

- Amounts are stored as `amount_minor BIGINT` — integer minor units, never
  `float`. A balance is never stored; it is the sum of ledger entries.
- **Immutable double-entry ledger.** Corrections are new reversing entries,
  never edits or deletes.
- **One money-writer.** Every movement — transfer, payment, card spend,
  scheduled transfer, round-up — goes through `ledger.post_transaction()`
  and nothing else.
- **Atomic + concurrency-safe.** One DB transaction per movement, with row
  locking so concurrent transfers cannot double-spend. There are integration
  tests that fire concurrent transfers at the same account and assert the
  final balance is exactly what it should be — this is the single strongest
  piece of evidence for the "correctness" claims in the thesis.
- **Idempotency.** Every money-moving endpoint requires an `Idempotency-Key`
  header; replaying a key returns the original result, never a second effect.

**The AI layer.** Five specialized agents (banking, insights, planning,
documents, docs/RAG) behind keyword+LLM routing, backed by Azure OpenAI or a
mock provider. The rule that matters for the thesis's "trust boundary"
discussion: **the agent may read freely but only ever propose writes** —
every money-moving action still requires an explicit user confirmation
(password or Face ID) through the normal validated endpoint. The agent never
gets raw database access. This is a good case study for an "LLM agents in
regulated domains" related-work section.

**Identity & security.** Session-cookie auth, Face ID login (DIY, demo-grade
— no liveness detection for a static photo, though enrollment does require
a real multi-frame blink sequence from the browser, so a single still image
cannot enroll), ID-card and IBAN OCR onboarding via the vision microservice,
trusted devices, OTP-gated step-up auth for e-Sign (Ed25519 detached
signatures) on admin-issued documents. **OTP delivery runs over real Gmail
SMTP**, not the Microsoft Teams webhook the demo originally shipped with —
see the 2026-09-13 and 2026-09-14 entries in `daily-notes.md`; delivery has
been verified end to end with a real email landing in a real inbox, not
just a 204 response.

**Spending categorization uses real Merchant Category Codes.** The
insights layer classifies a transaction by first checking whether its
merchant matches a curated table of real, published card-network MCCs
(ISO 18245) — the same mechanism an actual card issuer uses — before
falling back to keyword matching and then a cached few-shot LLM
classifier for anything neither layer recognizes. Each category the tool
returns carries the real MCC(s) behind it when one applies, and the AI is
instructed to cite them rather than ever inventing one. Good material for
both the Implementation chapter (a concrete "we mirrored the real-world
mechanism instead of approximating it" decision) and Testing (verified
live: asked which MCCs were used, got real codes back for known merchants
and an honest "no MCC" for the two categories that came from the
fallback).

**The AI routing layer is keyword-based and was found to be fragile in
exactly the way that implies.** Two real bugs surfaced only once the
docs/RAG knowledge base actually had content to ground answers in
(2026-09-14): a plural Romanian word ("comisioane") didn't match its own
singular keyword stem and fell through to the wrong agent for a confidently
-worded but ungrounded answer; a generic time word ("anual") pre-empted a
more specific stem before the right agent ever saw the message, because of
registration order. Both are fixed, but the *pattern* — deterministic
keyword rules are simple and auditable, but silently miss inflected forms
and interact by registration order rather than specificity — is worth its
own paragraph in Related Work or Discussion: it is a genuine limitation of
the chosen approach, found empirically rather than assumed, with a
documented before/after fix for each case.

**Known limitations — state these plainly in the Results/Discussion
chapter, they are a strength (self-awareness) not a weakness:**

- No RLS policies: RLS is enabled on sensitive tables but no policy exists;
  the backend uses the `service_role` key everywhere, so data isolation
  between users relies entirely on hand-written `user_id` checks.
- Face ID has no liveness detection — a demo factor, not a real biometric
  boundary.
- IBANs are fictitious (valid MOD-97 checksum, no real bank behind the
  "BANK" code) — this system can never move real money externally.
- Rate limiting is in-process memory — would need Redis for multiple
  replicas.

**Testing.** `pytest` across three layers: `tests/unit` (money math, ledger
invariants, card state machine), `tests/integration` (hits a real,
disposable Supabase project, wiped before every test — concurrency and
idempotency proofs live here), `tests/ai` (fully offline, mocked provider).

## 3. Thesis blueprint

Standard structure for a software-engineering bachelor's thesis, adapted to
BanK. Use this as the chapter skeleton; the short excerpts under three of
the chapters below are *illustrative of register and depth only* — they are
not text to submit. The real chapters need the author's own analysis,
screenshots, and results.

1. **Abstract**
2. **Introduction** — motivation, objectives, thesis structure
3. **Related Work** — banking-software conventions (double-entry
   accounting, idempotent APIs), OTP/2FA norms, safety patterns for LLM
   agents with tool access
4. **Requirements Analysis** — functional requirements (accounts,
   transfers, beneficiaries, payments, cards, pots, scheduled transfers, AI
   chat, e-Sign) and non-functional requirements (auditability, atomicity,
   idempotency, data isolation)
5. **System Architecture** — the three-container topology, the
   Supabase-over-REST decision, ledger design, the AI trust boundary
6. **Implementation** — walkthrough of the key modules and the engineering
   decisions behind them (integer minor units, idempotency keys, row
   locking, Ed25519 signatures, the OTP delivery evolution)
7. **Testing & Validation** — the three test layers, the concurrency proof,
   a short security review checklist
8. **Results & Discussion** — what was achieved, screenshots, the
   limitations listed above stated as scoped-out rather than overlooked
9. **Conclusions & Future Work** — real RLS policies, a distributed rate
   limiter, a real payment rail as a "what would productionizing this look
   like" close
10. **Bibliography**
11. **Appendices** — environment setup, full API reference (the running
    `/docs` Swagger UI is generated, not hand-written — cite it as a live
    artifact, not a static reference)

### 3.1 Introduction — illustrative excerpt

> Online banking systems must satisfy a property that most web applications
> never have to consider: a transaction, once recorded, must never silently
> change or disappear. This thesis presents BanK, a web banking application
> built to enforce that property mechanically rather than by convention — a
> single, immutable, double-entry ledger is the only source of truth for
> every balance in the system, and every money-moving code path is required
> to pass through one function that makes the movement atomic,
> concurrency-safe, and idempotent. The system additionally integrates a
> conversational AI layer that can read a user's financial data but is
> structurally prevented from acting on it without explicit human
> confirmation — a constraint imposed not by prompt instructions alone, but
> by the API surface the agent is given access to.

### 3.2 System Architecture — illustrative excerpt

> The ledger's central invariant — that the sum of debits equals the sum of
> credits within every journal entry — is enforced at the point of writing,
> not checked after the fact. `ledger.post_transaction()` opens a single
> database transaction, acquires row-level locks on every account involved,
> re-reads the available balance under that lock, appends the debit and
> credit legs, asserts the invariant, and commits or rolls back the entire
> operation as one unit. No other code path is permitted to write to the
> ledger. This design trades a small amount of throughput for a correctness
> guarantee that can be tested directly: Section 7.2 describes an
> integration test that fires N concurrent transfers against the same
> account and verifies the resulting balance is exactly what double-entry
> arithmetic predicts, rather than trusting the lock to have worked.

### 3.3 Conclusions & Future Work — illustrative excerpt

> BanK demonstrates that the correctness guarantees expected of a real
> banking ledger — atomicity, auditability, and resistance to double-spend
> under concurrency — can be built and tested at the scale of a bachelor's
> thesis without simplifying away the properties that make ledgers hard.
> The system knowingly scopes out what a production deployment would still
> need: row-level security policies enforced by the database rather than by
> application code, a distributed rate limiter, and a real payment rail
> behind the fictitious IBANs. None of these are architectural dead ends —
> each is a bounded extension of a design that was built to accommodate them
> from the start.

## 4. If the author gives you an example thesis

The author may separately upload a real, previously-written thesis (their
own earlier draft, a friend's, or a university template) as a structural
example. If one appears in this conversation:

- **Use it for FORM, never for CONTENT.** Chapter numbering and naming
  conventions, front-matter (title page, declaration of originality,
  abstract placement, table of contents depth), citation style, figure/table
  captioning conventions, section length and register, how formal the
  academic voice runs at that specific institution — all fair game to
  match.
- **Never pull facts, findings, or claims from it into BanK's thesis.** It
  describes a different project. A number, a technology choice, a
  conclusion from that document must never appear in this one, even
  reworded — that isn't a style choice, it's introducing content from
  someone else's work into the author's own thesis, which is exactly the
  kind of thing an originality/plagiarism check exists to catch. If a
  structural choice in the example only makes sense alongside content
  specific to that other project, don't carry the choice over either — copy
  the shape only where the shape stands on its own.
- **When in doubt, ask which chapter of the example is meant to guide which
  chapter of this one** — a template thesis in a different sub-field (e.g.
  a hardware or theoretical thesis) may not map chapter-for-chapter onto a
  software-engineering one, and forcing the fit is worse than asking.

## 5. How to keep this guide current

The canonical copies of this guide and the journal live in the repository:

- `thesis/THESIS_GUIDE.md` (this file)
- `thesis/daily-notes.md`

Whenever the author has made changes to BanK since the last thesis-writing
session, the right move — for the author and for the assistant — is:

1. Re-read `README.md` and `flow.md` for anything architectural that
   changed.
2. Re-read `thesis/daily-notes.md` for the latest entries — new entries are
   appended at the bottom with a date heading.
3. Only then continue drafting or revising thesis text, so it stays
   consistent with both the current code and the author's own account of
   why it changed.

If the assistant is a Claude.ai Project without live repository access, the
author should paste the diff or the relevant new file(s) at the start of
the session rather than describing them from memory — this guide is a
snapshot, not a live feed.
