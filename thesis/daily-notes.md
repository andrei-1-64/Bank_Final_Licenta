# Daily notes — BanK / thesis journal

Personal working journal, first person, written as things happened. This is
the voice reference: casual, technical, honest about what was confusing or
annoying. **Not** the thesis register — see `THESIS_GUIDE.md` §1 before
turning any of this into thesis text.

New entries get appended at the bottom with a `## YYYY-MM-DD` heading.

---

## 2026-08-20

Decided to build the thesis around BanK instead of starting something new
from scratch. It already does more than I could realistically build solo in
the time I have left: real double-entry ledger, cards, scheduled transfers,
an AI chat layer with five agents, e-Sign, OCR onboarding. My job now is to
understand every piece well enough to explain it, extend the parts that are
still rough, and be honest in the thesis about what's a demo shortcut versus
what's actually solid engineering.

Spent today just reading. `README.md` and `flow.md` first, then the
directory tree. The non-negotiable rules in `flow.md` (no floats for money,
one money-writer, idempotency everywhere, AI is read/propose-only) read like
they were written by someone who'd been burned before. Good rules to build a
thesis argument around — they're specific enough to actually test.

## 2026-08-23

First real look at `ledger/post_transaction()`. It's short, which is the
opposite of what I expected — I think I assumed "atomic double-entry ledger"
would be a few hundred lines. It's one DB transaction, row locks on every
account involved, re-check the balance under the lock, write both legs,
assert debits equal credits, commit or roll back. All the complexity is in
what it *refuses* to let happen, not in what it does.

Found the concurrency test in `tests/integration` that fires transfers at
the same account at the same time and checks the final balance is exactly
right. That test is going to be the centerpiece of my "Testing & Validation"
chapter — it's the one piece of evidence that actually proves the atomicity
claim instead of just asserting it.

## 2026-08-27

Today I finally understood *why* the backend talks to Supabase only over
REST instead of a direct Postgres connection — it's not a style preference,
it's because the network this was originally built on blocks outbound
Postgres ports and only allows 443. That's a real constraint I hadn't
considered as a "design decision" before, but it clearly shaped the whole
data layer (no ORM, no Alembic, hand-applied SQL migrations through the
Supabase SQL editor). Worth its own paragraph in the architecture chapter —
it explains a lot of choices that would otherwise look unusual.

## 2026-08-31

Went through the AI layer today. Five agents (banking, insights, planning,
documents, docs/RAG), each with its own system prompt and tool registry, a
router in front deciding who handles what. The part I actually care about
for the thesis is the trust boundary: the agent can call read tools freely,
but a write tool stops the loop and returns a *proposal* instead of
executing anything. The user still has to confirm through the normal
endpoint, with a password or Face ID, exactly like they would without the
AI in the picture at all.

That's a nice thing to write about — it's not "the AI is instructed to be
careful," it's "the AI physically doesn't have a tool that moves money
without a human in the loop." Structural constraint beats a polite prompt.
Want to frame the related-work section around that distinction.

## 2026-09-03

Spent the afternoon on cards — issuance, freeze/unfreeze, spending limits,
and the card_orders flow with delivery tracking (ordered, shipped,
delivered). It's a small state machine but a clean one. Took a few
screenshots of the card management screen and the order tracking timeline
for the Results chapter — need more of these as I go, screenshots are much
easier to grab now than to reconstruct later from memory.

## 2026-09-06

Face ID + OCR onboarding today, via the vision microservice (dlib for
embeddings, tesseract for OCR on the ID card and IBAN). It works, and it's
genuinely satisfying to onboard with a photo instead of typing an IBAN by
hand. But there's no liveness detection — a photo of a photo gets through.
Writing that down now so it doesn't get lost: this needs to be stated
plainly in the limitations section, not glossed over. It's a demo-grade
biometric factor, not a security boundary, and pretending otherwise in the
thesis would undercut the parts that actually are solid.

## 2026-09-09

Read through e-Sign today — Ed25519 detached signatures over uploaded
documents, OTP+Face step-up required specifically for admin-issued
documents (not for a user's own uploads, which makes sense: nobody needs to
prove their identity to themselves). Started a running list of every
"security decision" in the app so I have raw material for a checklist in
the Testing & Validation chapter instead of writing that section from
memory later.

## 2026-09-11

Ran the whole test suite end to end for the first time — unit, integration
against the disposable test Supabase project, and the offline AI tests.
Watching the concurrent-transfer test actually pass, rather than just
reading it, made it click in a way reading the code hadn't. I think that
test result — a screenshot or the raw pytest output — is going to be the
single most convincing exhibit in the whole thesis. Everything else is "we
designed it this way"; this is "and here's proof it holds under load."

## 2026-09-13

Spent today replacing the OTP delivery mechanism. It's been going out over
a Microsoft Teams webhook since the original build — fine as a "pretend
this reaches the user" convention for a demo, explicitly called out as such
in the README, but not something I want to still be true by the time I'm
writing the Results chapter. Swapped it for real Gmail SMTP: a new
`app/core/email.py` with the same fail-closed, never-raise contract the
Teams sender had (`send_otp_email(to, subject, body) -> bool`), wired into
the three places that actually send OTPs — password reset, trusted-device
enrollment, and document-signing codes.

Also had to run down every "check Teams" and "cod trimis prin Teams" string
across all nine frontend language files plus a few code comments that
referenced the old flow by name — easy to miss half of them if you only
grep for the function name and not the user-facing copy too.

Before this, the app was up and running locally (had to start Docker
Desktop and rebuild — dlib takes a while to compile from source every
time), but hit a wall I didn't expect: the Supabase project in `.env`
doesn't resolve in DNS at all, not paused, just gone. Login 500s with a
connection error. That's now a blocking item before I can actually click
through the app myself — need to either revive that project or point at a
fresh one. Worth a line in the thesis's "environment" appendix too: a
demo project silently disappearing is a real operational risk for anyone
trying to reproduce this work later, and free-tier hosted Postgres is
exactly the kind of dependency that does that.

Good, concrete entry to end the week on: real bug, real fix, real
before/after. This is the kind of thing I want more of in this journal —
specific enough that future-me (or the thesis) can quote the actual change
instead of a vague "improved security."

---

**Next up:** get the Supabase project reachable again, actually exercise
the new email OTP flow end to end (trigger a reset, watch the email land),
then start turning the last few weeks of entries above into the
Implementation chapter draft.
