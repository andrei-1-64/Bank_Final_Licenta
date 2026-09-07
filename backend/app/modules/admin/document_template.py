"""Renders an admin-authored document into PDF bytes, styled to look like an
actual bank document: logo, brand color header band, a bordered "titular"
info box, justified body text and a signature/footer block. Header, footer,
title and the wrapping-field helper are shared with account statements -
see app/core/pdf_branding.py.

Pure function, no DB/network access - easy to unit test, and keeps
`admin/service.py` focused on orchestration (load user, create conversation,
store the document) rather than layout.

Single A4 page, deliberately not paginated: pymupdf's `insert_textbox`
reports overflow as a negative "missing space" number but does not report
which characters fit, so splitting text correctly across pages would need
its own line-wrapping logic. Given a page comfortably holds ~2000 characters
of body text at the font size used here, `AdminDocumentSendRequest.body` in
admin/schemas.py caps there instead - the admin gets a clear 422 telling
them to shorten the text rather than a document that silently loses its
ending.
"""

from __future__ import annotations

from datetime import date

import pymupdf

from app.core import pdf_branding as pb
from app.core.exceptions import ValidationError

_BOX_TOP = pb.TITLE_TOP + pb.TITLE_HEIGHT + 22
_BOX_HEIGHT = 155
_BODY_TOP = _BOX_TOP + _BOX_HEIGHT + 25


def _draw_holder_info_box(
    page: pymupdf.Page,
    *,
    first_name: str,
    last_name: str,
    national_id: str | None,
    address: str | None,
    issued_at: date,
) -> None:
    box = pymupdf.Rect(pb.MARGIN, _BOX_TOP, pb.CONTENT_RIGHT, _BOX_TOP + _BOX_HEIGHT)
    page.draw_rect(box, color=pb.TEAL_DARK, fill=pb.TEAL_TINT, width=0.75)

    inner_left = pb.MARGIN + 12
    inner_right = pb.CONTENT_RIGHT - 12
    page.insert_text(
        (inner_left, _BOX_TOP + 17), "DATE TITULAR",
        fontsize=8, fontname=pb.FONT_BOLD, color=pb.TEAL_DARK,
    )

    # Full-width, stacked rows - not a two-column "Nume / CNP" layout. A
    # fixed CNP column next to a wrapping name would still collide the
    # moment the name wraps to a second line; stacking removes that risk
    # entirely instead of bounding it.
    pb.insert_wrapping_field(
        page, pymupdf.Rect(inner_left, _BOX_TOP + 25, inner_right, _BOX_TOP + 25 + 28),
        "Nume:", f"{first_name} {last_name}",
    )
    pb.insert_wrapping_field(
        page, pymupdf.Rect(inner_left, _BOX_TOP + 55, inner_right, _BOX_TOP + 55 + 16),
        "CNP:", national_id or "-",
    )
    pb.insert_wrapping_field(
        page, pymupdf.Rect(inner_left, _BOX_TOP + 73, inner_right, _BOX_TOP + 73 + 44),
        "Adresă:", address or "-",
    )
    pb.insert_wrapping_field(
        page, pymupdf.Rect(inner_left, _BOX_TOP + 125, inner_right, _BOX_TOP + 125 + 16),
        "Data emiterii:", issued_at.strftime("%d.%m.%Y"),
    )


def _draw_signature_block(page: pymupdf.Page, *, first_name: str, last_name: str) -> None:
    y = pb.PAGE_HEIGHT - 130
    page.draw_line(
        (pb.MARGIN, y), (pb.MARGIN + 200, y), color=pb.TEXT_MUTED, width=0.75
    )
    page.insert_text(
        (pb.MARGIN, y + 14), f"Semnătură titular ({first_name} {last_name})",
        fontsize=9, fontname=pb.FONT_REGULAR, color=pb.TEXT_MUTED,
    )


def render_document_pdf(
    *,
    title: str,
    body: str,
    first_name: str,
    last_name: str,
    national_id: str | None,
    address: str | None,
) -> bytes:
    issued_at = date.today()
    reference = pb.new_reference("BK")

    doc = pymupdf.open()
    page = doc.new_page(width=pb.PAGE_WIDTH, height=pb.PAGE_HEIGHT)
    pb.register_fonts(page)

    pb.draw_header(page, reference=reference, issued_at=issued_at)
    pb.draw_title(page, title)
    _draw_holder_info_box(
        page,
        first_name=first_name,
        last_name=last_name,
        national_id=national_id,
        address=address,
        issued_at=issued_at,
    )

    body_rect = pymupdf.Rect(pb.MARGIN, _BODY_TOP, pb.CONTENT_RIGHT, pb.PAGE_HEIGHT - 145)
    overflow = page.insert_textbox(
        body_rect, body, fontsize=11, fontname=pb.FONT_REGULAR, color=pb.TEXT_DARK,
        align=pymupdf.TEXT_ALIGN_JUSTIFY,
    )
    if overflow < 0:
        raise ValidationError(
            "Textul documentului este prea lung pentru o singură pagină - scurtează-l."
        )

    _draw_signature_block(page, first_name=first_name, last_name=last_name)
    pb.draw_footer(
        page, reference,
        note="Nu necesită semnătură olografă sau ștampilă pentru a fi valid electronic.",
    )

    # Without this, both embedded DejaVu Sans files (regular + bold, ~1.4MB
    # together) ride along in full on every single document, even though a
    # one-page adeverință only ever uses a few dozen distinct glyphs from
    # them. Subsetting keeps only the glyphs actually drawn above, which is
    # the difference between a ~1.8MB PDF and one a few tens of KB.
    doc.subset_fonts()

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes
