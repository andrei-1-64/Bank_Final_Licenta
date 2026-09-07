"""The BNR rate client, the `convert_currency` tool, and its routing.

Entirely offline, like the rest of `tests/ai`: `httpx` is never called, and
the one test that exercises the network path monkeypatches the fetch to fail.
The fixture XML below is a trimmed copy of a real BNR document, with the two
shapes that matter kept side by side - EUR, quoted per unit, and HUF, quoted
per 100.

THE MULTIPLIER is the point of half this file. A HUF rate read straight off
the feed is 1.4592 RON, which is nonsense (a forint is worth about a penny and
a half) but a plausible-looking nonsense that no assertion about "did we get a
number back" would ever catch. `test_a_per_hundred_currency_is_not_off_by_a_
factor_of_a_hundred` is the one that would fail if someone dropped the
attribute.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.ai.agents.currency_rules import CURRENCY_ROUTING_RULES
from app.ai.context import Context
from app.ai.routing import normalise
from app.ai.schemas import ToolCall
from app.ai.tools.banking.convert_currency import ConvertCurrencyTool
from app.core import bnr_client

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

#: A real BNR document, trimmed to five currencies. EUR/USD carry no
#: `multiplier` (implicitly 1); HUF and JPY are quoted per 100.
SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<DataSet xmlns="https://www.bnr.ro/xsd">
  <Header>
    <Publisher>National Bank of Romania</Publisher>
    <PublishingDate>2026-08-26</PublishingDate>
  </Header>
  <Body>
    <Subject>Reference rates</Subject>
    <OrigCurrency>RON</OrigCurrency>
    <Cube date="2026-08-26">
      <Rate currency="EUR">5.0000</Rate>
      <Rate currency="USD">4.0000</Rate>
      <Rate currency="GBP">6.0000</Rate>
      <Rate currency="HUF" multiplier="100">1.5000</Rate>
      <Rate currency="JPY" multiplier="100">3.0000</Rate>
    </Cube>
  </Body>
</DataSet>
"""


@pytest.fixture(autouse=True)
def cold_cache():
    """Every test starts with nothing cached and leaves nothing behind.

    The cache is module-level state by design (no migration - see
    bnr_client's docstring), which is exactly the kind of thing that leaks
    between tests if nobody says otherwise.
    """
    bnr_client.reset_cache()
    yield
    bnr_client.reset_cache()


@pytest.fixture
def context() -> Context:
    """A trusted identity the tool is handed and must not use."""
    return Context(user_id="user-under-test", account_ids=("acc-owned-1",))


@pytest.fixture
def tool() -> ConvertCurrencyTool:
    return ConvertCurrencyTool()


def _call(**arguments: object) -> ToolCall:
    return ToolCall(id="call-1", name="convert_currency", arguments=arguments)


def _serve(monkeypatch, xml: str = SAMPLE_XML) -> None:
    """Make the BNR fetch succeed with `xml`, without touching the network."""

    async def _fake_fetch() -> bnr_client.BnrRates:
        return bnr_client.parse_rates(xml)

    monkeypatch.setattr(bnr_client, "_fetch_rates", _fake_fetch)


def _fail(monkeypatch) -> None:
    """Make the BNR fetch fail the way an unreachable host does."""

    async def _fake_fetch() -> bnr_client.BnrRates:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(bnr_client, "_fetch_rates", _fake_fetch)


# --------------------------------------------------------------------------
# Parsing - the multiplier
# --------------------------------------------------------------------------


def test_a_per_unit_currency_parses_at_its_quoted_value():
    rates = bnr_client.parse_rates(SAMPLE_XML)
    assert rates.rates["EUR"] == Decimal("5.0000")


