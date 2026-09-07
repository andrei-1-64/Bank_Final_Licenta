"""AIService — the single entry point into the AI layer.

Wires provider -> tools -> agent -> orchestrator. The future `/chat` endpoint
calls `handle_message` and nothing else.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.ai.agents.banking_agent import BankingAgent
from app.ai.agents.docs_agent import DocsAgent
from app.ai.agents.document_agent import DocumentAgent
from app.ai.agents.insights_agent import InsightsAgent
from app.ai.agents.planning_agent import PlanningAgent
from app.ai.context import Context
from app.ai.orchestrator import Orchestrator
from app.ai.providers.base import ModelProvider
from app.ai.schemas import Message
from app.ai.tools.banking import (
    AddBeneficiaryTool,
    ConvertCurrencyTool,
    CreateScheduledTransferTool,
    FindBeneficiaryByNameTool,
    FreezeCardTool,
    GetBalanceTool,
    ListAccountsTool,
    ListCardsTool,
    ListTransactionsTool,
    ListTransfersTool,
    ProposeCardOrderTool,
    RemoveBeneficiaryTool,
    ResolveIbanHolderTool,
    SetCardSpendingLimitTool,
    UnfreezeCardTool,
)
from app.ai.tools.document_tools import ReadDocumentTool
from app.ai.tools.handoff_tool import HandoffToAgentTool
from app.ai.tools.insights import (
    CategorizeTransactionsTool,
    CompareStatementToLedgerTool,
    ComputeSpendingStatsTool,
    DetectAnomaliesTool,
    DetectRecurringPaymentsTool,
    GetTransactionsInRangeTool,
)
from app.ai.tools.knowledge import SearchKnowledgeBaseTool
from app.ai.tools.planning import ProjectBalanceTool, SavingsGoalTool, SimulateScenarioTool
from app.ai.tools.propose_tools import (
    ProposeCancelCardTool,
    ProposeCloseAccountTool,
    ProposeOpenAccountTool,
    ProposePaymentTool,
    ProposeTransferTool,
)
from app.ai.tools.registry import ToolRegistry
from app.ai.tools.statement_tools import SummarizeStatementTool
from app.ai.turn import TurnDispatchResult

if TYPE_CHECKING:
    from app.ai.providers.embedding_base import EmbeddingProvider
    from supabase import AsyncClient


def build_banking_tools(supabase: AsyncClient) -> ToolRegistry:
    """The tools the banking agent is allowed to call: the original read-only
    set, a handful of low-stakes write tools that execute directly (see each
    one's own docstring for why), and the propose_* tools for higher-stakes
    actions (money movement, opening/closing an account, cancelling a card,
    ordering a physical card) - those only ever create a pending proposal,
    never execute anything themselves (see app/ai/tools/propose_tools.py's
    module docstring).

    `supabase` is handed to every tool that reads/writes data; the tools hold
    it for their lifetime (the client is a stateless HTTP client, safe to
    share).
    """
    return ToolRegistry(
        [
            GetBalanceTool(supabase),
            ListAccountsTool(supabase),
            ListTransactionsTool(supabase),
            ListCardsTool(supabase),
            ListTransfersTool(supabase),
            ResolveIbanHolderTool(supabase),
            FindBeneficiaryByNameTool(supabase),
            # Takes no `supabase`, unlike everything else here: currency
            # conversion is a pure calculation over BNR's public daily rate
            # table and reads nothing of the user's (see its docstring).
            ConvertCurrencyTool(),
            # Low-stakes and reversible: execute directly (see each tool's
            # own docstring for why it doesn't need a UI-level confirm step).
            FreezeCardTool(supabase),
            UnfreezeCardTool(supabase),
            SetCardSpendingLimitTool(supabase),
            AddBeneficiaryTool(supabase),
            RemoveBeneficiaryTool(supabase),
            CreateScheduledTransferTool(supabase),
            ProposeCardOrderTool(supabase),
            # Write-adjacent: each only creates a `pending` proposal row, never
            # executes (see app/ai/tools/propose_tools.py's module docstring).
            ProposeTransferTool(supabase),
            ProposePaymentTool(supabase),
            ProposeOpenAccountTool(supabase),
            ProposeCloseAccountTool(supabase),
            ProposeCancelCardTool(supabase),
            # NO HandoffToAgentTool here, deliberately (Step 15): BankingAgent
            # is TERMINAL in a handoff chain. It is the agent that produces
            # proposals - the end of the line - and the one holding every
            # write-adjacent tool above, so it gets no way to pull another
            # agent in behind it. See ALLOWED_HANDOFF_TARGETS in
            # app/ai/orchestrator.py, which also has no "banking" key.
        ]
    )


def build_insights_tools(supabase: AsyncClient, provider: ModelProvider) -> ToolRegistry:
    """The read-only tools the insights agent is allowed to call, plus the
    handoff tool (Step 15).

    Deliberately a different registry from `build_banking_tools`: an agent's
    reach is defined by what it is handed, so the analytical agent cannot read
    card numbers and the banking agent cannot run an unbounded date sweep.

    `provider` only feeds CategorizeTransactionsTool's few-shot classifier
    (see categorize_transactions.py) - every other tool here ignores it.

    `handoff_to_agent` is the one thing here that is not a read: it lets an
    analytical finding continue on an agent that can act on it (the Step 15
    demo path - a recurring charge becoming a cancel_card proposal). It does
    not widen this agent's own reach by a single tool. It only ASKS for the
    turn to continue elsewhere, and where it may continue to is decided by
    `ALLOWED_HANDOFF_TARGETS` in app/ai/orchestrator.py - banking or planning,
    never documents - not by anything the model writes into its arguments.
    """
    return ToolRegistry(
        [
            GetTransactionsInRangeTool(supabase),
            CategorizeTransactionsTool(supabase, provider),
            DetectRecurringPaymentsTool(supabase),
            ComputeSpendingStatsTool(supabase),
            DetectAnomaliesTool(supabase),
            CompareStatementToLedgerTool(supabase),
            # Same instance-free read tool the banking agent gets: an analysis
            # of foreign-currency spending needs to state amounts in RON, and
            # sending the turn to another agent just to divide by a published
            # rate would be a handoff that buys nothing.
            ConvertCurrencyTool(),
            HandoffToAgentTool(),
        ]
    )


def build_planning_tools(supabase: AsyncClient) -> ToolRegistry:
    """The read-only tools the planning agent is allowed to call.

    A third distinct registry, same reason as insights vs banking: the
    goal-oriented agent projects and simulates, it does not need (and is not
    handed) the analytical categorisation/anomaly tools either.

    It does get `handoff_to_agent` (Step 15), for the same reason Insights
    does: a plan that needs a concrete first step has nowhere to put one.
    Its allow-list is narrower than Insights' - banking only, never planning
    back into itself and never documents (see ALLOWED_HANDOFF_TARGETS).
    """
    return ToolRegistry(
        [
            ProjectBalanceTool(supabase),
            SimulateScenarioTool(supabase),
            SavingsGoalTool(supabase),
            HandoffToAgentTool(),
        ]
    )


def build_docs_tools(
    supabase: AsyncClient, embedding_provider: EmbeddingProvider
) -> ToolRegistry:
    """The docs agent's one tool: semantic search over ingested documentation.

    Unlike the other three registries, this one also needs an embedding
    provider (to embed the query) - the tool holds both.
    """
    return ToolRegistry([SearchKnowledgeBaseTool(supabase, embedding_provider)])


def build_document_tools(supabase: AsyncClient) -> ToolRegistry:
    """DocumentAgent's ENTIRE toolset: read_document and, since Step 13,
    summarize_statement. Nothing else.

    This is the structural half of Step 12's prompt-injection defense (the
    other half is the <untrusted_document> / <untrusted_statement> wrapping
    + system prompt in document_agent.py), and it still holds for
    summarize_statement: no propose_* tool, no banking read tool, nothing
    that could hand control to another agent is ever in this registry - a
    document's or statement's content reaching the model can therefore
    never result in a write, a proposal, or a read of anything outside that
    one document/statement, regardless of what its text says. Do not add
    any OTHER kind of tool here - a new read-only, no-handoff,
    aggregate-or-wrapped tool over the same active document/statement is
    the only thing this registry may ever grow.

    STEP 15 MADE "nothing that could hand control to another agent" LITERAL.
    Cross-agent handoff now exists as an actual tool,
    `app/ai/tools/handoff_tool.py::HandoffToAgentTool`, and it is NOT here -
    that omission is the invariant above, not an oversight, and adding it
    would end DocumentAgent's isolation in one line. The quarantine is closed
    from the other side too: `Orchestrator.dispatch` rejects any handoff
    naming "documents" as its target, whichever agent asked (see
    QUARANTINED_AGENT in app/ai/orchestrator.py), so a document cannot talk
    its way into this agent from outside either.
    """
    return ToolRegistry([ReadDocumentTool(supabase), SummarizeStatementTool(supabase)])


class AIService:
    """Holds the orchestrator and hands conversations to the routed agent."""

    def __init__(
        self,
        supabase: AsyncClient,
        provider: ModelProvider | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        orchestrator: Orchestrator | None = None,
    ) -> None:
        # Real providers by default; tests inject mocks.
        if provider is None:
            from app.ai.providers.azure_provider import AzureOpenAIProvider

            provider = AzureOpenAIProvider()
        if embedding_provider is None:
            from app.ai.providers.azure_embedding_provider import AzureEmbeddingProvider

            embedding_provider = AzureEmbeddingProvider()
        self._provider = provider
        # The provider goes to the orchestrator too: routing's LLM fallback
        # needs a model of its own now that more than one agent is registered.
        self._orchestrator = orchestrator or self._build_orchestrator(
            supabase, provider, embedding_provider
        )

    @staticmethod
    def _build_orchestrator(
        supabase: AsyncClient, provider: ModelProvider, embedding_provider: EmbeddingProvider
    ) -> Orchestrator:
        """Register the agents, in the order routing should consider them.

        ORDER MATTERS. `Orchestrator._match_rules` walks agents in registration
        order and stops at the first rule that claims the message, so the agent
        registered first gets first refusal. Insights goes first deliberately:
        it and Banking share the `cheltui` / `bani` keywords (Banking has had
        them since Step 6), and for a phrase like "cât am cheltuit?" the
        analytical reading is the more specific one.

        Docs goes right after Insights, for the same kind of reason: a message
        like "ce comision are contul curent" contains Banking's `cont` stem
        too, but the more specific reading is "what does the fee schedule say"
        - a documentation question, not a request to read the account's data.

        Planning goes LAST deliberately, for the opposite reason: it shares
        the `econom` stem with Banking's "Economii" account keyword, and here
        Banking should win - "arată-mi contul de economii" is transactional.
        That does mean a bare "econom"-only savings-goal phrasing currently
        mis-routes to Banking (see the KNOWN COLLISION note on
        `PlanningAgent`'s rules); acceptable for now, revisited in Step 16.

        Banking is still the DEFAULT — what an unmatched or unclassifiable
        message falls back to — which is a separate thing from rule order.

        DocumentAgent's registration position among these barely matters:
        DocumentAgent's registration position among these matters only for
        messages sent with nothing attached. While a document or statement IS
        attached, `Orchestrator.route()` takes its own path (see
        `_route_with_attachment`): the attachment gets first refusal and the
        last word, but a message another agent's rules actually claim — "cât
        am acum în cont?" — goes to that agent instead of being pinned here.
        DocumentAgent's own keyword rules (`document`, `pdf`, `contract`, ...)
        don't overlap any other agent's stems, so registering it here — after
        Insights, before Docs/Banking — costs nothing either way.
        """
        orchestrator = Orchestrator(provider=provider)
        orchestrator.register(InsightsAgent(provider, build_insights_tools(supabase, provider)))
        orchestrator.register(DocumentAgent(provider, build_document_tools(supabase)))
        orchestrator.register(DocsAgent(provider, build_docs_tools(supabase, embedding_provider)))
        orchestrator.register(
            BankingAgent(provider, build_banking_tools(supabase)), default=True
        )
        orchestrator.register(PlanningAgent(provider, build_planning_tools(supabase)))
        return orchestrator

    @property
    def orchestrator(self) -> Orchestrator:
        return self._orchestrator

    async def handle_message(
        self,
        history: Sequence[Message],
        user_message: str,
        context: Context,
    ) -> TurnDispatchResult:
        """Answer `user_message` for the user identified by `context`.

        `context` is required and has no default on purpose: forgetting to pass
        an identity must be an error, never a silent fallback to some ambient
        one. Callers build it at the edge (see `app.ai.context`).

        Returns the whole turn - every agent hop that ran, in order, each with
        its own reply, trace and routing decision (see `app.ai.turn`). Callers
        that only want the transcript take `result.new_messages` and append it
        to the history they passed in; callers that must keep the hops apart,
        because each one persists its own routing row, walk `result.hops`
        instead (see `app.modules.chat.router`). Either way a reload replays
        the same transcript the models actually saw.

        Before Step 15 this returned `(reply, history, routing)` and carried a
        TODO about switching to a model rather than widening that tuple a
        fourth time. Cross-agent handoff is what made it a fourth time.
        """
        conversation = [*history, Message(role="user", content=user_message)]
        return await self._orchestrator.dispatch(conversation, user_message, context)
