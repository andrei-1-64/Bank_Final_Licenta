"""Offline contract tests for the propose_* tools (Step 11).

Same spirit as test_tools.py: input-schema validation and access-denied
refusals never touch the database (Tool.execute validates before `run()`
runs, and `Context.resolve_account`/`owns` are pure in-memory checks), so
these run against the offline `FakeSupabase`. The real create_proposal
insert + confirm/reject/execute flow is covered in
tests/integration/test_propose_tools.py and test_proposals_confirm.py.
"""

from __future__ import annotations

import pytest

from app.ai.context import Context
from app.ai.schemas import ToolCall
from app.ai.tools.propose_tools import (
    PROPOSE_TOOL_NAMES,
    ProposeCancelCardTool,
    ProposeCloseAccountTool,
    ProposeOpenAccountTool,
    ProposePaymentTool,
    ProposeTransferTool,
)
from app.ai.tools.registry import ToolRegistry
from tests.ai.conftest import OWNED_ACCOUNT_IDS, UNOWNED_ACCOUNT_ID

ALL_PROPOSE_TOOL_CLASSES = (
    ProposeTransferTool,
    ProposePaymentTool,
    ProposeOpenAccountTool,
    ProposeCloseAccountTool,
    ProposeCancelCardTool,
)


def test_propose_tool_names_match_the_module_constant():
    assert {cls.name for cls in ALL_PROPOSE_TOOL_CLASSES} == set(PROPOSE_TOOL_NAMES)


def test_every_propose_tool_is_write_adjacent(supabase):
    for cls in ALL_PROPOSE_TOOL_CLASSES:
        assert cls(supabase).read_only is False


def test_every_propose_tool_advertises_a_usable_spec(supabase):
    registry = ToolRegistry([cls(supabase) for cls in ALL_PROPOSE_TOOL_CLASSES])

    for spec in registry.list_specs():
        assert spec["type"] == "function"
        assert spec["function"]["name"]
        assert spec["function"]["description"]
        assert spec["function"]["parameters"]["type"] == "object"


async def test_propose_transfer_rejects_non_positive_amount(context, supabase):
    result = await ProposeTransferTool(supabase).execute(
        ToolCall(
            id="c1",
            name="propose_transfer",
            arguments={
                "from_account_id": OWNED_ACCOUNT_IDS[0],
                "to_account_id": OWNED_ACCOUNT_IDS[1],
                "amount_minor": 0,
                "currency": "RON",
            },
        ),
        context,
    )
    assert result.ok is False
    assert "invalid input" in (result.error or "")


async def test_propose_transfer_refuses_an_account_the_user_does_not_own(context, supabase):
    """SECURITY: the model naming someone else's account id must never reach
    the DB - `context.resolve_account` refuses before propose_tools.py's
    `_resolve_owned_account` issues any query, and the refusal never echoes
    the refused id (see IdentityError's docstring)."""
    result = await ProposeTransferTool(supabase).execute(
        ToolCall(
            id="c1",
            name="propose_transfer",
            arguments={
                "from_account_id": UNOWNED_ACCOUNT_ID,
                "to_account_id": OWNED_ACCOUNT_IDS[1],
                "amount_minor": 10_000,
                "currency": "RON",
            },
        ),
        context,
    )
    assert result.ok is False
    assert "access denied" in (result.error or "")
    assert UNOWNED_ACCOUNT_ID not in (result.error or "")


async def test_propose_close_account_refuses_an_account_the_user_does_not_own(context, supabase):
    result = await ProposeCloseAccountTool(supabase).execute(
        ToolCall(
            id="c1",
            name="propose_close_account",
            arguments={"account_id": UNOWNED_ACCOUNT_ID},
        ),
        context,
    )
    assert result.ok is False
    assert "access denied" in (result.error or "")


