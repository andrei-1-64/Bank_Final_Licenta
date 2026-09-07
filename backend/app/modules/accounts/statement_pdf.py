"""Renders an account's transaction history into a PDF bank statement -
same BanK letterhead as admin-sent documents (see app/core/pdf_branding.py
and app/modules/admin/document_template.py), but with a hand-drawn,
paginated transaction table instead of free text: pymupdf has no built-in
"table" widget, and letting the table run onto a second page (unlike the
single-page admin documents) is the whole point of a statement covering a
real period, so a fixed-row-budget pagination scheme lives here instead of
an overflow-raises-an-error guard.

Pure function, no DB/network access - `accounts/statement_service.py` does
the ownership check, balance math and transaction fetch, and hands this
module plain data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import pymupdf

from app.core import pdf_branding as pb

_ROW_HEIGHT = 16
#: Reserves room, on every page, for the closing-balance line + footer that
#: only actually appear on the LAST page - simpler than tracking "is this
#: the last page" while laying out earlier pages, at the cost of a little
#: unused space at the bottom of any page that isn't the last one.
_TABLE_BOTTOM = pb.PAGE_HEIGHT - 140

_BOX_TOP = pb.TITLE_TOP + pb.TITLE_HEIGHT + 22
_BOX_HEIGHT = 140
_FIRST_PAGE_TABLE_TOP = _BOX_TOP + _BOX_HEIGHT + 25
_CONTINUATION_TABLE_TOP = 115

_ROWS_FIRST_PAGE = (_TABLE_BOTTOM - _FIRST_PAGE_TABLE_TOP) // _ROW_HEIGHT
_ROWS_CONTINUATION_PAGE = (_TABLE_BOTTOM - _CONTINUATION_TABLE_TOP) // _ROW_HEIGHT

_COL_DATE_X = pb.MARGIN
_COL_DESC_X = pb.MARGIN + 68
_COL_AMOUNT_RIGHT = pb.CONTENT_RIGHT
_COL_AMOUNT_WIDTH = 115
_COL_DESC_WIDTH = (_COL_AMOUNT_RIGHT - _COL_AMOUNT_WIDTH) - _COL_DESC_X - 8



@dataclass(frozen=True)
class StatementRow:
    """One transaction line. `amount_minor` is already SIGNED (positive =
    credit, negative = debit) - this module never has to know
    ledger_entries' 'credit'/'debit' string shape, it just checks the sign."""

    created_at: str
    description: str
    amount_minor: int
    currency: str


def _format_amount(amount_minor: int, currency: str) -> str:
    sign = "+" if amount_minor >= 0 else "-"
    return f"{sign}{abs(amount_minor) / 100:.2f} {currency}".replace(".", ",", 1)


def _truncate_to_width(text: str, *, fontsize: float, max_width: float) -> str:
    if pb.REGULAR_FONT.text_length(text, fontsize=fontsize) <= max_width:
        return text
    ellipsis = "…"
    while text and pb.REGULAR_FONT.text_length(text + ellipsis, fontsize=fontsize) > max_width:
        text = text[:-1]
    return text + ellipsis


def _draw_table_header(page: pymupdf.Page, top: float) -> None:
    page.draw_rect(
        pymupdf.Rect(pb.MARGIN, top, pb.CONTENT_RIGHT, top + _ROW_HEIGHT),
        color=None, fill=pb.TEAL_TINT,
    )
    baseline = top + _ROW_HEIGHT - 4
    page.insert_text(
        (_COL_DATE_X + 2, baseline), "Data", fontsize=8, fontname=pb.FONT_BOLD, color=pb.TEAL_DARK
    )
    page.insert_text(
        (_COL_DESC_X, baseline), "Descriere", fontsize=8, fontname=pb.FONT_BOLD, color=pb.TEAL_DARK
    )
    page.insert_textbox(
        pymupdf.Rect(
            _COL_AMOUNT_RIGHT - _COL_AMOUNT_WIDTH, top, _COL_AMOUNT_RIGHT - 2, top + _ROW_HEIGHT
        ),
        "Sumă", fontsize=8, fontname=pb.FONT_BOLD, color=pb.TEAL_DARK,
        align=pymupdf.TEXT_ALIGN_RIGHT,
    )


def _draw_table_rows(page: pymupdf.Page, rows: Sequence[StatementRow], top: float) -> None:
    for i, row in enumerate(rows):
        row_top = top + i * _ROW_HEIGHT
        if i % 2 == 1:
            page.draw_rect(
                pymupdf.Rect(pb.MARGIN, row_top, pb.CONTENT_RIGHT, row_top + _ROW_HEIGHT),
                color=None, fill=(0.97, 0.98, 0.99),
            )
        baseline = row_top + _ROW_HEIGHT - 4

        day = row.created_at[:10]
        try:
            day = date.fromisoformat(day).strftime("%d.%m.%Y")
        except ValueError:
            pass  # Keep the raw ISO date rather than fail a whole statement over one bad row.
        page.insert_text(
            (_COL_DATE_X + 2, baseline), day,
            fontsize=8, fontname=pb.FONT_REGULAR, color=pb.TEXT_DARK,
        )

        description = _truncate_to_width(
            row.description or "-", fontsize=8, max_width=_COL_DESC_WIDTH
        )
        page.insert_text(
            (_COL_DESC_X, baseline), description,
            fontsize=8, fontname=pb.FONT_REGULAR, color=pb.TEXT_DARK,
        )

        amount_color = (0.024, 0.588, 0.365) if row.amount_minor >= 0 else (0.816, 0.204, 0.204)
        page.insert_textbox(
            pymupdf.Rect(
                _COL_AMOUNT_RIGHT - _COL_AMOUNT_WIDTH, row_top,
                _COL_AMOUNT_RIGHT - 2, row_top + _ROW_HEIGHT,
            ),
            _format_amount(row.amount_minor, row.currency),
            fontsize=8, fontname=pb.FONT_REGULAR, color=amount_color,
            align=pymupdf.TEXT_ALIGN_RIGHT,
        )


