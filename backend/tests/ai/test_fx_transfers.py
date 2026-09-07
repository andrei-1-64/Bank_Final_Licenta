"""Cross-currency transfers: BNR conversion at proposal time, locked rate at
confirm time.

Offline throughout. `bnr_client.get_rates` is monkeypatched, so nothing here
touches the network or the database - the same approach as
tests/ai/test_currency_conversion.py, which covers the BNR client itself
(parsing, the multiplier, cache/staleness). These tests are about what the
transfer path DOES with a rate, not about where the rate came from.

THE NUMBER THAT MATTERS in this file is the minor-unit one. BNR quotes per
major unit and every amount in the system is an integer of minor units, so a
factor-of-100 slip is a silent 100x error in a real transfer. Several tests
below assert a hand-calculated integer rather than recomputing the formula
under test.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.ai.context import Context
from app.ai.schemas import ToolCall
from app.ai.tools.propose_tools import ProposeTransferTool
from app.core import bnr_client, fx
from tests.ai.conftest import OWNED_ACCOUNT_IDS, TEST_USER_ID

RATE_DATE = date(2026, 8, 27)

#: RON per one unit, exactly as `BnrRates` stores them (multiplier already
#: divided out). HUF is quoted by BNR per 100 units, so a per-unit HUF rate is
#: a small number - that is the whole point of the HUF case below.
RATES = {
    "EUR": Decimal("4.9600"),
    "USD": Decimal("4.0000"),
    "HUF": Decimal("0.0125"),
}


def _rates() -> bnr_client.BnrRates:
    return bnr_client.BnrRates(
        rates=dict(RATES), published_on=RATE_DATE, fetched_at=datetime.now(UTC)
    )


# ---------------------------------------------------------------------------
# A supabase double: two accounts in whatever currencies a test asks for, and
# a source balance big enough that the funds check never gets in the way.
# ---------------------------------------------------------------------------

FROM_ID = OWNED_ACCOUNT_IDS[0]
TO_ID = OWNED_ACCOUNT_IDS[1]

#: The ids the tools address accounts BY (above) are opaque strings the
#: Context vouches for; the `id` a row carries is a real UUID, because
#: `_insufficient_funds_error` parses it before asking the ledger. Same split
#: the shared conftest makes with STUB_ACCOUNT_ROW_ID.
FROM_ROW_ID = "aaaaaaaa-0000-0000-0000-000000000001"
TO_ROW_ID = "aaaaaaaa-0000-0000-0000-000000000002"


@pytest.fixture
def context() -> Context:
    """Overrides the shared fixture: a propose_* tool needs a conversation to
    hang the proposal off, and the shared one deliberately has none."""
    return Context(
        user_id=TEST_USER_ID,
        account_ids=OWNED_ACCOUNT_IDS,
        conversation_id="cccccccc-0000-0000-0000-000000000001",
    )


class _Query:
    def __init__(self, rows, inserted):
        self._rows = rows
        self._inserted = inserted
        self._row = None
        self._insert_payload = None

    def eq(self, column, value):
        if column == "id":
            self._row = self._rows.get(value)
        return self

    def insert(self, payload):
        self._insert_payload = payload
        return self

    def __getattr__(self, _name):
        return lambda *a, **kw: self

    async def execute(self):
        if self._insert_payload is not None:
            self._inserted.append(self._insert_payload)
            return SimpleNamespace(data=[{**self._insert_payload, "id": "prop-1"}])
        return SimpleNamespace(data=self._row)


class _Supabase:
    def __init__(self, from_currency: str, to_currency: str, balance: int = 10**9):
        self.rows = {
            FROM_ID: {
                "id": FROM_ROW_ID,
                "name": "Cont Curent",
                "currency": from_currency,
            },
            TO_ID: {
                "id": TO_ROW_ID,
                "name": "Cont Destinație",
                "currency": to_currency,
            },
        }
        self.balance = balance
        self.inserted: list[dict] = []

    def table(self, *_a, **_kw):
        return _Query(self.rows, self.inserted)

    def rpc(self, _name, _params):
        balance = self.balance

        class _Rpc:
            async def execute(self):
                return SimpleNamespace(data=balance)

        return _Rpc()


@pytest.fixture(autouse=True)
def _no_real_bnr(monkeypatch):
    """Every test in this file states its own BNR behaviour. Failing loudly by
    default means a test that forgets to can never quietly hit the network."""

    async def _refuse(*_a, **_kw):
        raise AssertionError("get_rates was called without the test arranging it")

    monkeypatch.setattr(bnr_client, "get_rates", _refuse)


def _serve(monkeypatch, *, stale: bool = False):
    calls: list[int] = []

    async def _get_rates(**_kw):
        calls.append(1)
        return _rates(), stale

    monkeypatch.setattr(bnr_client, "get_rates", _get_rates)
    return calls


def _fail(monkeypatch, exc: Exception):
    async def _get_rates(**_kw):
        raise exc

    monkeypatch.setattr(bnr_client, "get_rates", _get_rates)


async def _propose(supabase, context, amount_minor: int):
    return await ProposeTransferTool(supabase).execute(
        ToolCall(
            id="c1",
            name="propose_transfer",
            arguments={
                "from_account_id": FROM_ID,
                "to_account_id": TO_ID,
                "amount_minor": amount_minor,
            },
        ),
        context,
    )


# ---------------------------------------------------------------------------
# The unit boundary, asserted against hand-calculated integers.
# ---------------------------------------------------------------------------


def test_the_minor_unit_conversion_is_a_hand_checked_integer():
    """500 EUR at 4.96 RON/EUR is 2480.00 RON.

        500 EUR            = 50_000 minor
        500 x 4.96         = 2480.00 RON
        2480.00 RON        = 248_000 minor

    Written out as literals rather than recomputed from `rate`, because
    recomputing it would reproduce whatever mistake `convert_minor` makes.
    """
    rate = fx.rate_between(_rates(), "EUR", "RON")
    assert rate == Decimal("4.9600")
    assert fx.convert_minor(50_000, rate) == 248_000


def test_ron_to_eur_is_the_inverse_direction():
    """248_000 RON minor units back to EUR is 50_000 EUR minor units."""
    rate = fx.rate_between(_rates(), "RON", "EUR")
    assert fx.convert_minor(248_000, rate) == 50_000


def test_a_cross_rate_never_passes_through_a_rounded_ron_amount():
    """EUR->USD: 4.96 / 4.00 = 1.24, so 100 EUR is 124 USD.

    Both sides are quoted against RON, and the ratio is taken in exact
    Decimal - RON is never materialised as a rounded intermediate amount.
    """
    rate = fx.rate_between(_rates(), "EUR", "USD")
    assert rate == Decimal("1.24")
    assert fx.convert_minor(10_000, rate) == 12_400


def test_huf_is_not_off_by_the_bnr_multiplier():
    """BNR quotes HUF per 100 units; `BnrRates` stores it per one.

    100_000 HUF (10_000_000 minor) at 0.0125 RON/HUF is 1250.00 RON, i.e.
    125_000 minor. Taking BNR's raw 1.2500 instead would give 100x that -
    which is exactly the kind of error this test exists to catch.
    """
    rate = fx.rate_between(_rates(), "HUF", "RON")
    assert fx.convert_minor(10_000_000, rate) == 125_000


def test_an_unlisted_currency_is_refused_not_guessed():
    with pytest.raises(fx.UnsupportedCurrencyError):
        fx.rate_between(_rates(), "EUR", "XYZ")


# ---------------------------------------------------------------------------
# propose_transfer: the proposal the user is shown.
# ---------------------------------------------------------------------------


async def test_a_cross_currency_proposal_carries_both_amounts_and_the_rate(
    context, monkeypatch
):
    _serve(monkeypatch)
    supabase = _Supabase("EUR", "RON")

    result = await _propose(supabase, context, 50_000)

    assert result.ok is True, result.error
    summary = result.data["summary"]
    # Both amounts, the rate, and BNR's own publication date - the user is
    # agreeing to the number that leaves AND the number that arrives.
    assert "500,00 EUR" in summary
    assert "2.480,00 RON" in summary
    assert "4,9600" in summary
    assert "27.08.2026" in summary

    (row,) = supabase.inserted
    assert row["original_amount_minor"] == 50_000
    assert row["original_currency"] == "EUR"
    assert row["converted_amount_minor"] == 248_000
    assert row["converted_currency"] == "RON"
    # A STRING, not a float: JSON has no decimal type, and this is the one
    # place the rate could stop being exact. Postgres parses it back into an
    # unbounded NUMERIC regardless of how many trailing zeros the string has.
    assert Decimal(row["exchange_rate"]) == Decimal("4.9600")
    assert row["exchange_rate_date"] == "2026-08-27"


async def test_a_same_currency_proposal_never_calls_bnr(context, monkeypatch):
    """The happy path is untouched: no fetch, no cache read, and none of the
    six conversion columns in the insert."""
    calls = _serve(monkeypatch)
    supabase = _Supabase("RON", "RON")

    result = await _propose(supabase, context, 50_000)

    assert result.ok is True, result.error
    assert calls == []
    (row,) = supabase.inserted
    assert set(row) == {
        "user_id",
        "conversation_id",
        "proposal_type",
        "payload",
        "summary",
    }
    assert "cursul BNR" not in row["summary"]


async def test_a_cold_cache_refuses_rather_than_inventing_a_rate(context, monkeypatch):
    _fail(monkeypatch, bnr_client.BnrUnavailableError("boom"))
    supabase = _Supabase("EUR", "RON")

    result = await _propose(supabase, context, 50_000)

    assert result.ok is False
    assert "BNR nu este disponibil" in (result.error or "")
    # No proposal at all - not one with a made-up number on it.
    assert supabase.inserted == []


async def test_an_unlisted_currency_refuses_at_proposal_time(context, monkeypatch):
    _serve(monkeypatch)
    supabase = _Supabase("EUR", "XYZ")

    result = await _propose(supabase, context, 50_000)

    assert result.ok is False
    assert "XYZ" in (result.error or "")
    assert supabase.inserted == []


async def test_a_stale_rate_is_used_but_the_user_is_told(context, monkeypatch):
    """BNR unreachable, cache warm. The transfer is still possible - refusing
    would be worse - but the summary says so, and still names the date the
    rate was published."""
    _serve(monkeypatch, stale=True)
    supabase = _Supabase("EUR", "RON")

    result = await _propose(supabase, context, 50_000)

    assert result.ok is True, result.error
    summary = result.data["summary"]
    assert "2.480,00 RON" in summary
    assert "27.08.2026" in summary
    assert "ultimul curs cunoscut" in summary
    # Stale or not, the numbers written down are the numbers shown.
    (row,) = supabase.inserted
    assert row["converted_amount_minor"] == 248_000


async def test_the_model_cannot_override_the_rate_by_naming_a_currency(
    context, monkeypatch
):
    """`currency` is advisory. A model insisting on "RON" for a EUR source
    account changes nothing about what is converted or in which direction."""
    _serve(monkeypatch)
    supabase = _Supabase("EUR", "RON")

    result = await ProposeTransferTool(supabase).execute(
        ToolCall(
            id="c1",
            name="propose_transfer",
            arguments={
                "from_account_id": FROM_ID,
                "to_account_id": TO_ID,
                "amount_minor": 50_000,
                "currency": "RON",
            },
        ),
        context,
    )

    assert result.ok is True, result.error
    (row,) = supabase.inserted
    assert row["original_currency"] == "EUR"
    assert row["converted_amount_minor"] == 248_000
    assert row["payload"]["currency"] == "EUR"


# ---------------------------------------------------------------------------
# confirm: executes the LOCKED numbers, and never re-fetches.
# ---------------------------------------------------------------------------


def _locked_proposal(**overrides) -> dict:
    proposal = {
        "id": "11111111-1111-1111-1111-111111111111",
        "proposal_type": "transfer",
        "payload": {
            "from_account_id": "22222222-2222-2222-2222-222222222222",
            "to_account_id": "33333333-3333-3333-3333-333333333333",
            "amount_minor": 50_000,
            "currency": "EUR",
            "description": None,
        },
        "original_currency": "EUR",
        "original_amount_minor": 50_000,
        "converted_currency": "RON",
        "converted_amount_minor": 248_000,
        # PostgREST hands NUMERIC back as a string.
        "exchange_rate": "4.9600",
        "exchange_rate_date": "2026-08-27",
    }
    proposal.update(overrides)
    return proposal


def _accounts(from_currency: str, to_currency: str):
    rows = {
        "22222222-2222-2222-2222-222222222222": {
            "id": "22222222-2222-2222-2222-222222222222",
            "name": "Cont Euro",
            "currency": from_currency,
        },
        "33333333-3333-3333-3333-333333333333": {
            "id": "33333333-3333-3333-3333-333333333333",
            "name": "Cont Curent",
            "currency": to_currency,
        },
    }

    async def _get_account(_supabase, _user, account_id):
        return rows[str(account_id)]

    return _get_account


async def test_confirm_executes_the_amount_that_was_locked(monkeypatch):
    """THE central guarantee. The user confirmed 248_000 RON minor units, so
    248_000 is what is credited - even if BNR has moved since."""
    from app.modules.chat import proposals_service

    captured = {}

    async def _create_fx_transfer(_supabase, _user, **kwargs):
        captured.update(kwargs)
        return {"id": "t-1"}

    async def _refuse_rates(*_a, **_kw):
        raise AssertionError("confirm must not re-fetch the rate")

    monkeypatch.setattr(proposals_service, "create_fx_transfer", _create_fx_transfer)
    monkeypatch.setattr(bnr_client, "get_rates", _refuse_rates)

    result = await proposals_service._execute(None, None, _locked_proposal(), "face")

    assert result == {"id": "t-1"}
    assert captured["from_amount_minor"] == 50_000
    assert captured["to_amount_minor"] == 248_000
    assert captured["exchange_rate"] == Decimal("4.9600")
    assert captured["exchange_rate_date"] == date(2026, 8, 27)
    # The proposal id is the idempotency key, and step-up already happened.
    assert captured["idempotency_key"] == "11111111-1111-1111-1111-111111111111"
    assert captured["proposal_pre_authorized"] is True


async def test_confirm_still_uses_the_plain_path_for_a_same_currency_transfer(
    monkeypatch,
):
    from app.modules.chat import proposals_service

    called = []

    async def _create_transfer(_supabase, _user, payload, key, **kwargs):
        called.append((payload.amount_minor, payload.currency, key, kwargs))
        return {"id": "t-2"}

    async def _refuse_fx(*_a, **_kw):
        raise AssertionError("a same-currency transfer must not go through FX")

    monkeypatch.setattr(proposals_service, "create_transfer", _create_transfer)
    monkeypatch.setattr(proposals_service, "create_fx_transfer", _refuse_fx)

    proposal = {
        "id": "44444444-4444-4444-4444-444444444444",
        "proposal_type": "transfer",
        "payload": {
            "from_account_id": "22222222-2222-2222-2222-222222222222",
            "to_account_id": "33333333-3333-3333-3333-333333333333",
            "amount_minor": 50_000,
            "currency": "RON",
        },
    }
    assert await proposals_service._execute(None, None, proposal, "face") == {"id": "t-2"}
    assert called[0][:3] == (
        50_000,
        "RON",
        "44444444-4444-4444-4444-444444444444",
    )


async def test_a_proposal_read_before_the_migration_is_applied_still_works(monkeypatch):
    """The six columns are read with `.get`, so a row that predates
    0023 (or is read from a database where it hasn't been applied) takes the
    ordinary path instead of raising KeyError."""
    from app.modules.chat import proposals_service

    assert proposals_service._locked_conversion({"proposal_type": "transfer"}) is None


async def test_confirm_lets_a_locked_cross_currency_proposal_through(monkeypatch):
    from app.modules.chat import proposals_service

    monkeypatch.setattr(proposals_service, "get_account", _accounts("EUR", "RON"))

    # No exception is the assertion.
    await proposals_service._assert_still_executable(None, None, _locked_proposal())


async def test_confirm_refuses_when_an_account_changed_currency(monkeypatch):
    """The conversion was calculated EUR->RON. If the source account is now
    USD, the figure the user approved describes nothing real."""
    from app.core.exceptions import CurrencyMismatchError
    from app.modules.chat import proposals_service

    monkeypatch.setattr(proposals_service, "get_account", _accounts("USD", "RON"))

    with pytest.raises(CurrencyMismatchError) as exc:
        await proposals_service._assert_still_executable(None, None, _locked_proposal())

    assert "s-au schimbat" in str(exc.value)


async def test_confirm_refuses_a_conversion_whose_accounts_now_agree(monkeypatch):
    """Both accounts read RON now, but the proposal carries an EUR->RON
    conversion. Executing the converted figure would move the wrong amount."""
    from app.core.exceptions import CurrencyMismatchError
    from app.modules.chat import proposals_service

    monkeypatch.setattr(proposals_service, "get_account", _accounts("RON", "RON"))

    with pytest.raises(CurrencyMismatchError) as exc:
        await proposals_service._assert_still_executable(None, None, _locked_proposal())

    assert "schimb valutar" in str(exc.value)
