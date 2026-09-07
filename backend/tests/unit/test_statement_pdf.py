"""app/modules/accounts/statement_pdf.py - pure function, no DB/network.

Covers the pagination math specifically: a statement is the one BanK PDF
that can legitimately run past a single page (see the module docstring for
why that's different from admin/document_template.py's single-page,
overflow-raises approach), so the row-budget-per-page arithmetic is what
actually needs pinning down here.
"""

from __future__ import annotations

from datetime import date

import pymupdf

from app.modules.accounts.statement_pdf import (
    _ROWS_CONTINUATION_PAGE,
    _ROWS_FIRST_PAGE,
    StatementRow,
    render_statement_pdf,
)

_ACCOUNT = dict(
    holder_name="Andrei Popescu",
    national_id="1950615123456",
    account_name="Cont Curent",
    iban="RO49AAAA1B31007593840000",
    currency="RON",
    period_start=date(2026, 1, 1),
    period_end=date(2026, 1, 31),
)


def _rows(n: int) -> list[StatementRow]:
    return [
        StatementRow(
            created_at=f"2026-01-{(i % 28) + 1:02d}T10:00:00Z",
            description=f"Tranzacție {i}",
            amount_minor=1000 + i,
            currency="RON",
        )
        for i in range(n)
    ]


def _page_count(pdf_bytes: bytes) -> int:
    return pymupdf.open(stream=pdf_bytes, filetype="pdf").page_count


def test_few_rows_fit_on_a_single_page():
    pdf_bytes = render_statement_pdf(
        **_ACCOUNT, opening_balance_minor=0, closing_balance_minor=5000, rows=_rows(3)
    )
    assert _page_count(pdf_bytes) == 1


def test_exactly_one_row_over_the_first_page_budget_spills_to_a_second_page():
    row_count = _ROWS_FIRST_PAGE + 1
    pdf_bytes = render_statement_pdf(
        **_ACCOUNT, opening_balance_minor=0, closing_balance_minor=0, rows=_rows(row_count)
    )
    assert _page_count(pdf_bytes) == 2


def test_row_budget_matches_the_actual_continuation_page_capacity():
    """Fill the first page exactly, then one continuation page exactly -
    the total must stay at 2 pages, not overflow to a 3rd from an
    off-by-one in the per-page row budget."""
    row_count = _ROWS_FIRST_PAGE + _ROWS_CONTINUATION_PAGE
    pdf_bytes = render_statement_pdf(
        **_ACCOUNT, opening_balance_minor=0, closing_balance_minor=0, rows=_rows(row_count)
    )
    assert _page_count(pdf_bytes) == 2


def test_no_transactions_renders_a_single_page_with_a_placeholder():
    pdf_bytes = render_statement_pdf(
        **_ACCOUNT, opening_balance_minor=10000, closing_balance_minor=10000, rows=[]
    )
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    assert doc.page_count == 1
    assert "Nicio tranzacție în această perioadă." in doc[0].get_text()


def test_closing_balance_and_diacritics_appear_on_the_last_page():
    pdf_bytes = render_statement_pdf(
        **_ACCOUNT, opening_balance_minor=0, closing_balance_minor=123456,
        rows=_rows(_ROWS_FIRST_PAGE + 5),
    )
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    last_page_text = doc[doc.page_count - 1].get_text()
    assert "Sold final:" in last_page_text
    assert "1234,56 RON" in last_page_text
    assert "Cont Curent" not in last_page_text  # only on page 1's info box
    assert "?" not in doc[0].get_text()  # DejaVu round-trip, same guard as the admin-doc tests


def test_long_description_is_truncated_not_overflowing_the_column():
    long_description = "O descriere foarte lungă a tranzacției " * 5
    row = StatementRow(
        created_at="2026-01-05T10:00:00Z", description=long_description,
        amount_minor=-500, currency="RON",
    )
    pdf_bytes = render_statement_pdf(
        **_ACCOUNT, opening_balance_minor=0, closing_balance_minor=-500, rows=[row]
    )
    text = pymupdf.open(stream=pdf_bytes, filetype="pdf")[0].get_text()
    assert long_description not in text
    assert "…" in text
