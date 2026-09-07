"""The BNR daily reference exchange rates, fetched and cached.

Banca Națională a României publishes ONE official reference rate per currency
per business day, released once (around midday, Romania time) as a small XML
document. It is not a live, streaming, or intraday feed, and treating it like
one would be both wrong and rude to the publisher: this module fetches the
day's rates, caches them in memory, and reuses that cache.

WHY THE `curs.` HOST. The commonly cited URL is
https://www.bnr.ro/nbrfxrates.xml, which today answers 302 and redirects to
the BNR homepage - following it yields ~120KB of HTML, not rates. The XML is
served at https://curs.bnr.ro/nbrfxrates.xml, which is what
`BNR_RATES_URL` points at. Both are HTTPS on 443, the only port reachable from
this network.

THE MULTIPLIER. Some currencies are quoted per 100 units rather than per one,
marked by an attribute on the element:

    <Rate currency="EUR">5.2589</Rate>                  -> 5.2589 RON per EUR
    <Rate currency="HUF" multiplier="100">1.4592</Rate> -> per *100* HUF

so the per-unit rate is `value / multiplier`. Ignoring the attribute makes
every HUF/JPY/KRW/ISK/IDR conversion wrong by exactly 100x - silently, and in
the direction that looks plausible. `parse_rates` normalises everything to a
per-ONE-unit rate at parse time, so nothing downstream has to remember this.

RON is the feed's base: every quoted rate is foreign -> RON. Converting
between two non-RON currencies goes through RON (see convert_currency).

THE FEED IS DATA, NOT INSTRUCTIONS. Only known-shaped values are lifted out of
it - a currency code matching [A-Z]{3}, a decimal rate, a positive integer
multiplier. Anything else in the document is ignored rather than interpreted,
and no part of it is ever put into a model prompt. BNR is a trusted government
publisher and we are extracting numbers, but a parser that only accepts what
it recognises costs nothing and ages better.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

import httpx

logger = logging.getLogger(__name__)

#: The XML feed. See the module docstring for why this is the `curs.` host.
BNR_RATES_URL = "https://curs.bnr.ro/nbrfxrates.xml"

#: XML namespace every element in the document carries.
_NAMESPACE = "https://www.bnr.ro/xsd"

#: The feed's base currency - every quoted rate is "this many RON per unit".
BASE_CURRENCY = "RON"

#: BNR publishes once per business day. A few hours is short enough to pick up
#: the day's publication promptly and long enough that a busy conversation
#: does not re-fetch an unchanged document per message. Correctness never
#: depends on this: a cold or expired cache just means a fetch.
CACHE_TTL = timedelta(hours=3)

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

#: An ISO-4217-shaped code. Anything else in the feed is not something this
#: module claims to understand, so it is skipped.
_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")


class BnrUnavailableError(RuntimeError):
    """BNR could not be reached and nothing usable was cached.

    Raised rather than returning a sentinel so a caller cannot mistake "no
    rates" for a rate. `convert_currency` turns it into a Romanian tool error.
    """


@dataclass(frozen=True)
class BnrRates:
    """One publication of the daily reference rates.

    `rates` maps an uppercase currency code to RON per ONE unit of it, with
    any `multiplier` already divided out. `RON` itself is not a key: it is the
    base, and its rate against itself is 1 by definition.
    """

    rates: dict[str, Decimal]
    published_on: date
    fetched_at: datetime

    def per_unit(self, currency: str) -> Decimal | None:
        """RON per one unit of `currency`, or None if the feed doesn't list it.

        `RON` answers 1 rather than None - it is the base, not a missing entry.
        """
        code = currency.strip().upper()
        if code == BASE_CURRENCY:
            return Decimal(1)
        return self.rates.get(code)


def parse_rates(xml_text: str) -> BnrRates:
    """Turn the feed into per-unit rates plus its publication date.

    Strict about the shapes it lifts out (see the module docstring) and
    forgiving about everything else: a single malformed `<Rate>` is skipped,
    not fatal, so one bad entry cannot cost us the other forty.

    Raises ValueError if the document is not parseable as the expected
    structure at all, or carries no usable rate - a caller must never be
    handed an empty rate table that looks like a successful parse.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"BNR feed is not well-formed XML: {exc}") from exc

    cube = root.find(f".//{{{_NAMESPACE}}}Cube")
    if cube is None:
        raise ValueError("BNR feed has no <Cube> element")

    published_on = _parse_date(cube.get("date"))
    if published_on is None:
        # Fall back to the header's publishing date before giving up - the two
        # agree in every observed document, and either is a real publication
        # date rather than a guess.
        header_date = root.find(f".//{{{_NAMESPACE}}}PublishingDate")
        published_on = _parse_date(header_date.text if header_date is not None else None)
    if published_on is None:
        raise ValueError("BNR feed carries no parseable publication date")

    rates: dict[str, Decimal] = {}
    for element in cube.findall(f"{{{_NAMESPACE}}}Rate"):
        parsed = _parse_rate_element(element)
        if parsed is None:
            continue
        code, per_unit = parsed
        rates[code] = per_unit

    if not rates:
        raise ValueError("BNR feed carried no usable rates")

    return BnrRates(
        rates=rates,
        published_on=published_on,
        fetched_at=datetime.now(UTC),
    )


