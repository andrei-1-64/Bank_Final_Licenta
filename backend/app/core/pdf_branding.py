"""Shared BanK letterhead/branding for generated PDFs - the logo header,
footer and a couple of layout primitives every BanK-issued document reuses.
Used by app/modules/admin/document_template.py (admin-sent documents) and
app/modules/accounts/statement_pdf.py (account statements); pulled out here
once a second document type needed the exact same header/footer/title so
the two didn't drift into two slightly-different-looking "official" BanK
documents.

FONT: the base14 "helv" (Helvetica) shortcut pymupdf falls back to only
supports WinAnsiEncoding, which does NOT include ă/ș/ț (Romanian's
comma-below letters aren't in cp1252 at all - only â/î are, by accident of
overlapping with Western European accents). Rendered with "helv", "conținut"
comes out as "con?inut" (a literal question mark). DejaVu Sans covers the
full Unicode range instead, so it is bundled here as a real asset
(assets/DejaVuSans*.ttf, copied from the vision container's fonts - see
that image's Dockerfile) and embedded via `fontfile=`, not pulled from a
system font path that may not exist in this image.
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import pymupdf

from app.core.exceptions import ValidationError

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "modules" / "admin" / "assets"
LOGO_PATH = _ASSETS_DIR / "bank_logo.png"
_FONT_REGULAR_PATH = _ASSETS_DIR / "DejaVuSans.ttf"
_FONT_BOLD_PATH = _ASSETS_DIR / "DejaVuSans-Bold.ttf"

MARGIN = 50
PAGE_WIDTH, PAGE_HEIGHT = pymupdf.paper_size("a4")
CONTENT_RIGHT = PAGE_WIDTH - MARGIN

FONT_REGULAR = "dejavu"
FONT_BOLD = "dejavu-bold"

#: Real Font objects, not just the page-registered name strings above -
#: `pymupdf.get_text_length` (the module-level helper) only recognises the
#: base14 fonts, so measuring a custom embedded font's width (for a label
#: column, or to truncate a table cell to fit) needs one of these instead.
BOLD_FONT = pymupdf.Font(fontfile=str(_FONT_BOLD_PATH))
REGULAR_FONT = pymupdf.Font(fontfile=str(_FONT_REGULAR_PATH))

#: BanK's brand teal (frontend/style.css --primary-teal: #2DD4BF), and two
#: shades derived from it for use on a WHITE page - the bright original is
#: an accent-bar/border color here, not body text (too low-contrast on
#: white at text size).
TEAL = (0.176, 0.831, 0.749)
TEAL_DARK = (0.051, 0.580, 0.533)
TEAL_TINT = (0.941, 0.992, 0.980)
TEXT_DARK = (0.118, 0.161, 0.231)
TEXT_MUTED = (0.392, 0.455, 0.545)

def new_reference(prefix: str) -> str:
    """Cosmetic only - not stored or tracked anywhere else. Gives a
    document the "this is a real numbered record" look an official one
    would have, without pretending there is a registry behind it."""
    today = date.today()
    return f"{prefix}-{today.strftime('%Y%m')}-{uuid.uuid4().hex[:6].upper()}"


def register_fonts(page: pymupdf.Page) -> None:
    page.insert_font(fontname=FONT_REGULAR, fontfile=str(_FONT_REGULAR_PATH))
    page.insert_font(fontname=FONT_BOLD, fontfile=str(_FONT_BOLD_PATH))


def draw_header(page: pymupdf.Page, *, reference: str, issued_at: date) -> None:
    if LOGO_PATH.exists():
        page.insert_image(pymupdf.Rect(MARGIN, 40, MARGIN + 40, 80), filename=str(LOGO_PATH))

    page.insert_text((MARGIN + 50, 58), "BanK", fontsize=18, fontname=FONT_BOLD, color=TEAL_DARK)
    page.insert_text(
        (MARGIN + 50, 73), "CONFORTUL TĂU FINANCIAR", fontsize=7, fontname=FONT_REGULAR,
        color=TEXT_MUTED,
    )

    meta_rect = pymupdf.Rect(300, 42, CONTENT_RIGHT, 80)
    meta_text = f"Nr. document: {reference}\nData: {issued_at.strftime('%d.%m.%Y')}"
    page.insert_textbox(
        meta_rect, meta_text, fontsize=9, fontname=FONT_REGULAR, color=TEXT_MUTED,
        align=pymupdf.TEXT_ALIGN_RIGHT,
    )

    # The header band itself - bleeds to both page edges, unlike everything
    # else which respects MARGIN, so it reads as a banner, not a rule.
    page.draw_rect(pymupdf.Rect(0, 92, PAGE_WIDTH, 96), color=None, fill=TEAL)


#: Fixed layout constants for the title. Deliberately NOT computed from
#: actual content height: pymupdf draws text the instant insert_text/
#: insert_textbox is called (there is no measure-then-draw pass), so
#: anything that must be sized from its own text's real height would need
#: a second, throwaway render pass. A fixed, generous allowance is simpler
#: and can never overlap; the trade-off is a little unused whitespace for
#: a short title, which is the common case anyway.
TITLE_TOP = 108
TITLE_HEIGHT = 46


def draw_title(page: pymupdf.Page, title: str) -> None:
    rect = pymupdf.Rect(MARGIN, TITLE_TOP, CONTENT_RIGHT, TITLE_TOP + TITLE_HEIGHT)
    spare = page.insert_textbox(
        rect, title.upper(), fontsize=16, fontname=FONT_BOLD, color=TEXT_DARK,
    )
    if spare < 0:
        raise ValidationError("Titlul documentului este prea lung - scurtează-l.")

    line_y = TITLE_TOP + TITLE_HEIGHT - 8
    page.draw_line((MARGIN, line_y), (MARGIN + 60, line_y), color=TEAL, width=2)


def insert_wrapping_field(
    page: pymupdf.Page, rect: pymupdf.Rect, label: str, value: str
) -> None:
    """A "**Label:** value" row that WRAPS within `rect` instead of running
    past the page edge - the failure mode a plain single-line insert_text
    call has no defence against (it broke on a real long address before
    this existed - see admin/document_template.py's git history)."""
    page.insert_text(
        (rect.x0, rect.y0 + 10), label, fontsize=10, fontname=FONT_BOLD, color=TEXT_DARK
    )
    label_width = BOLD_FONT.text_length(label, fontsize=10)

    value_rect = pymupdf.Rect(rect.x0 + label_width + 4, rect.y0, rect.x1, rect.y1)
    spare = page.insert_textbox(
        value_rect, value, fontsize=10, fontname=FONT_REGULAR, color=TEXT_DARK
    )
    if spare < 0:
        raise ValidationError("Datele sunt prea lungi pentru șablonul documentului.")


def draw_footer(
    page: pymupdf.Page, reference: str, *, note: str, page_num: int | None = None,
    page_count: int | None = None,
) -> None:
    y = PAGE_HEIGHT - 60
    page.draw_line((MARGIN, y), (CONTENT_RIGHT, y), color=TEAL, width=1)

    page_line = ""
    if page_num is not None and page_count is not None and page_count > 1:
        page_line = f" · Pagina {page_num}/{page_count}"

    page.insert_textbox(
        pymupdf.Rect(MARGIN, y + 8, CONTENT_RIGHT, y + 40),
        f"BanK S.A. · Document generat electronic prin sistemul BanK · "
        f"Referință {reference}{page_line}\n{note}",
        fontsize=7, fontname=FONT_REGULAR, color=TEXT_MUTED, align=pymupdf.TEXT_ALIGN_CENTER,
    )
