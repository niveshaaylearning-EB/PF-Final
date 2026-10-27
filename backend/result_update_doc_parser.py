"""Parses an uploaded result-update Word document (.doc/.docx, as sent by
the research team for one company) into the exact shape result_update_pdf.py
renders: Operational Performance bullets, Outlook bullets, and an optional
financial snapshot table.

Confirmed live against a real sample (Document 1.DOC, 2026-10-23) via
python-docx: each bullet is a single paragraph with ONE bold run holding the
lead-in phrase INCLUDING its trailing colon (e.g. "Record Quarterly
Performance:"), followed by one or more plain (non-bold) runs holding the
rest of the sentence. Section boundaries are paragraphs whose entire text is
bold and reads "Operational Performance:" / "Outlook:" / "Disclosure:" --
the Disclosure section (the firm's own RA disclaimer boilerplate, often
re-attached at the end of the source document by whoever sent it) is
deliberately NOT parsed or carried through here; this app always appends
its OWN fixed disclosure footer once at the very end of a merged PDF
(result_update_pdf.py's _DISCLOSURE_FOOTER), so including a per-company copy
here would duplicate it.

A financial snapshot table, if the source document has one, is carried
through generically (whatever rows/columns it actually has) rather than
assuming a fixed set of line items -- confirmed live that even two real
companies' tables don't share the same row set (e.g. "Cost of materials
consumed" vs "Cost of raw materials and components consumed" vs separate
"Purchases of stock-in-trade" + "Changes in inventories" lines).

Some companies' financial snapshot instead arrives as a pasted-in PICTURE
(a screenshot of the formatted table, logo and all) rather than a real
Word table -- confirmed live 2026-10-27 ("the financial snapshot image is
not getting captured"). Any embedded image in the uploaded document is
now extracted too (see extract_images) and carried through as-is into the
PDF, since a raster snapshot like that already has its own formatting
(and the firm's logo) baked in -- there's nothing to re-parse out of it.
"""
import subprocess
import shutil
import zipfile
from pathlib import Path

import docx

_SECTION_HEADINGS = {"operational performance:", "outlook:", "disclosure:"}


def _is_section_heading(paragraph, text: str) -> str | None:
    """Returns the lowercased heading text if this whole paragraph is one of
    the known section markers. Originally required every run on the line to
    be bold; loosened 2026-10-27 after a real document (E2E's) silently
    parsed to zero bullets in both sections -- a heading line can carry a
    trailing non-bold space/tab run (e.g. from a copy-paste) that the old
    all-or-nothing check rejected outright. Now just requires the BULK of
    the line's characters to be bold, not literally every run."""
    stripped = text.strip().lower()
    if stripped not in _SECTION_HEADINGS:
        return None
    runs_with_text = [r for r in paragraph.runs if r.text.strip()]
    if not runs_with_text:
        return None
    bold_chars = sum(len(r.text) for r in runs_with_text if r.bold)
    total_chars = sum(len(r.text) for r in runs_with_text)
    if total_chars and bold_chars / total_chars < 0.8:
        return None
    return stripped


def _parse_bullet(paragraph) -> dict:
    """One bullet paragraph -> {"heading": str, "text": str}. The bold
    run(s) are the lead-in (colon stripped); everything else is the body.
    Falls back to heading="" (whole line as plain text) if the paragraph
    has no bold run at all -- some bullets in the wild are plain sentences
    with no lead-in phrase."""
    bold_parts = [r.text for r in paragraph.runs if r.bold]
    plain_parts = [r.text for r in paragraph.runs if not r.bold]
    heading = "".join(bold_parts).strip().rstrip(":").strip()
    text = "".join(plain_parts).strip()
    if not heading:
        return {"heading": "", "text": paragraph.text.strip()}
    return {"heading": heading, "text": text}