def test_a_per_hundred_currency_is_not_off_by_a_factor_of_a_hundred():
    """THE bug this feature is most likely to ship with.

    BNR quotes HUF per 100 units. Reading 1.5000 as "RON per forint" makes
    every HUF answer 100x too large, in a direction that still looks like a
    number. The parser divides by the `multiplier` so nothing downstream has
    to remember the rule.
    """
    rates = bnr_client.parse_rates(SAMPLE_XML)
    assert rates.rates["HUF"] == Decimal("0.015000")
    # And emphatically not the raw quoted figure.
    assert rates.rates["HUF"] != Decimal("1.5000")


def test_the_publication_date_comes_off_the_feed():
    rates = bnr_client.parse_rates(SAMPLE_XML)
    assert rates.published_on == date(2026, 8, 26)


def test_ron_is_the_base_and_is_worth_one_of_itself():
    rates = bnr_client.parse_rates(SAMPLE_XML)
    assert rates.per_unit("RON") == Decimal(1)
    # Not because it is listed - it isn't.
    assert "RON" not in rates.rates


def test_a_currency_bnr_does_not_publish_has_no_rate():
    rates = bnr_client.parse_rates(SAMPLE_XML)
    assert rates.per_unit("XYZ") is None


def test_one_malformed_rate_does_not_cost_us_the_others():
    """Strict about what it accepts, forgiving about what it skips."""
    xml = SAMPLE_XML.replace(
        '<Rate currency="USD">4.0000</Rate>',
        '<Rate currency="USD">not-a-number</Rate>'
        '<Rate currency="TOOLONG">1.0</Rate>'
        '<Rate currency="CHF" multiplier="0">1.0</Rate>',
    )
    rates = bnr_client.parse_rates(xml)
    assert rates.rates["EUR"] == Decimal("5.0000")
    for skipped in ("USD", "TOOLONG", "CHF"):
        assert skipped not in rates.rates


def test_a_document_that_is_not_the_feed_is_rejected_outright():
    """An empty rate table must never look like a successful parse."""
    with pytest.raises(ValueError):
        bnr_client.parse_rates("<html><body>Redirected to the homepage</body></html>")


# --------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------


async def test_a_second_call_within_the_ttl_does_not_refetch(monkeypatch):
    """BNR publishes once a day; asking it per message would be rude."""
    calls = 0

    async def _counting_fetch() -> bnr_client.BnrRates:
        nonlocal calls
        calls += 1
        return bnr_client.parse_rates(SAMPLE_XML)

    monkeypatch.setattr(bnr_client, "_fetch_rates", _counting_fetch)

    await bnr_client.get_rates()
    await bnr_client.get_rates()
    assert calls == 1


async def test_an_expired_cache_refetches(monkeypatch):
    calls = 0

    async def _counting_fetch() -> bnr_client.BnrRates:
        nonlocal calls
        calls += 1
        return bnr_client.parse_rates(SAMPLE_XML)

    monkeypatch.setattr(bnr_client, "_fetch_rates", _counting_fetch)

    await bnr_client.get_rates()
    later = datetime.now(UTC) + bnr_client.CACHE_TTL + timedelta(minutes=1)
    await bnr_client.get_rates(now=later)
    assert calls == 2


async def test_a_warm_cache_survives_bnr_going_away_and_says_so(monkeypatch):
    _serve(monkeypatch)
    fresh, stale = await bnr_client.get_rates()
    assert stale is False

    _fail(monkeypatch)
    later = datetime.now(UTC) + bnr_client.CACHE_TTL + timedelta(minutes=1)
    served, stale = await bnr_client.get_rates(now=later)

    assert stale is True
    assert served.rates == fresh.rates
    assert served.published_on == date(2026, 8, 26)


async def test_a_cold_cache_and_no_bnr_is_an_error_not_a_guess(monkeypatch):
    """The one case with no honest answer. It must raise, not improvise."""
    _fail(monkeypatch)
    with pytest.raises(bnr_client.BnrUnavailableError):
        await bnr_client.get_rates()


# --------------------------------------------------------------------------
# The tool
# --------------------------------------------------------------------------


