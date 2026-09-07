"""The balance tool, its validation boundary, and the registry.

Offline: the tool's Supabase handle is the `FakeSupabase` from conftest, so
these cover the tool's *contract* (shape, validation, refusals). The real
ledger read is covered in tests/integration/test_get_balance_tool.py.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.ai.context import Context
from app.ai.providers.mock_provider import MockProvider
from app.ai.schemas import ModelResponse, ToolCall, ToolResult
from app.ai.tools.banking import (
    AddBeneficiaryTool,
    CreateScheduledTransferTool,
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
from app.ai.tools.base import Tool
from app.ai.tools.insights import (
    CategorizeTransactionsTool,
    CompareStatementToLedgerTool,
    ComputeSpendingStatsTool,
    DetectAnomaliesTool,
    DetectRecurringPaymentsTool,
    GetTransactionsInRangeTool,
)
from app.ai.tools.planning import ProjectBalanceTool, SavingsGoalTool, SimulateScenarioTool
from app.ai.tools.propose_tools import (
    ProposeCancelCardTool,
    ProposeCloseAccountTool,
    ProposeOpenAccountTool,
    ProposePaymentTool,
    ProposeTransferTool,
)
from app.ai.tools.registry import ToolRegistry
from tests.ai.conftest import OWNED_ACCOUNT_IDS, STUB_BALANCE_MINOR, STUB_CURRENCY

#: Every READ-ONLY tool the banking agent exposes, in the order
#: build_banking_tools registers them. The five propose_* write-adjacent
#: tools (Step 11) are covered separately below, since they carry
#: read_only = False.
ALL_TOOL_CLASSES = (
    GetBalanceTool,
    ListAccountsTool,
    ListTransactionsTool,
    ListCardsTool,
    ListTransfersTool,
    ResolveIbanHolderTool,
    FreezeCardTool,
    UnfreezeCardTool,
    SetCardSpendingLimitTool,
    AddBeneficiaryTool,
    RemoveBeneficiaryTool,
    CreateScheduledTransferTool,
    ProposeCardOrderTool,
)

#: The five propose_* tools added in Step 11 - see app/ai/tools/propose_tools.py.
ALL_PROPOSE_TOOL_CLASSES = (
    ProposeTransferTool,
    ProposePaymentTool,
    ProposeOpenAccountTool,
    ProposeCloseAccountTool,
    ProposeCancelCardTool,
)

#: Every tool the insights agent exposes, in the order build_insights_tools
#: registers them (Step 8's get_transactions_in_range, then Step 9's four,
#: then Step 13's compare_statement_to_ledger).
ALL_INSIGHTS_TOOL_CLASSES = (
    GetTransactionsInRangeTool,
    CategorizeTransactionsTool,
    DetectRecurringPaymentsTool,
    ComputeSpendingStatsTool,
    DetectAnomaliesTool,
    CompareStatementToLedgerTool,
)


async def test_get_balance_returns_the_ledger_figures(context, supabase):
    tool = GetBalanceTool(supabase)

    result = await tool.execute(
        ToolCall(id="c1", name="get_balance", arguments={"account_id": OWNED_ACCOUNT_IDS[0]}),
        context,
    )

    assert result.ok
    assert result.tool_call_id == "c1"
    assert result.data == {
        "account_id": OWNED_ACCOUNT_IDS[0],
        "balance_minor": STUB_BALANCE_MINOR,
        "currency": STUB_CURRENCY,
        # No longer "stub" - the number came from the ledger read path.
        "source": "ledger",
    }
    # Minor units stay an int — never a float.
    assert isinstance(result.data["balance_minor"], int)


def test_get_balance_is_read_only(supabase):
    assert GetBalanceTool(supabase).read_only is True


async def test_execute_rejects_invalid_input_without_raising(context, supabase):
    tool = GetBalanceTool(supabase)

    result = await tool.execute(
        ToolCall(id="c1", name="get_balance", arguments={"account_id": 123}), context
    )

    assert result.ok is False
    assert "account_id" in (result.error or "")
    assert result.tool_call_id == "c1"


async def test_execute_contains_a_crashing_tool(context):
    class Boom(Tool):
        name = "boom"
        description = "always fails"
        input_schema = GetBalanceTool.input_schema

        async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
            raise RuntimeError("secret detail that must not leak")

    result = await Boom().execute(ToolCall(id="c1", name="boom", arguments={}), context)

    assert result.ok is False
    assert "secret detail" not in (result.error or "")


def test_registry_specs_and_lookup(supabase):
    registry = ToolRegistry([GetBalanceTool(supabase)])

    assert registry.names() == ["get_balance"]
    assert registry.get("get_balance") is not None
    assert registry.get("transfer_money") is None

    spec = registry.list_specs()[0]
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "get_balance"
    assert "account_id" in spec["function"]["parameters"]["properties"]


# ---------------------------------------------------------------------------
# The read tools added in Step 6, as the model is shown them.
# ---------------------------------------------------------------------------


def _spec_for(registry: ToolRegistry, name: str) -> dict:
    return next(s for s in registry.list_specs() if s["function"]["name"] == name)


def test_all_banking_tools_are_registered(supabase):
    from app.ai.service import build_banking_tools

    assert build_banking_tools(supabase).names() == [
        "get_balance",
        "list_accounts",
        "list_transactions",
        "list_cards",
        "list_transfers",
        "resolve_iban_holder",
        "find_beneficiary_by_name",
        # Reads no user data at all - the BNR daily reference rates.
        "convert_currency",
        "freeze_card",
        "unfreeze_card",
        "set_card_spending_limit",
        "add_beneficiary",
        "remove_beneficiary",
        "create_scheduled_transfer",
        "propose_card_order",
        "propose_transfer",
        "propose_payment",
        "propose_open_account",
        "propose_close_account",
        "propose_cancel_card",
    ]


def test_every_tool_advertises_a_usable_spec(supabase):
    """Structural guard: whatever a tool is, the model must see a named
    function with a described JSON-Schema parameter object."""
    registry = ToolRegistry([cls(supabase) for cls in ALL_TOOL_CLASSES])

    for spec in registry.list_specs():
        assert spec["type"] == "function"
        assert spec["function"]["name"]
        assert spec["function"]["description"]
        assert spec["function"]["parameters"]["type"] == "object"


def test_list_accounts_spec_takes_no_arguments(supabase):
    registry = ToolRegistry([ListAccountsTool(supabase)])
    params = _spec_for(registry, "list_accounts")["function"]["parameters"]

    # Nothing to supply, so nothing to guess or widen.
    assert params.get("properties", {}) == {}
    assert not params.get("required")


def test_list_transactions_spec_advertises_its_bounded_arguments(supabase):
    registry = ToolRegistry([ListTransactionsTool(supabase)])
    params = _spec_for(registry, "list_transactions")["function"]["parameters"]
    properties = params["properties"]

    assert set(properties) == {"account_id", "days_back", "limit"}
    # All optional - the model never has to know an account id.
    assert not params.get("required")
    assert properties["days_back"]["default"] == 30
    assert properties["days_back"]["maximum"] == 365
    assert properties["limit"]["default"] == 50
    assert properties["limit"]["maximum"] == 200


def test_list_cards_spec_takes_no_arguments(supabase):
    registry = ToolRegistry([ListCardsTool(supabase)])
    params = _spec_for(registry, "list_cards")["function"]["parameters"]

    assert params.get("properties", {}) == {}
    assert not params.get("required")


def test_list_transfers_spec_advertises_a_bounded_limit(supabase):
    registry = ToolRegistry([ListTransfersTool(supabase)])
    params = _spec_for(registry, "list_transfers")["function"]["parameters"]
    properties = params["properties"]

    assert set(properties) == {"limit"}
    assert not params.get("required")
    assert properties["limit"]["default"] == 20
    assert properties["limit"]["maximum"] == 100


@pytest.mark.parametrize(
    "arguments",
    [{"days_back": 0}, {"days_back": 400}, {"limit": 0}, {"limit": 500}],
    ids=["days-too-small", "days-too-large", "limit-too-small", "limit-too-large"],
)
async def test_list_transactions_rejects_out_of_range_arguments(
    context, supabase, arguments
):
    """Bounds live in the schema, so an out-of-range value is a clean,
    correctable validation failure rather than an unbounded query."""
    result = await ListTransactionsTool(supabase).execute(
        ToolCall(id="c1", name="list_transactions", arguments=arguments), context
    )

    assert result.ok is False
    assert "invalid input" in (result.error or "")


def test_registry_rejects_duplicates(supabase):
    registry = ToolRegistry([GetBalanceTool(supabase)])

    with pytest.raises(ValueError):
        registry.register(GetBalanceTool(supabase))


def test_registry_subset_narrows_permissions(supabase):
    registry = ToolRegistry([GetBalanceTool(supabase)])

    assert registry.subset(["get_balance"]).names() == ["get_balance"]
    assert registry.subset([]).names() == []


#: The only write / write-adjacent tools the banking agent may expose - each
#: is either low-stakes and reversible enough to execute directly (see its
#: own module docstring for why), or a propose_* tool that never executes
#: anything itself, only ever inserting a pending `proposals` row (see
#: propose_tools.py's module docstring; propose_card_order is the one
#: exception - it stays read_only, see its own docstring). This guardrail is
#: the successor to the old test_no_write_tools_are_registered, back when
#: there were none: it still catches an ACCIDENTAL new write tool showing up
#: unreviewed, it just no longer assumes there are zero on purpose.
_ALLOWED_WRITE_TOOL_NAMES = frozenset(
    {
        "freeze_card",
        "unfreeze_card",
        "set_card_spending_limit",
        "add_beneficiary",
        "remove_beneficiary",
        "create_scheduled_transfer",
        "propose_transfer",
        "propose_payment",
        "propose_open_account",
        "propose_close_account",
        "propose_cancel_card",
    }
)


def test_only_the_reviewed_write_tools_are_registered(supabase):
    """Guardrail: the model is untrusted. Every banking tool must be either
    read-only, or one of the small, deliberately-reviewed write/write-adjacent
    tools above (propose_card_order stays read_only on purpose - see its
    docstring)."""
    from app.ai.service import build_banking_tools

    tools = build_banking_tools(supabase)
    write_adjacent = {tool.name for tool in tools if not tool.read_only}
    assert write_adjacent == _ALLOWED_WRITE_TOOL_NAMES


# ---------------------------------------------------------------------------
# The insights tools (Step 8's get_transactions_in_range, plus the four
# analytical tools added in Step 9), as the model is shown them.
# ---------------------------------------------------------------------------


def test_all_insights_tools_are_registered(supabase):
    from app.ai.service import build_insights_tools

    provider = MockProvider([ModelResponse(text="ok")])
    assert build_insights_tools(supabase, provider).names() == [
        "get_transactions_in_range",
        "categorize_transactions",
        "detect_recurring_payments",
        "compute_spending_stats",
        "detect_anomalies",
        "compare_statement_to_ledger",
        "convert_currency",
        # Step 15: not an analytical tool, but this agent's one way out.
        "handoff_to_agent",
    ]


def test_every_insights_tool_advertises_a_usable_spec(supabase):
    """Same structural guard as the banking tools: a named function with a
    described JSON-Schema parameter object, for every insights tool."""
    provider = MockProvider([ModelResponse(text="ok")])
    registry = ToolRegistry(
        [
            cls(supabase, provider) if cls is CategorizeTransactionsTool else cls(supabase)
            for cls in ALL_INSIGHTS_TOOL_CLASSES
        ]
    )

    for spec in registry.list_specs():
        assert spec["type"] == "function"
        assert spec["function"]["name"]
        assert spec["function"]["description"]
        assert spec["function"]["parameters"]["type"] == "object"


def test_no_write_tools_are_registered_for_insights(supabase):
    """Guardrail: the analytical agent is read-only, same as banking."""
    from app.ai.service import build_insights_tools

    provider = MockProvider([ModelResponse(text="ok")])
    assert all(tool.read_only for tool in build_insights_tools(supabase, provider))


# ---------------------------------------------------------------------------
# The planning tools (Step 10's project_balance, simulate_scenario, and
# savings_goal), as the model is shown them.
# ---------------------------------------------------------------------------

#: Every tool the planning agent exposes, in the order build_planning_tools
#: registers them.
ALL_PLANNING_TOOL_CLASSES = (
    ProjectBalanceTool,
    SimulateScenarioTool,
    SavingsGoalTool,
)


def test_all_planning_tools_are_registered(supabase):
    from app.ai.service import build_planning_tools

    assert build_planning_tools(supabase).names() == [
        "project_balance",
        "simulate_scenario",
        "savings_goal",
        "handoff_to_agent",  # Step 15
    ]


def test_every_planning_tool_advertises_a_usable_spec(supabase):
    """Same structural guard as the banking/insights tools: a named function
    with a described JSON-Schema parameter object, for every planning tool."""
    registry = ToolRegistry([cls(supabase) for cls in ALL_PLANNING_TOOL_CLASSES])

    for spec in registry.list_specs():
        assert spec["type"] == "function"
        assert spec["function"]["name"]
        assert spec["function"]["description"]
        assert spec["function"]["parameters"]["type"] == "object"


def test_no_write_tools_are_registered_for_planning(supabase):
    """Guardrail: the planning agent proposes, it doesn't execute."""
    from app.ai.service import build_planning_tools

    assert all(tool.read_only for tool in build_planning_tools(supabase))
