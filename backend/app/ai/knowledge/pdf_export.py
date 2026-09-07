"""HTML -> PDF, the automated equivalent of the "Ctrl+P > Save as PDF" step
described in backend/knowledge_base/*.html's own header comment.

Shells out to Microsoft Edge's headless print-to-PDF (same Chromium print
pipeline the interactive Ctrl+P dialog uses, so it respects the source
files' print CSS: @page size, page-break-after, etc.) rather than adding a
browser-automation dependency for a step that only ever runs offline, by
hand, in scripts/ingest_knowledge_base.py.

If Edge isn't installed, this fails with an actionable message: export the
PDF by hand instead (open the HTML file, Ctrl+P, "Save as PDF"), save it
next to the source .html, and re-run ingestion - it picks up an
already-exported PDF that's newer than its source and skips this step.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


class PdfExportError(RuntimeError):
    """Could not produce a PDF from the source HTML."""


def _find_edge() -> str:
    on_path = shutil.which("msedge")
    if on_path:
        return on_path
    for candidate in _EDGE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise PdfExportError(
        "Microsoft Edge was not found, so the HTML -> PDF step can't run "
        "automatically. Export it by hand instead: open the .html file, "
        "Ctrl+P, 'Save as PDF', save it next to the source file with the "
        "same name (.pdf), then re-run ingestion."
    )


def export_html_to_pdf(html_path: Path, pdf_path: Path) -> None:
    """Render `html_path` to `pdf_path` via Edge headless printing.

    Raises `PdfExportError` on any failure - a missing binary, a bad exit
    code, or an empty output file (Edge can exit 0 while still failing to
    write a real PDF in some sandboxed environments).
    """
    edge = _find_edge()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    # file:// URI, not a bare path: Edge headless resolves relative asset
    # references (fonts, if any are ever added) against the URI scheme.
    source_uri = html_path.resolve().as_uri()

    result = subprocess.run(
        [
            edge,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            f"--print-to-pdf={pdf_path.resolve()}",
            "--print-to-pdf-no-header",
            source_uri,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0 or not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        raise PdfExportError(
            f"Edge headless failed to export {html_path.name} to PDF "
            f"(exit={result.returncode}). stderr: {result.stderr.strip()[:500]}"
        )