def _draw_account_info_box(
    page: pymupdf.Page,
    *,
    holder_name: str,
    national_id: str | None,
    account_name: str,
    iban: str | None,
    period_start: date,
    period_end: date,
    opening_balance_minor: int,
    currency: str,
) -> None:
    box = pymupdf.Rect(pb.MARGIN, _BOX_TOP, pb.CONTENT_RIGHT, _BOX_TOP + _BOX_HEIGHT)
    page.draw_rect(box, color=pb.TEAL_DARK, fill=pb.TEAL_TINT, width=0.75)

    inner_left = pb.MARGIN + 12
    inner_right = pb.CONTENT_RIGHT - 12
    page.insert_text(
        (inner_left, _BOX_TOP + 17), "DETALII CONT",
        fontsize=8, fontname=pb.FONT_BOLD, color=pb.TEAL_DARK,
    )

    pb.insert_wrapping_field(
        page, pymupdf.Rect(inner_left, _BOX_TOP + 25, inner_right, _BOX_TOP + 25 + 28),
        "Titular:", f"{holder_name} (CNP: {national_id or '-'})",
    )
    pb.insert_wrapping_field(
        page, pymupdf.Rect(inner_left, _BOX_TOP + 55, inner_right, _BOX_TOP + 55 + 28),
        "Cont:", f"{account_name} · {iban or '-'}",
    )
    pb.insert_wrapping_field(
        page, pymupdf.Rect(inner_left, _BOX_TOP + 85, inner_right, _BOX_TOP + 85 + 16),
        "Perioadă:", f"{period_start.strftime('%d.%m.%Y')} - {period_end.strftime('%d.%m.%Y')}",
    )
    pb.insert_wrapping_field(
        page, pymupdf.Rect(inner_left, _BOX_TOP + 103, inner_right, _BOX_TOP + 103 + 16),
        "Sold inițial:", _format_amount(opening_balance_minor, currency).lstrip("+"),
    )


def render_statement_pdf(
    *,
    holder_name: str,
    national_id: str | None,
    account_name: str,
    iban: str | None,
    currency: str,
    period_start: date,
    period_end: date,
    opening_balance_minor: int,
    closing_balance_minor: int,
    rows: Sequence[StatementRow],
) -> bytes:
    reference = pb.new_reference("EXT")
    issued_at = date.today()

    if rows:
        pages_rows: list[Sequence[StatementRow]] = [rows[: _ROWS_FIRST_PAGE]]
        remaining = rows[_ROWS_FIRST_PAGE :]
        while remaining:
            pages_rows.append(remaining[: _ROWS_CONTINUATION_PAGE])
            remaining = remaining[_ROWS_CONTINUATION_PAGE :]
    else:
        pages_rows = [[]]
    page_count = len(pages_rows)

    doc = pymupdf.open()
    for page_index, page_rows in enumerate(pages_rows):
        page = doc.new_page(width=pb.PAGE_WIDTH, height=pb.PAGE_HEIGHT)
        pb.register_fonts(page)
        pb.draw_header(page, reference=reference, issued_at=issued_at)

        if page_index == 0:
            pb.draw_title(page, "Extras de cont")
            _draw_account_info_box(
                page,
                holder_name=holder_name,
                national_id=national_id,
                account_name=account_name,
                iban=iban,
                period_start=period_start,
                period_end=period_end,
                opening_balance_minor=opening_balance_minor,
                currency=currency,
            )
            table_top = _FIRST_PAGE_TABLE_TOP
        else:
            page.insert_text(
                (pb.MARGIN, 112), "EXTRAS DE CONT (continuare)",
                fontsize=11, fontname=pb.FONT_BOLD, color=pb.TEXT_DARK,
            )
            table_top = _CONTINUATION_TABLE_TOP

        _draw_table_header(page, table_top)
        if page_rows:
            _draw_table_rows(page, page_rows, table_top + _ROW_HEIGHT)
        else:
            page.insert_text(
                (pb.MARGIN + 4, table_top + _ROW_HEIGHT + 12),
                "Nicio tranzacție în această perioadă.",
                fontsize=9, fontname=pb.FONT_REGULAR, color=pb.TEXT_MUTED,
            )

        is_last_page = page_index == page_count - 1
        if is_last_page:
            summary_y = table_top + _ROW_HEIGHT + len(page_rows) * _ROW_HEIGHT + 20
            page.insert_text(
                (pb.MARGIN, summary_y), "Sold final:",
                fontsize=10, fontname=pb.FONT_BOLD, color=pb.TEXT_DARK,
            )
            label_width = pb.BOLD_FONT.text_length("Sold final:", fontsize=10)
            page.insert_text(
                (pb.MARGIN + label_width + 6, summary_y),
                _format_amount(closing_balance_minor, currency).lstrip("+"),
                fontsize=10, fontname=pb.FONT_BOLD, color=pb.TEAL_DARK,
            )

        pb.draw_footer(
            page, reference,
            note="Document informativ, generat electronic - nu necesită semnătură sau ștampilă.",
            page_num=page_index + 1, page_count=page_count,
        )

    doc.subset_fonts()
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes
