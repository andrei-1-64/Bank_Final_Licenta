"""Makes an agent's reply LANGUAGE follow the caller's UI language, without
translating the agent's own (carefully-tuned, tool-calling-critical)
instruction content out of Romanian.

Every agent's SYSTEM_PROMPT (see app/ai/agents/*.py) is a long, Romanian
document of reasoning/tool-selection instructions - rewriting all of them
into 9 languages would be a large, ongoing maintenance burden and a real
risk to tool-calling quality, for a part of the prompt the user never sees
anyway. What the user DOES see is the final reply text, so that is the only
thing this module touches: `language_directive` returns one short,
high-priority sentence to append AFTER the agent's own prompt, telling the
model which language to answer the human in. Appended last, not first,
because a model resolves a same-topic contradiction in favor of the most
recent instruction more reliably than one buried at the top of a long
prompt - and the agent's own "Răspunzi mereu în limba română" line, further
up, is exactly that kind of contradiction for any non-Romanian caller.
"""

from __future__ import annotations

#: Every code frontend/language.js's LANGUAGES map offers, each spelled out
#: in Romanian (the language every directive sentence itself is written in -
#: only the requested OUTPUT language changes, not the instruction's own).
LANGUAGE_NAMES: dict[str, str] = {
    "en": "engleză",
    "fr": "franceză",
    "de": "germană",
    "es": "spaniolă",
    "it": "italiană",
    "hu": "maghiară",
    "tr": "turcă",
    "uk": "ucraineană",
}

#: The language every agent's prompt is natively written to reply in - a
#: no-op language, so callers who never touch the language switcher (still
#: the overwhelming majority) see byte-identical prompts to before this
#: module existed.
DEFAULT_LANGUAGE = "ro"


def language_directive(language: str) -> str:
    """Empty string for "ro" or any code this module doesn't recognize (an
    unfamiliar code degrades to "reply in Romanian", never to an error).
    Otherwise a short paragraph instructing the model to answer in that
    language regardless of the prompt's own language, appended to the
    agent's system prompt by its caller."""
    if language == DEFAULT_LANGUAGE:
        return ""
    name = LANGUAGE_NAMES.get(language)
    if name is None:
        return ""
    return (
        "\n\n=== SUPRASCRIE instrucțiunea de limbă de mai sus ===\n"
        f"Utilizatorul a ales limba {name} pentru interfață. Indiferent de "
        "limba folosită în restul acestor instrucțiuni, TREBUIE să răspunzi "
        f"utilizatorului STRICT în {name}, în fiecare mesaj, fără nicio "
        "excepție - inclusiv confirmări, rezumate de propuneri și mesaje de "
        "eroare. Termenii tehnici fără traducere naturală (IBAN, RON, CVV) "
        "pot rămâne neschimbați."
    )