def _parse_rate_element(element: ElementTree.Element) -> tuple[str, Decimal] | None:
    """One `<Rate>` as (code, RON per one unit), or None to skip it."""
    code = (element.get("currency") or "").strip().upper()
    if not _CURRENCY_CODE.match(code):
        return None

    try:
        value = Decimal((element.text or "").strip())
    except (InvalidOperation, ValueError):
        return None
    if value <= 0:
        return None

    # THE MULTIPLIER (see the module docstring). Absent means 1.
    raw_multiplier = element.get("multiplier")
    if raw_multiplier is None:
        multiplier = Decimal(1)
    else:
        try:
            multiplier = Decimal(raw_multiplier.strip())
        except (InvalidOperation, ValueError):
            return None
        if multiplier <= 0:
            return None

    return code, value / multiplier


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------
#
# Module-level and in-memory ON PURPOSE: caching rates in the database would
# need a migration, and migrations here are applied by hand through the
# Supabase SQL editor. Nothing about correctness depends on this cache - it is
# a politeness-to-BNR and latency optimisation, and a cold one simply fetches.
# A multi-process deployment gets one cache per process, which is fine for
# something whose contents are identical everywhere and change once a day.

_cache: BnrRates | None = None


def _cache_is_fresh(cached: BnrRates, now: datetime) -> bool:
    return now - cached.fetched_at < CACHE_TTL


def reset_cache() -> None:
    """Drop the cached rates. For tests, and for anything that needs a
    guaranteed fetch; nothing in normal operation calls this."""
    global _cache
    _cache = None


def cached_rates() -> BnrRates | None:
    """Whatever is cached right now, fresh or not. For tests and diagnostics."""
    return _cache


async def get_rates(*, now: datetime | None = None) -> tuple[BnrRates, bool]:
    """The current reference rates, and whether they are STALE.

    Returns `(rates, stale)`. `stale` means "BNR could not be reached and this
    came out of the cache instead" - it is about the FETCH failing, not about
    the rates being old. A weekend or public holiday is not stale: BNR simply
    has not published since Friday, the fetch succeeds normally, and
    `published_on` shows Friday. Callers surface that date, so an old-but-
    correct rate is visible as such without being cried wolf over.

    Raises `BnrUnavailableError` only when the fetch fails AND nothing is
    cached - the one case where there is no honest answer to give.
    """
    global _cache

    now = now or datetime.now(UTC)

    if _cache is not None and _cache_is_fresh(_cache, now):
        return _cache, False

    try:
        fetched = await _fetch_rates()
    except (httpx.HTTPError, ValueError) as exc:
        if _cache is not None:
            # Serve what we have, flagged. Better a dated rate the user is
            # told is dated than no answer - and far better than a fresh
            # guess, which is the one thing this must never produce.
            logger.warning(
                "BNR fetch failed (%s); serving cached rates published %s",
                type(exc).__name__,
                _cache.published_on,
            )
            return _cache, True
        logger.warning("BNR fetch failed (%s) with a cold cache", type(exc).__name__)
        raise BnrUnavailableError(str(exc)) from exc

    _cache = fetched
    return fetched, False


async def _fetch_rates() -> BnrRates:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(BNR_RATES_URL)
        response.raise_for_status()
    return parse_rates(response.text)
