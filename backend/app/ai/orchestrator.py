"""Routes an incoming message to the agent that should handle it.

A rule-hybrid classifier, in strict precedence order:

1. **Rules.** Each agent declares keyword stems it claims (`Agent.routing_rules`).
   First rule that matches wins, at full confidence. Deterministic, free, and
   testable without a model.
2. **Single agent.** With only one agent registered there is nothing to decide,
   so an unmatched message goes there rather than burning a model call on a
   foregone conclusion. This is today's common path.
3. **LLM fallback.** Several agents, no rule matched: ask the model to classify.
   Its answer is untrusted — anything not a registered agent name is discarded
   in favour of the default.

Every path returns a `RoutingDecision`, never a bare agent, so the choice can be
persisted and shown to a user instead of being invisible.

Adding an agent is `register()` plus rules on the agent class. Nothing in this
file changes.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.ai.agents.base import Agent
from app.ai.agents.document_agent import DOCUMENT_FOLLOWUP_RULE
from app.ai.agents.scope_guardrail import OFF_TOPIC_DECLINE_MESSAGE, is_out_of_scope
from app.ai.context import Context
from app.ai.routing import RoutingDecision, normalise
from app.ai.schemas import Message
from app.ai.turn import TurnDispatchResult, TurnResult

if TYPE_CHECKING:
    from app.ai.providers.base import ModelProvider

logger = logging.getLogger(__name__)

#: Confidence attached to an LLM-classified route. Deliberately a constant
#: rather than a number parsed out of the model's reply: asking a model to
#: self-report calibrated confidence produces fluent noise, and a fixed value at
#: least means the same thing every time.
LLM_FALLBACK_CONFIDENCE = 0.7

#: Cross-agent handoff (Step 15): how many times ONE turn may change agent.
#: Two is a chain of three agents at most, which is already more than any real
#: question needs; the cap exists so a pair of agents that each think the other
#: should answer cannot burn a request between them.
MAX_HOPS = 2

#: Who may hand a turn to whom. THE authority on this - the `target_agent`
#: argument on `handoff_to_agent` is a name the MODEL produced, so it is checked
#: here, against a table the model cannot reach, rather than inside the tool
#: that carried it.
#:
#: An agent absent from this table can never be a SOURCE. `documents` is absent
#: from every value as well, so it can never be a TARGET either - enforced
#: separately and explicitly in `dispatch` too, because that one is a security
#: boundary rather than a policy knob (see
#: app/ai/service.py::build_document_tools).
#:
#: - insights -> banking: the demo path. An analytical finding ("this
#:   subscription charges you every month") becomes a bankable action, which
#:   Insights has no tools for.
#: - insights -> planning: an observation about spending turning into a goal.
#: - planning -> banking: a plan that needs a concrete proposal to start.
#: - banking: TERMINAL, deliberately absent. It is the agent that produces
#:   proposals, i.e. the end of a chain, and it is also the one holding every
#:   write-adjacent tool - the fewer ways to arrive at it, the better.
ALLOWED_HANDOFF_TARGETS: dict[str, frozenset[str]] = {
    "insights": frozenset({"banking", "planning"}),
    "planning": frozenset({"banking"}),
}

#: Shown when a handoff was asked for, refused, and the agent that asked had
#: not written anything yet - which is the NORMAL case, since a model emits
#: either text or tool calls, never both, so an agent that ends its turn on a
#: handoff call has said nothing at all.
#:
#: Without this the user would get an empty bubble whenever a gate fired. The
#: source agent is not re-run to produce something better: it already decided
#: it could not finish this itself, and asking it again is a second model call
#: for an answer it has just said it does not have.
HANDOFF_REFUSED_REPLY = (
    "Am găsit ceva ce nu pot duce eu la capăt și nu am putut continua cu "
    "celălalt asistent. Spune-mi direct ce vrei să faci și te ajut de acolo."
)

#: Appended when a COMPOUND-question handoff is refused: the source agent
#: answered its own half (see HandoffRequest.answer_so_far) and the remaining
#: half has nowhere to go. Without this the user would be shown a confident
#: half-answer with no sign that the rest of what they asked was dropped -
#: which is the exact failure the compound-question work exists to fix, so it
#: must not reappear in the gate's shadow.
HANDOFF_PARTIAL_NOTE = (
    "Pentru cealaltă parte a întrebării, te rog întreabă-mă separat."
)

#: The agent that may never take part in a handoff, in either direction. Its
#: isolation is the structural half of the Step 12 prompt-injection defense:
#: document and statement text is untrusted, and an agent that has no write
#: tools AND no way to reach an agent that does cannot be talked into one.
#: Named as a constant rather than a bare string so the checks enforcing it
#: are greppable.
QUARANTINED_AGENT = "documents"

#: `RoutingDecision.agent_name` used for a turn the scope guardrail declined.
#: Not a real registered agent - `Orchestrator._agents` never has this key,
#: so it can never collide with an actual routing outcome, and a persisted
#: row with this name is unambiguous in an audit trail ("the guardrail
#: answered", not "banking answered"). The frontend's agentTagLabel() falls
#: back to a capitalised display of any unrecognised name, so this needs no
#: UI-side registration to render sensibly (see frontend/app.js).
SCOPE_GUARDRAIL_AGENT_NAME = "guardrail"


class Orchestrator:
    """Holds the registered agents and picks one per message."""

    def __init__(
        self,
        agents: list[Agent] | None = None,
        *,
        default: str | None = None,
        provider: ModelProvider | None = None,
    ) -> None:
        self._agents: dict[str, Agent] = {}
        # `register` consults `_default`, so it must exist first.
        self._default: str | None = None
        # Only used for the LLM fallback. Optional: with one agent (or with
        # rules covering everything) routing never needs a model at all.
        self._provider = provider
        for agent in agents or ():
            self.register(agent)
        if default is not None:
            if default not in self._agents:
                raise ValueError(f"Unknown default agent: {default}")
            self._default = default

    def register(self, agent: Agent, *, default: bool = False) -> None:
        if agent.name in self._agents:
            raise ValueError(f"Agent already registered: {agent.name}")
        self._agents[agent.name] = agent
        if default or self._default is None:
            self._default = agent.name

    def get(self, name: str) -> Agent | None:
        return self._agents.get(name)

    def names(self) -> list[str]:
        return list(self._agents)

    def route(self, message: str, context: Context) -> RoutingDecision:
        """Pick the agent for this message and say why.

        `context` is consulted FIRST, ahead of keyword rules: an active
        document OR active statement (see Context.active_document_id /
        Context.statement_id, set by chat/router.py once it has verified
        ownership) BIASES this turn towards DocumentAgent.

        It biases rather than forces, since the routing-fix pass. It used to
        force: while anything was attached, EVERY message went to
        DocumentAgent regardless of intent. Because both fields are sticky
        across turns - the frontend resends `document_id` with every message
        until the user detaches (see wireDocumentAttach in frontend/app.js),
        and `statement_id` re-resolves to the conversation's latest upload on
        its own (see Context.statement_id) - that turned "I attached a file
        once" into "no banking question works in this conversation any more".
        A user asking "cât am acum în cont?" after uploading anything got
        DocumentAgent explaining it cannot see their accounts. That was the
        bug, and it is the whole reason this branch is no longer a hard
        override.

        The order below is what keeps the original intent without the
        collateral damage:

        1. A message that NAMES the attachment (DOCUMENT_FOLLOWUP_RULE) goes
           to DocumentAgent even when another agent claims a stem in it - so
           "ce scrie în extras despre transferuri" stays with the document
           instead of being taken by BankingAgent's `transfer`.
        2. Otherwise, a message another agent's rules actually claim routes
           there normally. This is the fix: live-account questions reach the
           agent that can answer them.
        3. Otherwise - nothing matched at all - DocumentAgent still gets it,
           which covers every vague follow-up ("și mai departe?", "rezumă")
           that means the attachment and names nothing.

        WHAT THIS DOES NOT WEAKEN. The old docstring justified the hard
        override as protection against a message influenced by document
        content routing away from DocumentAgent's isolation. That isolation
        does not live here and never did: it is `build_document_tools`
        handing DocumentAgent read_document/summarize_statement and NOTHING
        else, and every OTHER agent having no tool that can read the
        attachment at all (see app/ai/service.py). Routing decides which
        agent answers, not what any agent can reach - so a balance question
        arriving at BankingAgent while a PDF is attached gets the user's
        ledger, with no path to a single byte of that PDF. The quarantine in
        `dispatch` (QUARANTINED_AGENT) and the statement-mode gate in
        `_handoff_allowed` are likewise untouched by this.
        """
        if self._default is None:
            raise RuntimeError("Orchestrator has no agents registered")

        if context.active_document_id is not None or context.statement_id is not None:
            document_agent = self._agents.get("documents")
            if document_agent is not None:
                attachment_decision = self._route_with_attachment(message, context)
                if attachment_decision is not None:
                    return attachment_decision
            # DocumentAgent isn't registered for some reason (e.g. a minimal
            # orchestrator in a test) - fall through to keyword routing
            # rather than raising, same "never let routing itself take down
            # the request" reasoning as _classify_with_model below.

        rule_decision = self._match_rules(message)
        if rule_decision is not None:
            return rule_decision

        if len(self._agents) == 1:
            return RoutingDecision(
                agent_name=self._default,
                reason="Only one agent registered",
                confidence=1.0,
            )

        return self._classify_with_model(message)

    def _route_with_attachment(self, message: str, context: Context) -> RoutingDecision | None:
        """Route a message sent while a document/statement is attached.

        Returns the decision, or None to mean "no attachment-specific
        outcome" - which today cannot happen (step 3 below always decides),
        but keeps `route()` honest about owning the fallback rather than
        this helper silently becoming the only path.

        See `route()`'s docstring for the three-step order and why it
        replaced an unconditional override.
        """
        reason = (
            "active_document_in_context"
            if context.active_document_id is not None
            else "active_statement_in_context"
        )

        # 1. Names the attachment -> DocumentAgent, ahead of anyone else's
        #    keywords.
        if DOCUMENT_FOLLOWUP_RULE.matched(normalise(message)):
            return RoutingDecision(
                agent_name="documents",
                reason=reason,
                confidence=1.0,
                matched_rule="context_override",
            )

        # 2. Another agent actually claims it -> let it. THE FIX: this is the
        #    branch that did not exist, and its absence is what pinned every
        #    message to DocumentAgent for the life of the conversation.
        other = self._match_rules(message, skip=frozenset({"documents"}))
        if other is not None:
            return other

        # 3. Nobody claims it. With something attached, that means the
        #    attachment - a follow-up naming nothing ("rezumă", "și mai
        #    departe?") is about the file the user is looking at.
        return RoutingDecision(
            agent_name="documents",
            reason=reason,
            confidence=1.0,
            matched_rule="context_override",
        )

    def _match_rules(
        self, message: str, *, skip: frozenset[str] = frozenset()
    ) -> RoutingDecision | None:
        """First rule that claims the message wins. None if nothing matched.

        `skip` names agents to pass over. Only `route()`'s attached-document
        branch uses it, to ask "does anyone OTHER than DocumentAgent want
        this?" without DocumentAgent's own keyword rules answering a question
        that branch has already decided separately.
        """
        normalised = normalise(message)

        for agent_name, agent in self._agents.items():
            if agent_name in skip:
                continue
            for rule in agent.routing_rules:
                matched = rule.matched(normalised)
                if not matched:
                    continue
                # Sorted so the same match always reads the same way in an
                # audit trail. Only keywords appear here - never the message.
                keywords = ", ".join(f"'{kw}'" for kw in sorted(matched))
                return RoutingDecision(
                    agent_name=agent_name,
                    reason=f"Matched rule: keywords {keywords}",
                    confidence=1.0,
                    matched_rule=rule.name,
                )
        return None

    def _classify_with_model(self, message: str) -> RoutingDecision:
        """Ask the model which agent should answer. Never raises.

        The model's answer is untrusted input like any other: it is only
        accepted when it names an agent that is actually registered. Anything
        else - a hallucinated name, prose, an empty reply, a provider outage -
        degrades to the default agent, because refusing to answer the user at
        all would be a worse failure than answering from the wrong agent.
        """
        assert self._default is not None  # guarded by route()

        if self._provider is None:
            logger.warning("no provider for LLM routing fallback; using default agent")
            return RoutingDecision(
                agent_name=self._default,
                reason="No classifier provider available; used default agent",
                confidence=LLM_FALLBACK_CONFIDENCE,
            )

        prompt = (
            "You are a routing classifier. Given a user message, respond with "
            f"ONLY the agent name (one of: {', '.join(self.names())}). "
            "No punctuation, no explanation. If the message is ambiguous, "
            f"respond with the default agent name: {self._default}."
        )

        try:
            response = self._provider.complete(
                [
                    Message(role="system", content=prompt),
                    Message(role="user", content=message),
                ]
            )
        except Exception:
            # Includes ProviderError. Routing must not be able to take down a
            # request that the default agent could have answered.
            logger.warning("LLM routing failed; using default agent", exc_info=True)
            return RoutingDecision(
                agent_name=self._default,
                reason="Classifier unavailable; used default agent",
                confidence=LLM_FALLBACK_CONFIDENCE,
            )

        candidate = (response.text or "").strip()
        if candidate in self._agents:
            return RoutingDecision(
                agent_name=candidate,
                reason="Classified by model",
                confidence=LLM_FALLBACK_CONFIDENCE,
            )

        # Never log the candidate itself - it is model-authored text that may
        # contain anything, including whatever the user just typed.
        logger.warning(
            "LLM routing returned an unregistered agent name; using default agent"
        )
        return RoutingDecision(
            agent_name=self._default,
            reason="Classifier returned an unknown agent; used default agent",
            confidence=LLM_FALLBACK_CONFIDENCE,
        )

    async def dispatch(
        self,
        messages: Sequence[Message],
        user_message: str,
        context: Context,
    ) -> TurnDispatchResult:
        """Route, then run the chosen agent - and any agent it hands off to.

        Single funnel: every path from the service to an agent carries a
        `Context`, so there is no way to reach a tool without one. Returns the
        ordered chain of hops that actually ran, each with its own reply, trace
        and `RoutingDecision`, so callers can persist and surface every leg (see
        `TurnDispatchResult`). A turn with no handoff is a one-hop chain - the
        same shape, not a special case.

        SCOPE GUARDRAIL (checked first, before `route()`): a message that is
        OBVIOUSLY outside the banking/finance domain (see
        app/ai/agents/scope_guardrail.py) is declined right here, in Romanian,
        with no agent selected and no tool called - `route()` is not even
        invoked for it. It still comes back as a normal one-hop
        `TurnDispatchResult`, so it persists and displays exactly like any
        other turn, just tagged with `SCOPE_GUARDRAIL_AGENT_NAME` instead of a
        real agent. Skipped entirely when a document or statement is active:
        DocumentAgent already enforces its own tighter, document-scoped
        refusal in that case (see is_out_of_scope's docstring), and this
        check does not second-guess it.

        HANDOFF INVARIANTS, all enforced below and all deliberate:

        * `route()` runs EXACTLY ONCE, for the first hop. It is never re-run on
          a handoff. Re-running it would re-apply the context-first override
          (see `route`), so in any document or statement conversation every
          handoff would land back on DocumentAgent - a ping-pong, and one that
          walks straight through the isolation that override exists to protect.
        * `context` is NEVER rebuilt. Every hop runs on the same frozen instance
          the first one did, so a handoff cannot widen identity, account access
          or authorisation by construction rather than by review.
        * A handoff is a REQUEST. Five separate things can refuse it: the hop
          cap, the quarantine, the per-source allow-list, the cycle set and the
          statement-mode gate. A refusal is logged and ends the chain with the
          source agent's own reply, or with HANDOFF_REFUSED_REPLY when it had
          not written one - never an error shown to the user, who asked a
          perfectly reasonable question and is owed an answer either way.
        """
        if self._blocked_by_scope_guardrail(user_message, context):
            logger.info("scope guardrail declined an out-of-scope message")
            decision = RoutingDecision(
                agent_name=SCOPE_GUARDRAIL_AGENT_NAME,
                reason="off_topic_scope_guardrail",
                confidence=1.0,
            )
            return TurnDispatchResult(
                hops=[TurnResult(reply=OFF_TOPIC_DECLINE_MESSAGE, trace=[], routing=decision)]
            )

        decision = self.route(user_message, context)
        agent = self._agents[decision.agent_name]

        hops: list[TurnResult] = []
        # Every agent that has already run this turn. Seeded with the first, so
        # a handoff can never return to where the turn started (no A->B->A).
        visited: set[str] = {agent.name}
        hops_used = 0
        # Each hop after the first sees the source's context_hint appended as a
        # synthetic user turn; the caller's history is never mutated.
        turn_messages: list[Message] = list(messages)

        while True:
            result = await agent.run(turn_messages, context)
            # The agent knows what it did, not why it was picked - see
            # TurnResult.routing. Stamping it here is why TurnResult is the one
            # unfrozen type in app/ai/turn.py.
            result.routing = decision
            hops.append(result)

            handoff = result.handoff
            if handoff is None:
                break

            source = agent.name
            target = handoff.target_agent

            hops_used += 1
            if hops_used > MAX_HOPS:
                logger.warning(
                    "handoff cap reached (max_hops=%d) after agent=%s; ending turn",
                    MAX_HOPS,
                    source,
                )
                break

            if not self._handoff_allowed(source, target, visited, context):
                break

            visited.add(target)
            decision = RoutingDecision(
                agent_name=target,
                reason=handoff.reason,
                confidence=1.0,
                matched_rule=f"handoff_from:{source}",
                handoff_from=source,
            )
            agent = self._agents[target]
            # The hint becomes the target's "user message". It is model-authored
            # PROMPT TEXT and nothing else - the target treats it as untrusted
            # user input, exactly as it treats anything the real user typed. See
            # HandoffToAgentTool.execute for the one case that would need more
            # than that (content lifted out of a document or a statement).
            turn_messages = [
                *turn_messages,
                Message(role="user", content=handoff.context_hint),
            ]
            logger.info("handoff accepted source=%s target=%s", source, target)

        # A last hop still carrying a `handoff` is one whose request was NOT
        # honoured - every `break` above leaves it that way, and an honoured
        # one always has another hop after it.
        last = hops[-1]
        if last.handoff is not None:
            if not last.reply:
                # Said nothing and got nowhere: the user would otherwise be
                # handed an empty bubble.
                last.reply = HANDOFF_REFUSED_REPLY
            elif last.handoff.answer_so_far:
                # A COMPOUND question whose second half never got answered:
                # the source covered its own half (that is what it put in
                # `answer_so_far`) and the gate stopped the rest. Showing the
                # half that worked without saying the other half was dropped
                # is the silent-drop this pass exists to remove - so say it.
                # Only for `answer_so_far`: an ordinary handoff refusal where
                # the source happened to have text is a complete answer that
                # merely also asked for help, and appending to it would be
                # noise.
                last.reply = f"{last.reply}\n\n{HANDOFF_PARTIAL_NOTE}"

        return TurnDispatchResult(hops=hops)

    def _blocked_by_scope_guardrail(self, message: str, context: Context) -> bool:
        """Whether `dispatch` should decline `message` without routing it.

        Deliberately mirrors `route()`'s own document/statement-first check
        rather than calling `route()` to find out: this must run BEFORE
        `route()`, not after, and `route()` is not idempotent to call twice
        against the "route() runs EXACTLY ONCE" invariant documented on
        `dispatch`.
        """
        if context.active_document_id is not None or context.statement_id is not None:
            return False
        return is_out_of_scope(message)

    def _handoff_allowed(
        self, source: str, target: str, visited: set[str], context: Context
    ) -> bool:
        """Whether `source` may hand this turn to `target`. Logs every refusal.

        `target` is MODEL-AUTHORED. It is never logged as free text before it
        has been matched against a registered agent name - same reasoning as
        `_classify_with_model`'s unregistered-name branch.
        """
        if target == QUARANTINED_AGENT:
            # Both directions of the quarantine are closed: DocumentAgent is
            # never handed the handoff tool (so it can never be a source), and
            # this rejects it as a target however it was named.
            logger.warning(
                "handoff rejected source=%s: %s is quarantined and cannot be a target",
                source,
                QUARANTINED_AGENT,
            )
            return False

        if target not in ALLOWED_HANDOFF_TARGETS.get(source, frozenset()):
            logger.warning(
                "handoff rejected source=%s: target is not in that agent's allow-list",
                source,
            )
            return False

        if target not in self._agents:
            logger.warning(
                "handoff rejected source=%s target=%s: allowed but not registered",
                source,
                target,
            )
            return False

        if target in visited:
            # Cycle prevention. A turn that came back to an agent that already
            # ran would re-read the same data and reach the same conclusion,
            # i.e. loop - the hop cap would stop it eventually, but late and
            # after paying for it.
            logger.warning(
                "handoff rejected source=%s target=%s: already ran this turn",
                source,
                target,
            )
            return False

        if context.statement_id is not None and target == "banking":
            # STATEMENT-MODE GATE (Step 13 interaction). With a statement
            # active, the insights tools read `statement_rows` instead of the
            # ledger, so the ids in their output are statement_rows.id values,
            # NOT ledger references. A banking action taken as a consequence of
            # such a finding would be reasoning about ids that mean something
            # else entirely - silently unsafe rather than loudly broken.
            # Conservative for Step 15; a later step can relax it per-action,
            # once statement-aware banking actions exist to relax it for.
            logger.warning(
                "handoff rejected source=%s target=banking: a statement is active, so "
                "insights ids are statement_rows ids and not ledger references",
                source,
            )
            return False

        return True
