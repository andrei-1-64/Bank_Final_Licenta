"""What one user turn produced — the types, not the machinery.

Before Step 15 an agent returned `(reply, trace)` and `dispatch` widened that
to `(reply, trace, routing)`, with a TODO saying a fourth element meant it was
time for a model instead. Cross-agent handoff is that fourth element: a turn is
no longer "one agent answered", it is "a chain of one or more agents answered,
in order, sharing one Context".

Two levels, deliberately:

* `TurnResult` is ONE agent's contribution — its reply, its trace, the routing
  decision that sent the turn to it, and (optionally) its request to hand the
  rest of the turn to someone else.
* `TurnDispatchResult` is the whole turn: the ordered list of hops. Persistence
  and the HTTP response both walk it, so the chain a user sees ("→ Analiză →
  Bancar") is the same chain that was actually executed, not a reconstruction.

Lives apart from `orchestrator.py` for the same reason `routing.py` does:
`agents/base.py` must import `TurnResult` to declare `Agent.run`'s return type,
and `orchestrator.py` already imports `agents/base.py`. Putting these here
keeps that from closing into a circular import.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.ai.routing import RoutingDecision
from app.ai.schemas import Message


class HandoffRequest(BaseModel):
    """One agent asking for the rest of this turn to continue on another.

    Frozen: like `RoutingDecision`, this is a record of something the source
    agent decided. Nothing downstream may quietly rewrite where a handoff was
    aimed after the fact.

    EVERY FIELD HERE IS MODEL-AUTHORED AND UNTRUSTED. `target_agent` in
    particular is a name the model produced: `Orchestrator.dispatch` — not this
    type, and not the tool that built it — decides whether that target is
    reachable from the source agent at all (see ALLOWED_HANDOFF_TARGETS). A
    valid `HandoffRequest` means "an agent asked", never "a handoff will
    happen".
    """

    model_config = ConfigDict(frozen=True)

    #: Registered agent name the source wants to continue on. Untrusted.
    target_agent: str
    #: Why, for the audit trail. Ends up in the target hop's RoutingDecision.
    reason: str
    #: Short text the target agent is given as its "user message" for this
    #: turn. PROMPT TEXT ONLY — it never contributes to identity, account
    #: access, or authorisation anywhere (see HandoffToAgentTool.execute).
    context_hint: str
    #: The source agent's own answer to the part of a compound question it
    #: could cover, to be shown to the user as that hop's reply. Empty for an
    #: ordinary handoff, where the source has nothing to say and the target's
    #: reply is the whole answer. See HandoffToAgentTool for why this rides
    #: here instead of being text the model emits alongside the tool call —
    #: `ModelResponse` structurally forbids that. Defaults to "" so every
    #: handoff written before this field existed still validates.
    answer_so_far: str = ""


class TurnResult(BaseModel):
    """One agent's contribution to a turn.

    NOT frozen, unlike `HandoffRequest` and `RoutingDecision`: an agent builds
    this without knowing why it was routed to, so `dispatch` stamps `routing`
    on afterwards. That incremental build is the only intended mutation — treat
    a `TurnResult` that has left `dispatch` as read-only.
    """

    #: The agent's final text. Empty is legitimate for a hop that handed off
    #: before writing anything: the TARGET agent's reply is what the user sees
    #: for that leg of the chain.
    reply: str = ""
    #: Every message the agent generated getting there - assistant tool-call
    #: turns and their tool-result turns, in order, never its system prompt.
    trace: list[Message] = Field(default_factory=list)
    #: Which agent ran and why. None as the agent returns it; `dispatch` fills
    #: it in, because routing is the orchestrator's knowledge, not the agent's.
    routing: RoutingDecision | None = None
    #: Set only when this agent asked to hand the turn on. None = it finished.
    handoff: HandoffRequest | None = None


class TurnDispatchResult(BaseModel):
    """A whole turn: every hop that ran, in execution order.

    Always at least one hop. A single-agent turn (the overwhelmingly common
    case) is a one-element chain, so callers have exactly one shape to handle
    rather than a special case for "no handoff happened".
    """

    hops: list[TurnResult] = Field(min_length=1)

    @property
    def routing_chain(self) -> list[RoutingDecision]:
        """The decisions that produced this turn, in order.

        `dispatch` stamps `routing` on every hop before returning, so the
        filter below only ever drops a hop built by hand in a test.
        """
        return [hop.routing for hop in self.hops if hop.routing is not None]

    @property
    def final_reply(self) -> str:
        """The single reply the user sees live.

        The LAST hop's text, since that agent is the one that finished the
        turn. It falls back to the last hop that said anything at all so a
        chain whose final agent produced only a tool result (and then hit its
        iteration cap) still shows the user something rather than a blank
        bubble.
        """
        if self.hops[-1].reply:
            return self.hops[-1].reply
        for hop in reversed(self.hops):
            if hop.reply:
                return hop.reply
        return ""

    @property
    def combined_reply(self) -> str:
        """EVERY hop's reply, in order - what the user should actually read.

        `final_reply` above answers "which hop finished the turn", and for a
        handoff whose source said nothing (the Step 15 demo: Insights calls
        `handoff_to_agent` and emits no text) the two are byte-identical.
        They diverge exactly when more than one hop spoke, which is what the
        compound-question handoff introduced: Insights answers the spending
        half, hands off, and Banking answers the balance half. `final_reply`
        would show the user only the balance and silently drop the spending
        answer that was computed, paid for, and already persisted - the live
        reply would disagree with the same turn re-read from history after a
        reload (see _persist_turn in chat/router.py, which stores every hop).

        Kept as a SEPARATE property rather than a redefinition of
        `final_reply`, which stays exactly what it was - "the hop that
        finished the turn". That question still has callers and tests of its
        own (the hop-cap and cycle-guard chains assert on it directly, where
        last-hop-wins is the behaviour being described), and quietly widening
        it would have changed what those mean rather than adding something
        new. `chat/router.py` is the caller that wants every hop; it asks for
        it by name.

        Empty hops are skipped, so a chain that produced one answer reads as
        that one answer with no blank padding around it.
        """
        return "\n\n".join(hop.reply for hop in self.hops if hop.reply)

    @property
    def new_messages(self) -> list[Message]:
        """Every message this turn added, flattened, in order.

        Each hop contributes its trace and then its final assistant message —
        including when that message is empty, because the routing row rides on
        it and the chain is reconstructed from those rows on replay (see
        chat/router.py). Callers that need the hops kept apart (per-hop
        persistence) walk `hops` instead; this is for callers that only want
        the transcript, like the CLI.
        """
        return [
            message
            for hop in self.hops
            for message in (*hop.trace, Message(role="assistant", content=hop.reply))
        ]
