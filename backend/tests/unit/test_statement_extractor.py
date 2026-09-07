"""parse_layout_result - pure-function tests against a hand-built fake
AnalyzeResult, no network/SDK dependency (see statement_extractor.py's
module docstring: this is exactly what it was factored out to make
possible - it has NOT been smoke-tested against a live Document
Intelligence resource or a real bank statement PDF).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.modules.documents.statement_extractor import parse_layout_result


@dataclass
class FakeCell:
    row_index: int
    column_index: int
    content: str
    kind: str | None = None


@dataclass
class FakeTable:
    row_count: int
    cells: list[FakeCell]


@dataclass
class FakeParagraph:
    content: str
    role: str | None = None


@dataclass
class FakeResult:
    tables: list[FakeTable] = field(default_factory=list)
    paragraphs: list[FakeParagraph] = field(default_factory=list)


def _header_cells(*names: str) -> list[FakeCell]:
    return [
        FakeCell(row_index=0, column_index=i, content=name, kind="columnHeader")
        for i, name in enumerate(names)
    ]


def _data_row(row_index: int, *values: str) -> list[FakeCell]:
    return [
        FakeCell(row_index=row_index, column_index=i, content=value)
        for i, value in enumerate(values)
    ]


def test_parses_a_debit_credit_table_into_signed_amounts():
    """Debit column -> negative amount, credit column -> positive amount."""
    table = FakeTable(
        row_count=3,
        cells=[
            *_header_cells("Data", "Descriere", "Debit", "Credit", "Sold"),
            *_data_row(1, "01.03.2026", "Kaufland", "150,00", "", "1.000,00"),
            *_data_row(2, "03.03.2026", "Salariu", "", "5.000,00", "6.000,00"),
        ],
    )

    statement = parse_layout_result(FakeResult(tables=[table]))

    assert len(statement.rows) == 2
    kaufland, salariu = statement.rows
    assert kaufland.amount == -150.0
    assert kaufland.description == "Kaufland"
    assert kaufland.balance_after == 1000.0
    assert salariu.amount == 5000.0
    assert salariu.balance_after == 6000.0


def test_parses_a_single_signed_amount_column():
    table = FakeTable(
        row_count=2,
        cells=[
            *_header_cells("Data", "Descriere", "Suma"),
            *_data_row(1, "01.03.2026", "Netflix", "-45.99"),
        ],
    )

    statement = parse_layout_result(FakeResult(tables=[table]))

    assert len(statement.rows) == 1
    assert statement.rows[0].amount == -45.99


def test_skips_a_row_with_no_parseable_date_instead_of_raising():
    """A malformed row must be dropped, not crash the whole extraction -
    see the module docstring's "SKIP, never raise" contract."""
    table = FakeTable(
        row_count=3,
        cells=[
            *_header_cells("Data", "Descriere", "Debit", "Credit"),
            *_data_row(1, "not-a-date", "Garbled OCR row", "10,00", ""),
            *_data_row(2, "05.03.2026", "Good row", "20,00", ""),
        ],
    )

    statement = parse_layout_result(FakeResult(tables=[table]))

    assert len(statement.rows) == 1
    assert statement.rows[0].description == "Good row"


def test_skips_a_row_with_both_debit_and_credit_populated():
    """Both columns filled on one line is an artifact (subtotal/header row
    the header-scan let through), not a real transaction."""
    table = FakeTable(
        row_count=2,
        cells=[
            *_header_cells("Data", "Descriere", "Debit", "Credit"),
            *_data_row(1, "01.03.2026", "Ambiguous", "10,00", "10,00"),
        ],
    )

    statement = parse_layout_result(FakeResult(tables=[table]))

    assert statement.rows == []


def test_ignores_a_table_whose_headers_do_not_look_like_transactions():
    """Some statements have a summary table above the transactions table -
    it must be skipped, not misread as (mostly empty) transaction rows."""
    table = FakeTable(
        row_count=2,
        cells=[
            *_header_cells("Sold initial", "Sold final"),
            *_data_row(1, "1.000,00", "2.000,00"),
        ],
    )

    statement = parse_layout_result(FakeResult(tables=[table]))

    assert statement.rows == []


def test_row_index_is_assigned_in_document_order_across_multiple_tables():
    table_a = FakeTable(
        row_count=2,
        cells=[
            *_header_cells("Data", "Descriere", "Suma"),
            *_data_row(1, "01.03.2026", "First", "-1.00"),
        ],
    )
    table_b = FakeTable(
        row_count=2,
        cells=[
            *_header_cells("Data", "Descriere", "Suma"),
            *_data_row(1, "02.03.2026", "Second", "-2.00"),
        ],
    )

    statement = parse_layout_result(FakeResult(tables=[table_a, table_b]))

    assert [r.row_index for r in statement.rows] == [0, 1]


def test_infers_period_start_and_end_from_row_dates():
    table = FakeTable(
        row_count=3,
        cells=[
            *_header_cells("Data", "Descriere", "Suma"),
            *_data_row(1, "15.03.2026", "Mid", "-1.00"),
            *_data_row(2, "01.03.2026", "First", "-1.00"),
        ],
    )

    statement = parse_layout_result(FakeResult(tables=[table]))

    assert statement.period_start.isoformat() == "2026-03-01"
    assert statement.period_end.isoformat() == "2026-03-15"


def test_guesses_bank_name_from_a_title_paragraph():
    result = FakeResult(
        tables=[],
        paragraphs=[
            FakeParagraph(content="Banca Test S.A.", role="title"),
            FakeParagraph(content="some other text", role=None),
        ],
    )

    statement = parse_layout_result(result)

    assert statement.bank_name == "Banca Test S.A."


def test_bank_name_is_none_when_no_heading_paragraph_exists():
    statement = parse_layout_result(FakeResult(tables=[], paragraphs=[]))

    assert statement.bank_name is None


def test_empty_result_yields_an_empty_statement_not_an_error():
    statement = parse_layout_result(FakeResult())

    assert statement.rows == []
    assert statement.period_start is None
    assert statement.period_end is None
