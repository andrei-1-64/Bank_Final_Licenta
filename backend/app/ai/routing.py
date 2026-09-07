"""What a routing decision *is* — the types, not the machinery.

Lives apart from `orchestrator.py` for a concrete reason, not taste:
`agents/base.py` declares `routing_rules` as a class attribute, so it must
import `RoutingRule`, and `orchestrator.py` already imports `agents/base.py`.
Putting these types in `orchestrator.py` would close that loop into a circular
import. Keeping them here also separates "what a decision is" from "how routing
is executed", which is what makes the decision safe to persist and return over
HTTP without dragging the orchestrator along.
"""

from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field


def normalise(message: str) -> str:
    """Fold a message down to something keyword matching can rely on.

    Strips diacritics and lowercases, so `tranzacție`, `Tranzactie` and
    `TRANZACȚIE` are all the same string by the time a rule sees them. Romanian
    is routinely typed without diacritics, and a router that only matched the
    correctly-accented spelling would miss most real messages.
    """
    decomposed = unicodedata.normalize("NFKD", message)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return without_marks.lower()


class RoutingRule(BaseModel):
    """A named set of keyword stems that claim a message for an agent.

    `requires_any_of` / `excludes_any_of` (Step 16 Priority 2, item 7) turn a
    plain stem match into a two-token match for the handful of stems two
    agents both claim (`econom`, `cheltui`): the stem alone is not enough,
    it also needs (or must lack) one of a second set of marker words. Empty
    on both — the default, and every non-collision rule in this codebase —
    reproduces the original single-stem behaviour exactly.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    keywords: frozenset[str]
    #: If non-empty, this rule only fires when the message ALSO contains at
    #: least one of these (in addition to a keyword stem) - e.g. Planning
    #: claims "econom" only alongside a forward-looking marker like "vreau"
    #: or "plan".
    requires_any_of: frozenset[str] = frozenset()
    #: If non-empty, this rule is suppressed when the message contains any of
    #: these - the mirror image of `requires_any_of`, for the agent giving
    #: ground on the same collision stem (e.g. Banking backs off "econom"
    #: when a forward-looking marker makes it Planning's instead).
    excludes_any_of: frozenset[str] = frozenset()

    def matched(self, normalised_message: str) -> frozenset[str]:
        """Which of this rule's keywords (plus any matched markers) claim the
        message. Empty = no match.

        Keywords are matched as word PREFIXES (`\\bsold` catches sold, soldul,
        soldurile) because Romanian inflects heavily — exact word matching would
        miss `cardurile mele` while matching `card`.

        KNOWN LIMITATION: prefix matching over-matches. `cont` also fires on
        `contact`, `plat` on `platformă`. That is harmless while BankingAgent is
        the only agent registered — every message routes there regardless — but
        it must be revisited when InsightsAgent registers and a mis-route starts
        costing something. Tighten the stems (or add negative keywords) then,
        rather than guessing at the shape of the problem now.
        """

        def _search(candidates: frozenset[str]) -> frozenset[str]:
            return frozenset(
                candidate
                for candidate in candidates
                if re.search(rf"\b{re.escape(candidate)}", normalised_message)
            )

        stem_matches = _search(self.keywords)
        if not stem_matches:
            return frozenset()

        if self.excludes_any_of and _search(self.excludes_any_of):
            return frozenset()

        if self.requires_any_of:
            marker_matches = _search(self.requires_any_of)
            if not marker_matches:
                return frozenset()
            return stem_matches | marker_matches

        return stem_matches


class RoutingDecision(BaseModel):
    """Which agent handled a message, and why.

    Frozen: a decision is a record of something that already happened, so
    nothing downstream — persistence, the HTTP response, a future UI — may
    quietly rewrite it.

    `reason` is written for a human reading an audit trail. It names the rule
    and the keywords that fired, never the user's message: the transcript
    already holds what they said, and a reason that echoed it would duplicate
    user content into a field nobody expects to be sensitive.
    """

    model_config = ConfigDict(frozen=True)

    agent_name: str
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    matched_rule: str | None = None
    #: Set only on a hop that a PREVIOUS agent handed the turn to (Step 15):
    #: the name of the agent it came from. None means this decision came from
    #: `Orchestrator.route()` - i.e. it is the first hop of a turn, or the
    #: whole turn.
    #:
    #: No migration needed for this field. `messages.routing_metadata` is
    #: JSONB, so an extra key just stores; and because it defaults to None,
    #: every row written before Step 15 reads back as `handoff_from=None`,
    #: which is exactly what those turns were.
    handoff_from: str | None = None