async def test_converting_into_ron_multiplies(monkeypatch, tool, context):
    _serve(monkeypatch)
    result = await tool.execute(_call(amount=100, from_currency="EUR", to_currency="RON"), context)

    assert result.ok
    assert result.data["converted_amount"] == "500.00"
    assert result.data["rate"] == "5.0000"
    assert result.data["rate_date"] == "2026-08-26"
    assert result.data["stale"] is False


async def test_converting_out_of_ron_divides(monkeypatch, tool, context):
    _serve(monkeypatch)
    result = await tool.execute(_call(amount=500, from_currency="RON", to_currency="EUR"), context)

    assert result.ok
    assert result.data["converted_amount"] == "100.00"


async def test_two_foreign_currencies_go_through_ron(monkeypatch, tool, context):
    """EUR at 5 RON and USD at 4 RON means one EUR buys 1.25 USD."""
    _serve(monkeypatch)
    result = await tool.execute(_call(amount=100, from_currency="EUR", to_currency="USD"), context)

    assert result.ok
    assert result.data["converted_amount"] == "125.00"
    assert result.data["rate"] == "1.2500"


async def test_a_per_hundred_currency_converts_at_its_per_unit_rate(monkeypatch, tool, context):
    """The multiplier trap again, this time end to end.

    10000 HUF at 1.5 RON per *hundred* forint is 150 RON. Dropping the
    multiplier would answer 15000 RON - a hundred times too much, and a
    number a reviewer skimming the output would have no reason to doubt.
    """
    _serve(monkeypatch)
    result = await tool.execute(
        _call(amount=10000, from_currency="HUF", to_currency="RON"), context
    )

    assert result.ok
    assert result.data["converted_amount"] == "150.00"
    assert result.data["converted_amount"] != "15000.00"


async def test_the_same_currency_is_a_no_op_and_needs_no_rate(monkeypatch, tool, context):
    """It answers even with BNR unreachable, because it needs no rate."""
    _fail(monkeypatch)
    result = await tool.execute(_call(amount=250, from_currency="RON", to_currency="RON"), context)

    assert result.ok
    assert result.data["converted_amount"] == "250.00"
    assert result.data["stale"] is False


async def test_an_unpublished_currency_is_refused_in_romanian(monkeypatch, tool, context):
    _serve(monkeypatch)
    result = await tool.execute(_call(amount=10, from_currency="EUR", to_currency="XYZ"), context)

    assert result.ok is False
    assert "XYZ" in result.error
    assert "BNR" in result.error


async def test_a_non_positive_amount_is_refused_by_validation(monkeypatch, tool, context):
    _serve(monkeypatch)
    result = await tool.execute(_call(amount=0, from_currency="EUR", to_currency="RON"), context)

    assert result.ok is False


async def test_a_cold_bnr_outage_says_so_rather_than_inventing_a_rate(monkeypatch, tool, context):
    """A fabricated exchange rate is indistinguishable from a real one."""
    _fail(monkeypatch)
    result = await tool.execute(_call(amount=100, from_currency="EUR", to_currency="RON"), context)

    assert result.ok is False
    assert "curs" in result.error.lower()
    # Romanian, and no number anywhere that could be mistaken for a rate.
    assert "Nu pot estima un curs." in result.error


async def test_a_stale_rate_is_still_answered_but_flagged(monkeypatch, tool, context):
    _serve(monkeypatch)
    await bnr_client.get_rates()

    _fail(monkeypatch)
    monkeypatch.setattr(bnr_client, "CACHE_TTL", timedelta(seconds=-1))
    result = await tool.execute(_call(amount=100, from_currency="EUR", to_currency="RON"), context)

    assert result.ok
    assert result.data["stale"] is True
    assert result.data["converted_amount"] == "500.00"
    assert result.data["rate_date"] == "2026-08-26"
    assert "cache" in result.data["note"]


async def test_currency_case_and_padding_do_not_matter(monkeypatch, tool, context):
    _serve(monkeypatch)
    result = await tool.execute(_call(amount=100, from_currency="eur", to_currency="ron"), context)

    assert result.ok
    assert result.data["from_currency"] == "EUR"
    assert result.data["converted_amount"] == "500.00"


