"""Spending categories from merchant name / description patterns.

Two layers, cheapest first:

1. A fixed keyword map - deterministic, explainable, free. Matching a new
   merchant is a `dict` edit, not a retrain. This alone is everything
   GET /insights/spending-by-category (the dashboard widget) ever uses -
   that endpoint must stay LLM-free (see insights/router.py's docstring),
   so it only ever reads the keyword map plus whatever's already cached
   below; it never calls the model itself.
2. For whatever the keywords miss (which would otherwise all land in
   "Altele"), a few-shot LLM classifier - see FEW_SHOT_EXAMPLES below - but
   ONLY when this module is given a `provider` to call, which today means
   only the chat tool at the bottom of this file. The chat tool already
   pays LLM latency for the user's question anyway, so classifying its
   cache-miss merchants there is free in comparison; the dashboard read
   path never pays it. Results are cached in merchant_category_cache
   (global, not per-user - a merchant's category doesn't depend on who
   paid them), so the very next dashboard load - or the next chat question,
   by any user - sees the improved category without a second LLM call.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, model_validator

from app.ai.context import Context
from app.ai.providers.base import ModelProvider, ProviderError
from app.ai.schemas import Message, ToolResult
from app.ai.tools.base import Tool
from app.ai.tools.insights._shared import day_bounds, load_rows

if TYPE_CHECKING:
    from supabase import AsyncClient

logger = logging.getLogger(__name__)

#: category -> keywords, matched case-insensitively as a substring of
#: "description reference". Order matters only in that the FIRST category
#: whose keywords match wins - keep more specific categories earlier if two
#: lists could ever overlap on the same merchant (e.g. "bolt food" must be
#: checked before plain "bolt", so a food-delivery order doesn't land in
#: Transport just because "bolt" is a substring of "bolt food").
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Facturi & Utilități": (
        "enel", "eon", "e.on", "electrica", "engie", "distrigaz", "apa nova",
        "hidroelectrica", "restart energy", "premier energy",
    ),
    "Telecomunicații": ("vodafone", "orange", "digi", "upc", "telekom"),
    "Divertisment": (
        "spotify", "netflix", "hbo", "disney", "cinema", "eventim",
        "bilete", "teatru", "twitch", "steam",
    ),
    "Mâncare & Băutură": (
        "starbucks", "mcdonald", "kfc", "restaurant", "cafea", "glovo",
        "tazz", "foodpanda", "bolt food",
    ),
    "Cumpărături alimentare": (
        "mega image", "carrefour", "lidl", "kaufland", "penny", "profi", "auchan",
    ),
    "Transport / Combustibil": (
        "omv", "petrom", "mol", "rompetrol", "uber", "bolt", "taxi", "cfr", "ratb", "stb",
    ),
    "Sănătate": (
        "catena", "sensiblu", "dona", "farmacie", "clinica", "spital", "reginamaria", "medlife",
    ),
    "Electronice": ("emag", "e-mag", "altex", "pcgarage", "media galaxy"),
    "Îmbrăcăminte": ("zara", "h&m", "bershka", "pull&bear", "decathlon"),
    "Educație": ("udemy", "coursera", "scoala", "școala", "universitate"),
    "Locuință & Amenajări": ("chirie", "ikea", "dedeman", "leroy merlin", "hornbach"),
    "Asigurări": ("allianz", "groupama", "generali", "nn asigurari"),
    "Transferuri": ("revolut", "transfer"),
}
UNCATEGORIZED = "Altele"


def _categorize(text: str) -> str:
    lowered = text.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return category
    return UNCATEGORIZED


def _normalize_description(text: str) -> str:
    """Same normalization the cache key and the LLM prompt both use, so a
    lookup always matches what was written under - trimmed and lowercased,
    not stemmed or fuzzy: "Netflix.com" and "NETFLIX.COM" share a cache
    entry, "Netflix" and "Netflix Premium" deliberately do not."""
    return " ".join(text.split()).lower()[:255]


#: One or two demonstrations per keyword category above (few-shot prompting:
#: showing the model worked examples of the exact input -> output mapping we
#: want, rather than just describing the task, is what makes its answers
#: consistent with OUR taxonomy instead of one it invents on the spot). Kept
#: in the same order as CATEGORY_KEYWORDS so the two are easy to compare and
#: keep in sync - every key in that dict should have at least one example
#: here, and this never includes UNCATEGORIZED itself: "Altele" is what the
#: model should reach for on its own when nothing else genuinely fits, not
#: something to demonstrate examples of.
FEW_SHOT_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("plata factura enel energie mar 2026", "Facturi & Utilități"),
    ("hidroelectrica plata furnizare", "Facturi & Utilități"),
    ("abonament vodafone romania", "Telecomunicații"),
    ("digi rcs&rds servicii internet", "Telecomunicații"),
    ("netflix.com amsterdam nl", "Divertisment"),
    ("eventim bilete concert cluj", "Divertisment"),
    ("glovo comanda restaurant", "Mâncare & Băutură"),
    ("starbucks coffee bucuresti", "Mâncare & Băutură"),
    ("mega image cumparaturi", "Cumpărături alimentare"),
    ("kaufland romania sc", "Cumpărături alimentare"),
    ("uber trip bucuresti", "Transport / Combustibil"),
    ("petrom statie carburant", "Transport / Combustibil"),
    ("farmacia catena nr 42", "Sănătate"),
    ("clinica medlife consultatie", "Sănătate"),
    ("emag.ro comanda 123456", "Electronice"),
    ("altex electrocasnice", "Electronice"),
    ("zara magazin haine", "Îmbrăcăminte"),
    ("decathlon echipament sport", "Îmbrăcăminte"),
    ("udemy curs online python", "Educație"),
    ("taxa universitate semestru", "Educație"),
    ("ikea mobila living", "Locuință & Amenajări"),
    ("chirie apartament august", "Locuință & Amenajări"),
    ("allianz tiriac asigurare auto", "Asigurări"),
    ("revolut transfer catre ion popescu", "Transferuri"),
)

_VALID_CATEGORIES = frozenset({*CATEGORY_KEYWORDS, UNCATEGORIZED})


def _build_classification_messages(descriptions: list[str]) -> list[Message]:
    """Builds the few-shot prompt: a system message stating the fixed
    category vocabulary, the worked examples as alternating user/assistant
    turns (closer to how the model actually experienced them at
    pre-training time than dumping the same examples into one block of
    text), then one final user turn asking it to classify the real,
    never-seen-before descriptions - as a single batched request rather
    than one call per description."""
    category_list = ", ".join(sorted(_VALID_CATEGORIES))
    messages: list[Message] = [
        Message(
            role="system",
            content=(
                "Clasifici descrieri de tranzacții bancare într-o categorie de "
                f"cheltuieli. Categoriile valide sunt EXACT acestea, fără altele: "
                f"{category_list}. Dacă nicio categorie nu se potrivește clar, "
                f'alege "{UNCATEGORIZED}". Răspunzi DOAR cu un obiect JSON care '
                "mapează fiecare descriere primită (exact cum a fost dată) la "
                "categoria ei - fără text în plus, fără explicații."
            ),
        )
    ]
    for description, category in FEW_SHOT_EXAMPLES:
        messages.append(Message(role="user", content=f'Clasifică: "{description}"'))
        messages.append(Message(role="assistant", content=json.dumps({description: category})))

    numbered = "\n".join(f'- "{d}"' for d in descriptions)
    messages.append(
        Message(
            role="user",
            content=f"Clasifică fiecare dintre aceste descrieri noi:\n{numbered}",
        )
    )
    return messages


def _parse_classification_response(raw_text: str, descriptions: list[str]) -> dict[str, str]:
    """Best-effort JSON parse of the model's reply, defensive against both a
    malformed response and a hallucinated category: either failure mode
    just drops that one description (it falls back to UNCATEGORIZED
    upstream) rather than raising and losing the whole batch."""
    cleaned = raw_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Few-shot category classifier returned unparseable JSON")
        return {}
    if not isinstance(parsed, dict):
        return {}

    result: dict[str, str] = {}
    for description in descriptions:
        category = parsed.get(description)
        if isinstance(category, str) and category in _VALID_CATEGORIES:
            result[description] = category
    return result


async def _get_cached_categories(
    supabase: AsyncClient, description_keys: list[str]
) -> dict[str, str]:
    if not description_keys:
        return {}
    resp = (
        await supabase.table("merchant_category_cache")
        .select("description_key, category")
        .in_("description_key", description_keys)
        .execute()
    )
    return {row["description_key"]: row["category"] for row in resp.data}


async def _cache_categories(supabase: AsyncClient, categories_by_key: dict[str, str]) -> None:
    if not categories_by_key:
        return
    await supabase.table("merchant_category_cache").upsert(
        [
            {"description_key": key, "category": category}
            for key, category in categories_by_key.items()
        ],
        on_conflict="description_key",
    ).execute()


async def _classify_uncategorized_with_llm(
    supabase: AsyncClient, provider: ModelProvider, texts: list[str]
) -> dict[str, str]:
    """Resolves a batch of raw (unnormalized) transaction texts that the
    keyword matcher couldn't place, via cache-then-LLM - returns only the
    entries it managed to improve on UNCATEGORIZED; callers keep whatever
    isn't in the returned mapping as "Altele". Never raises: a provider
    outage degrades to "no improvement this time", not a broken widget."""
    key_by_text = {text: _normalize_description(text) for text in texts}
    unique_keys = sorted(set(key_by_text.values()))

    cached = await _get_cached_categories(supabase, unique_keys)
    missing_keys = [key for key in unique_keys if key not in cached]

    if missing_keys:
        try:
            response = provider.complete(_build_classification_messages(missing_keys))
            fresh = _parse_classification_response(response.text or "", missing_keys)
        except ProviderError:
            logger.warning("Few-shot category classifier call failed; leaving as Altele")
            fresh = {}
        if fresh:
            await _cache_categories(supabase, fresh)
        cached.update(fresh)

    return {text: cached[key] for text, key in key_by_text.items() if key in cached}


async def categorize_spending(
    supabase: AsyncClient,
    context: Context,
    *,
    start_date: date,
    end_date: date,
    account_id: str | None = None,
    provider: ModelProvider | None = None,
) -> dict[str, Any]:
    """The actual work, shared by the chat tool below and the
    /insights/spending-by-category REST endpoint (see
    app/modules/insights/router.py) - the dashboard widget needs this
    synchronously, without going through an LLM tool-call loop just to
    render a chart on page load.

    `provider` is the module-docstring's opt-in: omit it (the REST endpoint
    always does) to get pure keyword-plus-cache categorization with zero
    model calls; pass it (only the chat tool does) to also run the few-shot
    classifier on this call's leftover "Altele" entries, caching whatever it
    improves for every future caller, provider or not."""
    date_from, date_to = day_bounds(start_date, end_date)
    entries = await load_rows(
        supabase, context, date_from=date_from, date_to=date_to, account_id=account_id
    )
    spending = [entry for entry in entries if entry.direction.value == "debit"]

    entry_text = {str(entry.id): f"{entry.description} {entry.reference}" for entry in spending}
    entry_category = {entry_id: _categorize(text) for entry_id, text in entry_text.items()}

    if provider is not None:
        uncategorized_texts = [
            text for entry_id, text in entry_text.items() if entry_category[entry_id] == UNCATEGORIZED
        ]
        improved = await _classify_uncategorized_with_llm(supabase, provider, uncategorized_texts)
        for entry_id, text in entry_text.items():
            if entry_category[entry_id] == UNCATEGORIZED and text in improved:
                entry_category[entry_id] = improved[text]

    buckets: dict[str, dict[str, int]] = {}
    row_categories: dict[str, str] = {}
    for entry in spending:
        category = entry_category[str(entry.id)]
        row_categories[str(entry.id)] = category
        bucket = buckets.setdefault(category, {"count": 0, "total_minor": 0})
        bucket["count"] += 1
        bucket["total_minor"] += entry.amount_minor

    if context.statement_id is not None:
        # Persist onto the extracted rows - NEVER onto the ledger, see
        # statements/service.py's module docstring. Sequential writes:
        # PostgREST has no per-row bulk-update-by-value primitive, and a
        # statement's row count is small enough (a few hundred at most)
        # that this isn't worth a bespoke batching path.
        from app.modules.statements import service as statements_service

        for row_id, category in row_categories.items():
            await statements_service.set_row_category(
                supabase, context.user_id, context.statement_id, row_id, category
            )

    total_minor = sum(bucket["total_minor"] for bucket in buckets.values())
    categories: list[dict[str, Any]] = [
        {
            "name": name,
            "count": bucket["count"],
            "total_minor": bucket["total_minor"],
            "percentage": (
                round(bucket["total_minor"] / total_minor * 100, 2) if total_minor else 0.0
            ),
        }
        for name, bucket in buckets.items()
    ]
    categories.sort(key=lambda c: int(c["total_minor"]), reverse=True)

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "total_transactions": len(spending),
        "categories": categories,
        "uncategorized_count": buckets.get(UNCATEGORIZED, {}).get("count", 0),
    }


class CategorizeTransactionsInput(BaseModel):
    """Arguments the model may supply. Validated, but still untrusted."""

    start_date: date = Field(
        description="First day to include, inclusive. ISO format, e.g. 2026-08-01."
    )
    end_date: date = Field(
        description="Last day to include, inclusive. ISO format, e.g. 2026-08-31."
    )
    account_id: str | None = Field(
        default=None,
        description=(
            "Optional. Restrict to one of the user's own accounts. Omit it to "
            "categorize spending across all of their accounts."
        ),
    )

    @model_validator(mode="after")
    def _range_is_ordered(self) -> CategorizeTransactionsInput:
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class CategorizeTransactionsTool(Tool):
    name = "categorize_transactions"
    description = (
        "Break the user's spending (debit transactions only) down into "
        "categories - groceries, subscriptions, transport, etc. - based on "
        "merchant name patterns. Use this for 'what did I spend on X' or "
        "'spending by category' questions. Percentages are of total spending "
        "in the range, not of all transactions."
    )
    input_schema = CategorizeTransactionsInput
    read_only = True

    def __init__(self, supabase: AsyncClient, provider: ModelProvider) -> None:
        """`supabase` is the shared process-wide client (see
        db/supabase_client). `provider` feeds the few-shot classifier for
        whatever the keyword map alone can't place (see categorize_spending)
        - this tool call already pays LLM latency for the user's question,
        so improving its own "Altele" leftovers costs nothing extra, unlike
        the REST endpoint this shares its logic with."""
        self._supabase = supabase
        self._provider = provider

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, CategorizeTransactionsInput)

        data = await categorize_spending(
            self._supabase,
            context,
            start_date=validated_input.start_date,
            end_date=validated_input.end_date,
            account_id=validated_input.account_id,
            provider=self._provider,
        )
        return ToolResult(name=self.name, data=data)
