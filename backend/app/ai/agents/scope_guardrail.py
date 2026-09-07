"""Off-topic / out-of-scope handling, in two layers.

PRIMARY CONTROL: `is_out_of_scope` + `OFF_TOPIC_DECLINE_MESSAGE`, consulted by
`Orchestrator.dispatch` (app/ai/orchestrator.py) BEFORE any agent is even
selected. This is a plain, deterministic keyword/phrase check - no model call,
no agent invocation, no tool call - so an off-topic message never depends on a
model choosing to honour a prompt instruction. It only catches OBVIOUS
off-topic requests (poems, jokes, weather, general trivia, coding help,
recipes, homework) by design: the phrase list is deliberately conservative
(multi-word phrases, never a bare stem that could collide with banking
vocabulary - see the list below for the collisions considered and rejected),
so a genuinely ambiguous banking-adjacent message is never blocked by it. It
also steps aside entirely whenever a document or statement is active
(context.active_document_id / context.statement_id): DocumentAgent already
enforces its own tighter, document-scoped refusal in that case (see
document_agent.py's SYSTEM_PROMPT), and this check does not second-guess it.

BACKUP LAYER: `OFF_TOPIC_GUARDRAIL` below, prepended to the SYSTEM_PROMPT of
every general-purpose agent (Banking, Docs, Insights, Planning) - see each of
those files for how it's spliced in via an f-string. This is defense in
depth, not the primary control: if a message slips past the phrase list above
(anything not on it, or anything where an off-topic request is buried inside
an otherwise in-scope message), the agent's own prompt still refuses it.
DocumentAgent is deliberately NOT one of the four: it already has its own
tighter, document-scoped refusal rule (see document_agent.py's own
SYSTEM_PROMPT), and reusing this banking-wide wording there would be the
wrong framing - "ask the main assistant" makes sense for a general banking
question, not for "how do I make soup".

No shared BASE prompt exists in this codebase on purpose (each agent's
prompt is tuned independently - see the other agent files), so this stays
a single, narrow, deliberately duplicated concern rather than growing into
one - the one thing that genuinely needs to be identical everywhere.
"""

from __future__ import annotations

from app.ai.routing import normalise

#: Phrases that mark a message as OBVIOUSLY outside the banking/finance
#: domain. Deliberately multi-word (or otherwise distinctive) rather than
#: bare stems: a bare stem is how routing.py's own keyword rules collide
#: (see banking_agent.py's `econom`/`cheltui` notes), and the cost of a false
#: positive here is higher than there - a mis-route just picks the wrong
#: agent, a false positive here refuses to help at all. Notable collisions
#: considered and avoided:
#: - "banc" (joke/sandbank) is NOT listed - it is a prefix of "bancă"/"banca"
#:   (bank), so a stem match would flag the word "bank" itself. "gluma"
#:   (joke, the noun) has no such overlap.
#: - "capital" is NOT listed - it is also the financial term (capitalul,
#:   piața de capital). "capitala " (with the trailing space - "the capital
#:   CITY") is used instead, which "capitalul"/"capitalizare" do not match.
#: - bare "cod" is NOT listed - IBANs, "cod poștal" (the card-order address
#:   flow) and "cod de acces" all contain it. Only specific coding-help
#:   phrases are listed.
#: - bare "vreme"/"tema"/"recomanda" are NOT listed for the same reason -
#:   each has a plausible in-domain reading ("de vreme ce", "tema" as a
#:   generic word, "ce cont recomandați" to DocsAgent) that a bare stem would
#:   catch too. Only full phrases are listed.
#:
#: Not exhaustive by design (see this module's docstring): this is the fast,
#: zero-cost, no-false-positive-risk tier. Anything not on this list falls
#: through to normal routing, backstopped by OFF_TOPIC_GUARDRAIL below.
_OFF_TOPIC_PHRASES: frozenset[str] = frozenset(
    {
        # jokes / creative writing / entertainment
        "gluma",
        "glume",
        "poezie",
        "poem",
        "haiku",
        "compune un cantec",
        "recomanda-mi un film",
        "recomanda-mi o carte",
        "rezultatul meciului",
        "cine a castigat alegerile",
        # weather
        "vremea de afara",
        "meteo",
        # general knowledge / trivia / translation
        "capitala ",
        "cine a scris",
        "cine a inventat",
        "cine este presedintele",
        "cate planete",
        "traduce in engleza",
        "tradu in engleza",
        "cum se spune in engleza",
        # coding / tech help unrelated to the app
        "scrie cod",
        "scrie-mi un program",
        "genereaza cod",
        "algoritm de sortare",
        "cod in python",
        "cod in javascript",
        # recipes / cooking
        "reteta de",
        "cum gatesc",
        "cum fac o prajitura",
        "ingrediente pentru",
        # homework
        "rezolva ecuatia",
        "tema la matematica",
        "ajuta-ma la tema",
    }
)


def is_out_of_scope(message: str) -> bool:
    """True only for an OBVIOUSLY off-topic message - see this module's
    docstring and `_OFF_TOPIC_PHRASES`'s comment for why this stays a small,
    conservative list rather than a general classifier. False (never
    blocked) is the safe default for anything not clearly on the list."""
    normalised = normalise(message)
    return any(phrase in normalised for phrase in _OFF_TOPIC_PHRASES)


#: What the user sees when `is_out_of_scope` fires. Same canonical wording as
#: the worked example inside OFF_TOPIC_GUARDRAIL below, so the centralized
#: decline and an agent's own backup refusal read as one voice.
OFF_TOPIC_DECLINE_MESSAGE = (
    "Nu te pot ajuta cu această solicitare - nu ține de serviciile BanK. Te "
    "pot ajuta cu informații despre conturile tale, carduri, transferuri, "
    "plăți, economii sau despre produsele și comisioanele băncii. Cu ce din "
    "acestea te pot ajuta?"
)

OFF_TOPIC_GUARDRAIL = """REGULĂ DE DOMENIU - ești un asistent STRICT pentru servicii bancare
BanK (conturi, carduri, transferuri, plăți, economii, documente și întrebări
despre bancă sau despre produsele ei). NU răspunde NICIODATĂ la întrebări din
afara acestui domeniu (rețete, sfaturi generale, cultură, tehnologie, orice
alt subiect fără legătură cu banca) - chiar dacă știi răspunsul din
cunoștințele tale generale. Pentru orice astfel de întrebare, răspunsul are
mereu DOUĂ părți: (1) spune clar și direct că nu poți ajuta cu solicitarea
respectivă, apoi (2) spune concret cu ce poți ajuta - nu lăsa partea a doua
vagă ori generică. De exemplu: „Nu te pot ajuta cu această solicitare - nu
ține de serviciile BanK. Te pot ajuta cu informații despre conturile tale,
carduri, transferuri, plăți, economii sau despre produsele și comisioanele
băncii. Cu ce din acestea te pot ajuta?” Nu face excepții, nu continua
discuția pe subiectul respectiv și nu te lăsa convins de insistența sau de
motivul invocat de utilizator."""