async def test_propose_open_account_requires_term_months_for_term_deposit(context, supabase):
    result = await ProposeOpenAccountTool(supabase).execute(
        ToolCall(
            id="c1",
            name="propose_open_account",
            arguments={"name": "Depozitul meu", "product_type": "term_deposit"},
        ),
        context,
    )
    assert result.ok is False
    assert "invalid input" in (result.error or "")


async def test_propose_close_account_requires_an_active_conversation():
    """propose_* tools need Context.conversation_id (Step 11's addition to
    Context) to satisfy the proposals.conversation_id NOT NULL FK - a
    Context built with none (e.g. the CLI's dev_context) must fail cleanly
    as a tool error, after the account itself resolves fine, never crashing
    the loop."""

    class _FakeQueryWithName:
        async def execute(self):
            from types import SimpleNamespace

            return SimpleNamespace(
                data={"id": OWNED_ACCOUNT_IDS[0], "name": "Cont Curent", "currency": "RON"}
            )

        def __getattr__(self, _name):
            return lambda *a, **kw: self

    class _FakeSupabaseWithNamedAccount:
        def table(self, *_a, **_kw):
            return _FakeQueryWithName()

    context_without_conversation = Context(user_id="user-under-test", account_ids=OWNED_ACCOUNT_IDS)

    result = await ProposeCloseAccountTool(_FakeSupabaseWithNamedAccount()).execute(
        ToolCall(
            id="c1",
            name="propose_close_account",
            arguments={"account_id": OWNED_ACCOUNT_IDS[0]},
        ),
        context_without_conversation,
    )
    assert result.ok is False
    assert "conversation" in (result.error or "")


# ---------------------------------------------------------------------------
# Currency: settled from the accounts, never from the model.
#
# `transfers.service.create_transfer` requires both accounts and the transfer
# to share one currency. That check runs at EXECUTION time - after the user
# has read the proposal, tapped confirm, and proved their identity with Face
# ID or a password - and used to surface as a raw English
# "Transfer currency must match both accounts' currency." inside the
# confirmation dialog of a Romanian app.
#
# Two accounts in DIFFERENT currencies are no longer refused at all: they are
# converted at the BNR rate (see tests/ai/test_fx_transfers.py). What these
# tests still hold is the other half of the same fix - the model's `currency`
# argument is advisory and never decides anything.
# ---------------------------------------------------------------------------


class _TwoCurrencyQuery:
    """Answers with whichever account row was asked for, by id."""

    # The row's `id` is a real UUID (the tool parses it before asking the
    # ledger for a balance); the key is the opaque id the Context vouches for.
    _ROWS = {
        OWNED_ACCOUNT_IDS[0]: {
            "id": "aaaaaaaa-0000-0000-0000-000000000001",
            "name": "Cont Curent",
            "currency": "RON",
        },
        OWNED_ACCOUNT_IDS[1]: {
            "id": "aaaaaaaa-0000-0000-0000-000000000002",
            "name": "Cont Euro",
            "currency": "EUR",
        },
    }

    def __init__(self) -> None:
        self._row = None

    def eq(self, column, value):
        if column == "id":
            self._row = self._ROWS.get(value)
        return self

    def __getattr__(self, _name):
        return lambda *a, **kw: self

    async def execute(self):
        from types import SimpleNamespace

        return SimpleNamespace(data=self._row)


class _TwoCurrencySupabase:
    """Two accounts, RON and EUR, and an empty RON account balance.

    The zero balance is deliberate: it makes `propose_transfer` stop at its
    funds check, which is the first thing `run` does - so these tests stay
    about argument handling and never reach BNR.
    """

    def table(self, *_a, **_kw):
        return _TwoCurrencyQuery()

    def rpc(self, _name, _params):
        from types import SimpleNamespace

        class _Rpc:
            async def execute(self):
                return SimpleNamespace(data=0)

        return _Rpc()


