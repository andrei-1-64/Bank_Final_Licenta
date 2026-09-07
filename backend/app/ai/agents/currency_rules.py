"""Routing rules for currency-conversion questions.

Its own module rather than a block inside one agent's file because two agents
share these rules: BankingAgent and InsightsAgent both hold
`convert_currency` (see `ai/service.py`), so either can answer, and the rules
are registered on both. Defining them in one agent and importing from the
other would work but would imply an ownership that does not exist - and would
add an agent-to-agent import for nothing.

WHY TWO RULES. "Curs", "conversie", "valutar" are unambiguous on their own: no
other agent in this app claims them, and nothing else in a banking product is
described that way. A currency NAME is the opposite - "trimite 100 de euro lui
Andrei" is a transfer, and "câți bani am în euro" is a balance question. Both
must keep reaching BankingAgent's own rules. So the second rule claims a
currency name only alongside an explicit conversion marker ("cât face", "cât
înseamnă", "echivalent"), the same `requires_any_of` two-token mechanism
Planning and Insights already use for `econom` and `cheltui`.

COLLISION CHECK against every stem registered today: `conversie`, `convert`, `curs`,
`valut`, `rata de schimb` and `schimb valutar` appear in no other rule -
Banking's nearest neighbours are `card`/`cont`/`transfer`, Docs owns
`comision`/`tarif`/`cost`, Insights owns `luna`/`analiz`. `schimb` is
deliberately NOT a bare stem: "schimbă limita cardului meu" is a real Banking
request that a bare `schimb` would steal, so only the two-word forms appear.
`lei` is deliberately not a currency name below, for the same reason at
greater volume - it is in half the messages this app will ever see.

These rules go FIRST in both agents' rule tuples. Insights is registered
before Banking, and `insights_time_slice` claims `luna`, so "cursul euro luna
asta" would otherwise be answered as a spending analysis; first-position in
BOTH tuples means the conversion intent wins wherever the message lands, and
both landing spots hold the tool.
"""

from __future__ import annotations

from app.ai.routing import RoutingRule

#: Phrases that turn a mention of a currency into a request to CONVERT into or
#: out of it, rather than to move it or report a balance in it.
CURRENCY_CONVERSION_MARKERS = frozenset(
    {
        "cat face",
        "cat fac",
        "cat inseamna",
        "cat ar fi",
        "cat este in",
        "echivalent",
        "conversie",
        "convert",
        "transform",
        "prefac",
    }
)

CURRENCY_ROUTING_RULES = (
    RoutingRule(
        name="currency_conversion",
        keywords=frozenset(
            {
                # Two stems, not the shorter `conver` that would cover both:
                # `conver` also claims "conversație", and "despre ce am vorbit
                # in conversația asta" is not a currency question.
                "conversie",
                "convert",
                # "curs", "cursul", "cursul valutar". Safe bare: the only other
                # sense of the word ("a course") is not a banking topic.
                "curs",
                # "valuta", "valutar".
                "valut",
                # Two-word only - see the module docstring on `schimb`.
                "rata de schimb",
                "schimb valutar",
            }
        ),
    ),
    RoutingRule(
        name="currency_named",
        keywords=frozenset(
            {
                "euro",
                "eur",
                "dolar",
                "usd",
                "lir",
                "franc",
                "forint",
                "yen",
                "moned",
            }
        ),
        requires_any_of=CURRENCY_CONVERSION_MARKERS,
    ),
)
