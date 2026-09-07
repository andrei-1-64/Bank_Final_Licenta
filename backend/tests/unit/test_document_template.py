"""app/modules/admin/document_template.py - pure function, no DB/network.

Covers the two real bugs a visual review caught in the first version of this
template: a long title ran clean off the right edge of the page (a plain
insert_text call has no line-wrap or bounds check), and a long address did
the same inside the info box. Both are fixed by rendering through
insert_textbox with an explicit overflow check instead - these tests pin
that down: overflow must raise cleanly, never render past the page.
"""

from __future__ import annotations

import pymupdf
import pytest

from app.core.exceptions import ValidationError
from app.modules.admin.document_template import render_document_pdf

_HOLDER = dict(first_name="Andrei", last_name="Popescu", national_id="1950615123456")


def test_renders_a_valid_single_page_pdf():
    pdf_bytes = render_document_pdf(
        title="Adeverință de venit",
        body="Prin prezenta se confirmă că titularul deține un cont activ la BanK.",
        address="Str. Exemplu nr. 1, București",
        **_HOLDER,
    )

    assert pdf_bytes.startswith(b"%PDF-")
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    assert doc.page_count == 1


def test_romanian_diacritics_round_trip_through_the_extracted_text():
    """The base14 "helv" font pymupdf falls back to only supports
    WinAnsiEncoding, which drops ă/ș/ț entirely (see the module docstring) -
    this is what proves the bundled DejaVu Sans font is actually being used
    instead, not just present on disk."""
    body = "Diacritice: ă â î ș ț - conținut și ștampilă."
    pdf_bytes = render_document_pdf(
        title="Test diacritice", body=body, address="Str. Test", **_HOLDER
    )

    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    extracted = doc[0].get_text()
    assert "ă â î ș ț" in extracted
    assert "conținut și ștampilă" in extracted
    assert "?" not in extracted


def test_holder_data_appears_on_the_page_not_just_in_the_payload():
    pdf_bytes = render_document_pdf(
        title="Test", body="Corp.", address="Str. Test nr. 5", **_HOLDER
    )
    extracted = pymupdf.open(stream=pdf_bytes, filetype="pdf")[0].get_text()

    assert "Andrei Popescu" in extracted
    assert "1950615123456" in extracted
    assert "Str. Test nr. 5" in extracted


def test_very_long_title_raises_instead_of_printing_past_the_page_edge():
    long_title = "Notificare " * 20  # ~220 chars - overflows even 2 wrapped lines

    with pytest.raises(ValidationError):
        render_document_pdf(title=long_title, body="Corp.", address="Str. Test", **_HOLDER)


def test_very_long_address_raises_instead_of_overflowing_the_info_box():
    # A real value a user's profile can legitimately hold (UserUpdate.address
    # allows up to 255 chars) - this is not a synthetic-only edge case.
    pathological_address = ("Strada Foarte Lungă Cu Multe Cuvinte Repetate " * 6)[:255]

    with pytest.raises(ValidationError):
        render_document_pdf(
            title="Test", body="Corp.", address=pathological_address, **_HOLDER
        )


def test_realistic_long_address_fits_without_raising():
    """The fix's actual target: a genuinely long but realistic Romanian
    address (block, scară, etaj, apartment, postal code) must render
    normally, not just avoid crashing - see the module's _BOX_HEIGHT
    comment for the trade-off between this and the pathological case
    above."""
    realistic_address = (
        "Bulevardul Foarte Lung Numărul 100, Bloc A2, Scara 3, Etaj 7, "
        "Ap. 45, Sector 3, București, Cod Poștal 030167"
    )

    pdf_bytes = render_document_pdf(
        title="Adeverință", body="Corp scurt.", address=realistic_address, **_HOLDER
    )
    extracted = pymupdf.open(stream=pdf_bytes, filetype="pdf")[0].get_text()
    assert "Cod Poștal 030167" in extracted


def test_body_near_the_documented_cap_still_fits_one_page():
    body = (
        "Acest text verifică dacă documentul oficial mai încape pe o "
        "singură pagină cu diacritice românești: ă, â, î, ș, ț. "
    ) * 15
    body = body[:2000]

    pdf_bytes = render_document_pdf(title="Test", body=body, address="Str. Test", **_HOLDER)
    assert pymupdf.open(stream=pdf_bytes, filetype="pdf").page_count == 1


def test_missing_national_id_and_address_render_as_placeholder():
    """Both are nullable on the user profile (UserRead.national_id/address
    are `str | None`) - a user who never filled them in must not crash
    document generation, they render as an explicit "-"."""
    pdf_bytes = render_document_pdf(
        title="Test", body="Corp.", first_name="Andrei", last_name="Popescu",
        national_id=None, address=None,
    )
    extracted = pymupdf.open(stream=pdf_bytes, filetype="pdf")[0].get_text()
    assert "CNP: -" in extracted
    assert "Adresă: -" in extracted