async def test_propose_transfer_does_not_need_the_model_to_supply_a_currency(context):
    """The field is advisory. Omitting it entirely is not a schema error - the
    account's own currency is what ends up on the proposal either way."""
    result = await ProposeTransferTool(_TwoCurrencySupabase()).execute(
        ToolCall(
            id="c1",
            name="propose_transfer",
            arguments={
                "from_account_id": OWNED_ACCOUNT_IDS[0],
                "to_account_id": OWNED_ACCOUNT_IDS[1],
                "amount_minor": 10_000,
            },
        ),
        context,
    )

    # Refused for lack of funds, NOT for a missing argument.
    assert result.ok is False
    assert "invalid input" not in (result.error or "")
    assert "Fonduri insuficiente" in (result.error or "")


# ---------------------------------------------------------------------------
# The other half of the same fix: the CONFIRM path.
#
# The tools above stop a bad proposal being created. These cover proposals
# that already exist - built before propose_transfer read the currency off
# the account, or naming two accounts that no longer (or never did) agree.
# `_execute` hands the stored payload straight to create_transfer, which
# validates at execution time: after the user has read the proposal, tapped
# confirm, and proved their identity. `_assert_still_executable` moves that
# "no" ahead of the credential check and says it in Romanian.
# ---------------------------------------------------------------------------


def _transfer_proposal(currency: str) -> dict:
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "proposal_type": "transfer",
        "payload": {
            "from_account_id": "22222222-2222-2222-2222-222222222222",
            "to_account_id": "33333333-3333-3333-3333-333333333333",
            "amount_minor": 50_000,
            "currency": currency,
        },
    }


def _accounts(from_currency: str, to_currency: str):
    rows = {
        "22222222-2222-2222-2222-222222222222": {
            "id": "22222222-2222-2222-2222-222222222222",
            "name": "Cont Curent",
            "currency": from_currency,
        },
        "33333333-3333-3333-3333-333333333333": {
            "id": "33333333-3333-3333-3333-333333333333",
            "name": "Cont Euro",
            "currency": to_currency,
        },
    }

    async def _get_account(_supabase, _user, account_id):
        return rows[str(account_id)]

    return _get_account


async def test_confirm_refuses_a_cross_currency_proposal_with_no_locked_rate(monkeypatch):
    """A proposal built before cross-currency transfers existed, still
    pending. There is no rate on it, and inventing one now would execute a
    number the user never saw."""
    from app.core.exceptions import CurrencyMismatchError
    from app.modules.chat import proposals_service

    monkeypatch.setattr(proposals_service, "get_account", _accounts("RON", "EUR"))

    with pytest.raises(CurrencyMismatchError) as exc:
        await proposals_service._assert_still_executable(
            None, None, _transfer_proposal("RON")
        )

    message = str(exc.value)
    assert "curs de schimb" in message
    assert "Cont Curent" in message and "Cont Euro" in message


async def test_confirm_refuses_a_proposal_labelled_with_the_wrong_currency(monkeypatch):
    """A stale proposal must not be quietly executed in the RIGHT currency:
    500 EUR is not the 500 RON the user read and approved."""
    from app.core.exceptions import CurrencyMismatchError
    from app.modules.chat import proposals_service

    monkeypatch.setattr(proposals_service, "get_account", _accounts("RON", "RON"))

    with pytest.raises(CurrencyMismatchError) as exc:
        await proposals_service._assert_still_executable(
            None, None, _transfer_proposal("EUR")
        )

    assert "nu corespunde monedei contului" in str(exc.value)


async def test_confirm_lets_a_consistent_transfer_through(monkeypatch):
    from app.modules.chat import proposals_service

    monkeypatch.setattr(proposals_service, "get_account", _accounts("RON", "RON"))

    # No exception is the assertion.
    await proposals_service._assert_still_executable(None, None, _transfer_proposal("RON"))


async def test_confirm_does_not_second_guess_other_proposal_types():
    """Only a transfer carries a currency that can contradict its accounts."""
    from app.modules.chat import proposals_service

    for proposal_type in ("payment", "open_account", "close_account", "cancel_card"):
        await proposals_service._assert_still_executable(
            None, None, {"proposal_type": proposal_type, "payload": {}}
        )