def _row_is_bold(row) -> bool:
    """A row counts as bold (rendered as a subtotal/highlight row, e.g.
    Gross Profit / EBITDA / PAT) if its first cell's text is bold in the
    source -- detected from actual run formatting rather than guessing by
    label name, since line-item names vary company to company."""
    first_cell = row.cells[0]
    runs = [r for p in first_cell.paragraphs for r in p.runs if r.text.strip()]
    return bool(runs) and all(r.bold for r in runs)


def _parse_table(table) -> dict:
    if not table.rows:
        return {"headers": [], "rows": []}
    headers = [c.text.strip() for c in table.rows[0].cells]
    rows_out = [
        {"cells": [c.text.strip() for c in row.cells], "bold": _row_is_bold(row)}
        for row in table.rows[1:]
    ]
    return {"headers": headers, "rows": rows_out}


def convert_doc_to_docx(src_path: Path, out_dir: Path) -> Path:
    """Converts a legacy .doc to .docx via headless LibreOffice (Linux/
    production) -- python-docx only understands the modern .docx XML
    format. Raises a clear error if soffice isn't available (e.g. local
    Windows dev without LibreOffice installed) rather than failing with a
    confusing stack trace deep inside python-docx."""
    if shutil.which("soffice") is None:
        raise RuntimeError(
            "Cannot convert a .doc file -- LibreOffice (soffice) is not installed "
            "on this machine. Please upload a .docx file instead, or install "
            "LibreOffice for local .doc support."
        )
    subprocess.run(
        ["soffice", "--headless", "--convert-to", "docx", "--outdir", str(out_dir), str(src_path)],
        check=True, capture_output=True, timeout=60,
    )
    converted = out_dir / (src_path.stem + ".docx")
    if not converted.exists():
        raise RuntimeError("LibreOffice conversion did not produce the expected .docx output.")
    return converted


def extract_images(path: Path) -> list[bytes]:
    """Returns the raw bytes of every embedded picture in the document, in
    document order -- a .docx is just a zip, and every embedded image (no
    matter how it's anchored/wrapped) lands under word/media/. Picture order
    there matches insertion order in practice, which is good enough here
    since a result-update document realistically carries at most one
    financial-snapshot picture."""
    images = []
    with zipfile.ZipFile(path) as z:
        media_names = sorted(n for n in z.namelist() if n.startswith("word/media/"))
        for name in media_names:
            images.append(z.read(name))
    return images


def parse_result_update_document(path: Path) -> dict:
    """Returns {"operationalPerformance": [...], "outlook": [...],
    "financialTable": {...} | None}. Raises ValueError if NEITHER section
    heading was found anywhere in the document -- confirmed live 2026-10-27
    that a parse which silently produces two empty lists (heading detection
    simply never matching anything in that particular document) looked
    identical to "this company has nothing to report yet", when it was
    actually a parsing failure the admin had no way to notice. A document
    that genuinely only has one of the two sections is NOT an error -- only
    finding zero of either heading is treated as one."""
    d = docx.Document(str(path))

    section = None  # None | "operational performance:" | "outlook:" | "disclosure:"
    op_perf, outlook = [], []
    found_heading = False

    for p in d.paragraphs:
        text = p.text.strip()
        heading = _is_section_heading(p, text)
        if heading:
            section = heading
            found_heading = True
            continue
        if not text or section is None or section == "disclosure:":
            continue
        bullet = _parse_bullet(p)
        if not bullet["heading"] and not bullet["text"]:
            continue
        if section == "operational performance:":
            op_perf.append(bullet)
        elif section == "outlook:":
            outlook.append(bullet)

    if not found_heading:
        raise ValueError(
            "Could not find an 'Operational Performance:' or 'Outlook:' heading "
            "anywhere in this document -- check that those section headings are "
            "present and entirely bold."
        )

    financial_table = _parse_table(d.tables[0]) if d.tables else None
    images = extract_images(path)

    return {
        "operationalPerformance": op_perf,
        "outlook": outlook,
        "financialTable": financial_table,
        "images": images,
    }
