"""app/ai/language_directive.py - the mechanism that makes an agent answer in
the caller's UI language without translating its (Romanian) instructions.
"""

from __future__ import annotations

from app.ai.language_directive import LANGUAGE_NAMES, language_directive


def test_romanian_gets_no_directive():
    """The default/native language of every prompt - appending nothing here
    is what keeps every caller who never touches the language switcher
    byte-identical to before this module existed."""
    assert language_directive("ro") == ""


def test_unrecognized_language_gets_no_directive():
    """Degrades to "reply in Romanian" (i.e. does nothing), never raises or
    produces a broken prompt over a locale typo."""
    assert language_directive("xx") == ""
    assert language_directive("") == ""


def test_known_language_names_all_produce_a_directive_naming_that_language():
    for code, name in LANGUAGE_NAMES.items():
        directive = language_directive(code)
        assert directive != ""
        assert name in directive


def test_directive_is_appended_content_not_a_replacement():
    """Callers append this to their own system prompt string - it must read
    as a standalone addition, not assume it opens the message."""
    directive = language_directive("en")
    assert directive.startswith("\n\n")