async def test_the_tool_reads_no_account_and_needs_no_identity(monkeypatch, tool):
    """Conversion is pure. It must work with a Context that owns nothing.

    If this ever starts failing, someone has given a currency calculator a
    reason to touch the user's data - which is the thing worth not having.
    """
    _serve(monkeypatch)
    empty = Context(user_id="nobody", account_ids=())
    result = await tool.execute(_call(amount=100, from_currency="EUR", to_currency="RON"), empty)

    assert result.ok
    assert result.data["converted_amount"] == "500.00"


def test_the_tool_is_read_only(tool):
    assert tool.read_only is True


def test_the_tool_advertises_a_usable_spec(tool):
    """Same structural guard the other registries get: the model has to be
    shown a named function with a described object schema."""
    spec = tool.spec()

    assert spec["type"] == "function"
    assert spec["function"]["name"] == "convert_currency"
    assert spec["function"]["description"]
    assert spec["function"]["parameters"]["type"] == "object"
    assert set(spec["function"]["parameters"]["required"]) == {
        "amount",
        "from_currency",
        "to_currency",
    }


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


def _matches(message: str) -> bool:
    normalised = normalise(message)
    return any(rule.matched(normalised) for rule in CURRENCY_ROUTING_RULES)


@pytest.mark.parametrize(
    "message",
    [
        "care e cursul euro azi?",
        "vreau o conversie din euro in lei",
        "converteste 100 de dolari in lei",
        "care e rata de schimb pentru franci?",
        "cat face 100 euro in lei?",
        "cat inseamna 50 de dolari in lei",
        "curs valutar",
    ],
)
def test_a_conversion_question_is_claimed(message: str):
    assert _matches(message)


@pytest.mark.parametrize(
    "message",
    [
        # Currency names that are NOT conversion requests. These must keep
        # reaching BankingAgent's own rules.
        "trimite 100 de euro lui Andrei",
        "cati bani am in euro?",
        "am cheltuit 50 lei pe cafea",
        # Words a bare `schimb` stem would have stolen.
        "schimba limita cardului meu",
        "vreau sa schimb parola",
        # A `conver` stem would have claimed this one.
        "despre ce am vorbit in conversatia asta?",
        # Plain banking, untouched.
        "care e soldul meu?",
        "cat am cheltuit luna asta?",
    ],
)
def test_an_ordinary_banking_message_is_not_claimed(message: str):
    assert not _matches(message)


def test_the_rules_are_first_on_both_agents_that_hold_the_tool():
    """Order is load-bearing, not cosmetic.

    Insights is registered before Banking and claims `luna`, so "cursul euro
    luna asta" reaches Insights first; Banking's own `banking_keywords` would
    claim a conversion question that names a `cont`. Both are only avoided by
    these rules sitting at the front of both tuples.
    """
    from app.ai.agents.banking_agent import BANKING_ROUTING_RULES
    from app.ai.agents.insights_agent import INSIGHTS_ROUTING_RULES

    count = len(CURRENCY_ROUTING_RULES)
    assert BANKING_ROUTING_RULES[:count] == CURRENCY_ROUTING_RULES
    assert INSIGHTS_ROUTING_RULES[:count] == CURRENCY_ROUTING_RULES


def test_both_agents_that_route_here_can_actually_convert():
    """A routing rule pointing at an agent without the tool is a dead end."""
    from app.ai.providers.mock_provider import MockProvider
    from app.ai.schemas import ModelResponse
    from app.ai.service import build_banking_tools, build_insights_tools
    from tests.ai.conftest import FakeSupabase

    supabase = FakeSupabase()
    # Never called - `build_insights_tools` only forwards it to the
    # categorisation tool - but MockProvider refuses an empty script.
    provider = MockProvider([ModelResponse(text="unused")])

    for registry in (
        build_banking_tools(supabase),
        build_insights_tools(supabase, provider),
    ):
        assert registry.get("convert_currency") is not None
