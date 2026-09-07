"""Pure-logic tests for the few-shot spending-category classifier: prompt
construction and response parsing. No DB, no provider - see
app/ai/tools/insights/categorize_transactions.py.
"""

from __future__ import annotations

import json

from app.ai.tools.insights.categorize_transactions import (
    _VALID_CATEGORIES,
    CATEGORY_KEYWORDS,
    FEW_SHOT_EXAMPLES,
    UNCATEGORIZED,
    _build_classification_messages,
    _normalize_description,
    _parse_classification_response,
)


def test_normalize_description_lowercases_and_collapses_whitespace():
    assert _normalize_description("  Netflix.com   AMSTERDAM  ") == "netflix.com amsterdam"


def test_normalize_description_is_case_insensitive_alias():
    assert _normalize_description("NETFLIX.COM") == _normalize_description("netflix.com")


def test_normalize_description_truncates_to_255_chars():
    assert len(_normalize_description("x" * 500)) == 255


def test_valid_categories_includes_every_keyword_category_plus_uncategorized():
    assert _VALID_CATEGORIES == {*CATEGORY_KEYWORDS, UNCATEGORIZED}


def test_build_classification_messages_states_the_closed_vocabulary():
    messages = _build_classification_messages(["some new merchant"])

    system = messages[0]
    assert system.role == "system"
    for category in CATEGORY_KEYWORDS:
        assert category in system.content
    assert UNCATEGORIZED in system.content


def test_build_classification_messages_includes_every_few_shot_example_as_a_turn_pair():
    messages = _build_classification_messages(["some new merchant"])

    # One system message, then a user/assistant pair per example, then the
    # final "classify these" user turn.
    assert len(messages) == 1 + 2 * len(FEW_SHOT_EXAMPLES) + 1
    for i, (description, category) in enumerate(FEW_SHOT_EXAMPLES):
        user_turn = messages[1 + 2 * i]
        assistant_turn = messages[2 + 2 * i]
        assert user_turn.role == "user"
        assert description in user_turn.content
        assert assistant_turn.role == "assistant"
        assert json.loads(assistant_turn.content) == {description: category}


def test_build_classification_messages_lists_every_requested_description_last():
    descriptions = ["merchant one", "merchant two"]
    messages = _build_classification_messages(descriptions)

    final = messages[-1]
    assert final.role == "user"
    for description in descriptions:
        assert description in final.content


def test_parse_classification_response_reads_valid_json():
    descriptions = ["netflix.com"]
    raw = json.dumps({"netflix.com": "Divertisment"})

    assert _parse_classification_response(raw, descriptions) == {"netflix.com": "Divertisment"}


def test_parse_classification_response_strips_markdown_fences():
    descriptions = ["netflix.com"]
    raw = "```json\n" + json.dumps({"netflix.com": "Divertisment"}) + "\n```"

    assert _parse_classification_response(raw, descriptions) == {"netflix.com": "Divertisment"}


def test_parse_classification_response_drops_hallucinated_categories():
    """A category outside the closed vocabulary is dropped, not trusted -
    the caller then falls back to UNCATEGORIZED for that entry."""
    descriptions = ["some obscure merchant"]
    raw = json.dumps({"some obscure merchant": "Crypto și NFT-uri"})

    assert _parse_classification_response(raw, descriptions) == {}


def test_parse_classification_response_ignores_descriptions_it_was_not_asked_about():
    descriptions = ["netflix.com"]
    raw = json.dumps({"netflix.com": "Divertisment", "unasked merchant": "Sănătate"})

    assert _parse_classification_response(raw, descriptions) == {"netflix.com": "Divertisment"}


def test_parse_classification_response_handles_malformed_json():
    assert _parse_classification_response("not json at all", ["x"]) == {}


def test_parse_classification_response_handles_a_json_array_instead_of_an_object():
    assert _parse_classification_response(json.dumps(["Divertisment"]), ["x"]) == {}
