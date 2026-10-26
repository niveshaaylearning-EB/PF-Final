"""Result Update tracking + merged-PDF generation for quarterly investor
updates. Admin-only end to end (visible as a home-page tile only to admins).

Tracked PER COMPANY (by nseCode), not per (basket, company) -- a company
held in multiple baskets (e.g. ACUTAAS in both Mid & Small Cap and Trends
Triology) shares ONE result date / received-checked-sent-sentResults state /
bullets / financial table, since it's the same real-world result regardless
of which basket holds it. Per the user's own spec (2026-10-05):
"Now suppose Green Energy Basket has... A... Now you have already tracked
A... then don't track it again for the basket." Each row instead carries a
`baskets` map of every basket it's EVER been tracked under, each with its
own `currentlyHeld` flag -- so a company removed from one basket but still
held in another keeps showing (and stays sendable) for the one that still
holds it, while a company removed from EVERY basket it was tracked under
drops out of reminders/generation entirely.

Format (see Document 1.DOC / MID&SM~1.PDF / MID&SM~2.PDF / RA Dislcaimer.docx,
2026-10-23 refinement): basket name -> company name -> [optional financial
table, if the uploaded document had one] -> "Operational Performance:" +
bold-lead-in bullets -> "Outlook:" + bold-lead-in bullets -> (once, at the
very end of a merge) the fixed SEBI research-analyst disclosure footer on
its own fresh page. Per company, ALL of the table/bullets are auto-parsed
from ONE uploaded result-update document (see result_update_doc_parser.py)
rather than typed in by hand or built from a separately-uploaded snapshot
image -- that whole older flow (concall date, a standalone snapshot-image
upload, manual bullet editors) was removed in this refinement.

Lifecycle rules (from the user's own spec):
  - A company currently held in a basket is tracked (and its result date
    auto-filled from Result Calendar when available) the moment it's first
    seen -- no manual "add dates" step. It only surfaces as a "new"
    reminder if Result Calendar has no date for it yet.
  - A basket that stops holding an already-tracked company has JUST that
    basket's `currentlyHeld` flipped to False -- other baskets still
    holding the same company are unaffected. Only once EVERY basket entry
    is `currentlyHeld: False` does the company drop out of reminders and
    the per-basket "available to merge" list (we don't chase an update for
    a position that's gone everywhere). A company already `received`
    before full removal is left alone (still sendable from its basket
    history) but naturally stops generating new reminders.
  - `sentResults` (added 2026-10-23) is a SEPARATE checkbox from
    received/checked/sent -- it's what actually selects a company into a
    merged PDF, and the Generate Merged/Consolidated PDF buttons only
    appear once at least one held company has it checked.
"""
import json
import os
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from auth import is_admin_email
from routers.actual_portfolio_bridge import _fetch_all_webportal_baskets

router = APIRouter()

_DATA_DIR = Path(__file__).parent.parent
_DATA_FILE = _DATA_DIR / "result_updates.json"
_RAW_DOC_DIR = _DATA_DIR / "result_update_raw_docs"
_RAW_DOC_DIR.mkdir(exist_ok=True)

# Read-only reuse of results_calendar.py's own cache (NSE board-meeting +
# yfinance earnings dates, already fetched/refreshed by that feature's own
# background job) to auto-suggest a result date on "new" reminders instead
# of always starting from a blank field -- reading the plain JSON file
# directly rather than importing results_calendar.py, so this module stays
# decoupled from that one's DB/session dependencies. Does NOT cover concall
# dates -- NSE's board-meeting feed is specifically for the results board
# meeting, not the separate earnings-call announcement, which has no
# reliable automated source anywhere in this codebase.
_RESULTS_CALENDAR_CACHE_FILE = _DATA_DIR / "results_calendar_cache.json"


def _suggested_result_dates() -> dict:
    """{nseCode: "YYYY-MM-DD"} for every upcoming/recent result-type event
    in results_calendar.py's cache. Best-effort -- any read/parse failure
    just means no suggestions, not an error for this feature."""
    try:
        cache = json.loads(_RESULTS_CALENDAR_CACHE_FILE.read_text(encoding="utf-8"))
        events = (cache.get("calendar") or {}).get("data") or []
        return {e["stock_code"]: e["date"] for e in events if e.get("type") == "result" and e.get("stock_code")}
    except Exception:
        return {}


def _require_admin(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not is_admin_email(user):
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


def _load() -> dict:
    if _DATA_FILE.exists():
        try:
            return json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(data: dict) -> None:
    _DATA_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _current_holdings() -> dict:
    """{basket_key: {"label": str, "stocks": {nseCode: securityName}}}"""
    out = {}
    try:
        baskets = _fetch_all_webportal_baskets()
    except Exception as e:
        print(f"[result_updates] could not fetch current holdings: {e}")
        return out
    for key, basket_obj in baskets.items():
        label = basket_obj.get("name", key)
        stocks = {}
        for h in basket_obj.get("holdings", []):
            code = (h.get("code") or "").strip().upper()
            if code:
                stocks[code] = h.get("stock_name") or code
        out[key] = {"label": label, "stocks": stocks}
    return out


def _new_row(nse_code: str, security_name: str) -> dict:
    # Per the 2026-10-23 refinement: concall date, the separate financial-
    # snapshot-image upload, and the raw-document-as-reference-only upload
    # are all gone -- replaced by ONE "upload or drag" document per company
    # (resultUpdateDocument) that gets parsed automatically into
    # operationalPerformance/outlook/financialTable (see
    # _parse_result_update_document below), instead of being typed in by
    # hand or kept purely for reference. sentResults is a new, separate
    # checkbox from received/checked/sent -- it's what actually selects a
    # company into the merged PDF (see generate_pdf), not a lifecycle status.
    return {
        "nseCode": nse_code, "securityName": security_name or nse_code,
        "resultDate": None,
        "received": False, "checked": False, "sent": False, "sentResults": False,
        "operationalPerformance": [], "outlook": [],
        "financialTable": None,
        "resultUpdateDocument": None,
        "baskets": {},
    }


_LEGACY_ROW_KEYS = ("concallDate", "snapshotImage", "rawDocument")

def _migrate_legacy_rows(data: dict) -> None:
    """A row created before the 2026-10-23 refinement (see _new_row) is
    missing the new schema's keys (sentResults/financialTable/
    resultUpdateDocument) and still carries now-dead ones (concallDate/
    snapshotImage/rawDocument) -- confirmed live 2026-10-26 on production:
    2 pre-rework rows (SAILIFE, SSWL) stayed in this stale shape forever,
    since nothing but an explicit admin edit via upsert would otherwise
    touch their fields. Normalizes every row to the current shape in
    place, filling only missing keys (never overwriting anything already
    there) and dropping the dead legacy ones."""
    fresh = _new_row("", "")
    for row in data.values():
        for key, default in fresh.items():
            if key not in row and key not in ("nseCode", "securityName", "baskets"):
                row[key] = default
        for key in _LEGACY_ROW_KEYS:
            row.pop(key, None)


def _sync_and_compute_reminders(data: dict) -> list:
    """Mutates `data` in place (adds newly-discovered basket associations,
    flips `currentlyHeld` per basket) and returns the reminders list. Also
    creates a real tracking row for any newly-held company (see the
    2026-10-23 refinement below) and migrates any pre-refinement row to
    the current schema."""
    _migrate_legacy_rows(data)
    holdings = _current_holdings()
    reminders = []

    # Per-basket currentlyHeld flags on every row already tracked anywhere.
    for row in data.values():
        for basket_key, binfo in row.get("baskets", {}).items():
            basket_stocks = (holdings.get(basket_key) or {}).get("stocks", {})
            binfo["currentlyHeld"] = row["nseCode"] in basket_stocks
            binfo["label"] = (holdings.get(basket_key) or {}).get("label", binfo.get("label", basket_key))

    # Auto-associate: a company already tracked (via some other basket) that
    # is ALSO currently held in a basket not yet in its `baskets` map --
    # "don't track it again for the basket", just record where it's held.
    for row in data.values():
        for basket_key, info in holdings.items():
            if row["nseCode"] in info["stocks"] and basket_key not in row.get("baskets", {}):
                row.setdefault("baskets", {})[basket_key] = {"label": info["label"], "currentlyHeld": True}

    today = date.today().isoformat()

    # New-company reminders: held now in some basket, no tracking row at all
    # for that nseCode anywhere yet -- one reminder per company even when
    # held in several baskets at once, listing all of them together (same
    # "show the baskets in which such stock exists" treatment as the
    # overdue reminders below), rather than one near-duplicate reminder per
    # basket encounter.
    tracked_codes = set(data.keys())
    new_by_code: dict = {}
    for basket_key, info in holdings.items():
        for code, name in info["stocks"].items():
            if code in tracked_codes:
                continue
            entry = new_by_code.setdefault(code, {"name": name, "baskets": []})
            entry["baskets"].append(info["label"])
    suggested_dates = _suggested_result_dates()

    # Per the 2026-10-23 refinement ("fetch the result date of each company
    # in each basket automatically"), a newly-discovered held company is now
    # created as a REAL tracked row immediately -- with its result date
    # auto-filled straight from Result Calendar's own feed when available --
    # instead of only ever surfacing as a "new" reminder the admin has to
    # manually click "Add dates" on. It still shows as a reminder if the
    # date is missing (Result Calendar has nothing for it yet) or once that
    # date has passed without being Received (the overdue case below).
    for code, entry in new_by_code.items():
        suggested = suggested_dates.get(code)
        row = _new_row(code, entry["name"])
        row["resultDate"] = suggested
        for label in entry["baskets"]:
            basket_key = next((k for k, v in holdings.items() if v["label"] == label), None)
            if basket_key:
                row["baskets"][basket_key] = {"label": label, "currentlyHeld": True}
        row["updatedAt"] = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
        data[code] = row

    # Also backfill any ALREADY-tracked row that's still missing a result
    # date -- e.g. Result Calendar only just learned the date after this
    # company started being tracked. Never overwrites a date someone already
    # set (manually or from an earlier auto-fill), only fills a blank.
    for row in data.values():
        if not row.get("resultDate"):
            suggested = suggested_dates.get(row["nseCode"])
            if suggested:
                row["resultDate"] = suggested

    # New-company / missing-date reminders -- one per company even when held
    # in several baskets at once.
    for row in data.values():
        held_labels = [b["label"] for b in row.get("baskets", {}).values() if b.get("currentlyHeld")]
        if not held_labels or row.get("resultDate"):
            continue
        baskets_str = ", ".join(held_labels)
        reminders.append({
            "type": "new", "nseCode": row["nseCode"], "securityName": row["securityName"],
            "baskets": held_labels, "suggestedResultDate": None,
            "message": f"{row['securityName']} ({row['nseCode']}) is held in {baskets_str} but Result Calendar has no result date for it yet -- enter one manually.",
        })

    # Overdue reminders for already-tracked, still-held-somewhere, not-yet-
    # received companies -- list every basket currently holding it.
    for row in data.values():
        held_labels = [b["label"] for b in row.get("baskets", {}).values() if b.get("currentlyHeld")]
        if not held_labels or row.get("received"):
            continue
        baskets_str = ", ".join(held_labels)
        if row.get("resultDate") and row["resultDate"] <= today:
            reminders.append({
                "type": "overdue_result", "nseCode": row["nseCode"], "securityName": row["securityName"],
                "baskets": held_labels, "resultDate": row["resultDate"],
                "message": f"{row['securityName']} ({row['nseCode']}) announced results on {row['resultDate']} -- held in: {baskets_str}. Mark Received once the update is in hand.",
            })

    return reminders


@router.get("/api/admin/result-updates")
def list_result_updates(request: Request):
    _require_admin(request)
    data = _load()
    reminders = _sync_and_compute_reminders(data)
    _save(data)
    holdings = _current_holdings()
    basket_labels = {k: v["label"] for k, v in holdings.items()}
    return {
        "rows": list(data.values()),
        "reminders": reminders,
        "basketLabels": basket_labels,
    }


@router.post("/api/admin/result-updates")
def upsert_result_update(body: dict, request: Request):
    """Create or update a company's tracking row. Only the fields present in
    `body` are changed on an existing row -- send just {"nseCode": "...",
    "received": true} to flip one checkbox without re-sending everything
    else. `basket` is optional -- it just seeds the initial `baskets` entry
    immediately (e.g. when creating a row from a reminder) instead of
    waiting for the next GET's sync pass to discover it."""
    _require_admin(request)
    nse_code = (body.get("nseCode") or "").strip().upper()
    if not nse_code:
        raise HTTPException(status_code=400, detail="nseCode is required")

    data = _load()
    row = data.get(nse_code) or _new_row(nse_code, body.get("securityName"))

    basket = (body.get("basket") or "").strip()
    if basket:
        holdings = _current_holdings()
        label = (holdings.get(basket) or {}).get("label", basket)
        row.setdefault("baskets", {})[basket] = {"label": label, "currentlyHeld": True}

    for field in ("securityName", "resultDate", "received", "checked", "sent", "sentResults",
                  "operationalPerformance", "outlook", "financialTable"):
        if field in body:
            row[field] = body[field]
    row["updatedAt"] = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    data[nse_code] = row
    _save(data)
    return row


@router.delete("/api/admin/result-updates/{nse_code}")
def delete_result_update(nse_code: str, request: Request):
    """Stops tracking a company entirely, across every basket -- an explicit
    admin action, not something the removed-from-one-basket sync does on
    its own."""
    _require_admin(request)
    data = _load()
    nse_code = nse_code.strip().upper()
    if nse_code not in data:
        raise HTTPException(status_code=404, detail="Not found")
    row = data[nse_code]
    if row.get("resultUpdateDocument"):
        try:
            (_RAW_DOC_DIR / row["resultUpdateDocument"]).unlink(missing_ok=True)
        except Exception:
            pass
    del data[nse_code]
    _save(data)
    return {"status": "removed"}


@router.post("/api/admin/result-updates/{nse_code}/parse-document")
async def upload_and_parse_document(nse_code: str, request: Request, file: UploadFile = File(...)):
    """Replaces the old separate snapshot-image + raw-document uploads with
    ONE drag-and-drop per company (per the 2026-10-23 refinement): the admin
    uploads the result-update document exactly as received from the
    research team, and this endpoint auto-extracts Operational Performance/
    Outlook bullets + an optional financial table from it -- see
    result_update_doc_parser.py for the exact parsing rules, confirmed
    against a real sample. The original upload is kept on disk (served back
    via GET .../document/{filename}) purely for reference/re-checking, same
    as the old raw-document upload did -- it is not itself fed into the
    merged PDF, the PARSED fields are."""
    _require_admin(request)
    nse_code = nse_code.strip().upper()
    data = _load()
    if nse_code not in data:
        raise HTTPException(status_code=404, detail="Add the result date for this company first")

    ext = os.path.splitext(file.filename or "")[1].lower() or ".docx"
    if ext not in (".doc", ".docx"):
        raise HTTPException(status_code=400, detail="Only .doc/.docx files are supported")
    filename = f"{nse_code}{ext}"
    dest = _RAW_DOC_DIR / filename

    old = data[nse_code].get("resultUpdateDocument")
    if old and old != filename:
        try:
            (_RAW_DOC_DIR / old).unlink(missing_ok=True)
        except Exception:
            pass

    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    import result_update_doc_parser as parser
    parse_target = dest
    converted = None
    try:
        if ext == ".doc":
            converted = parser.convert_doc_to_docx(dest, _RAW_DOC_DIR)
            parse_target = converted
        parsed = parser.parse_result_update_document(parse_target)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not parse this document: {e}")
    finally:
        if converted and converted.exists():
            converted.unlink(missing_ok=True)

    data[nse_code]["resultUpdateDocument"] = filename
    data[nse_code]["operationalPerformance"] = parsed["operationalPerformance"]
    data[nse_code]["outlook"] = parsed["outlook"]
    data[nse_code]["financialTable"] = parsed["financialTable"]
    data[nse_code]["updatedAt"] = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    _save(data)
    return data[nse_code]


@router.get("/api/admin/result-updates/document/{filename}")
def get_result_update_document(filename: str, request: Request):
    _require_admin(request)
    path = _RAW_DOC_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(path), filename=filename)


@router.post("/api/admin/result-updates/generate-pdf")
def generate_pdf(body: dict, request: Request):
    """Merges companies (one basket at a time) into a single PDF, per
    routers/result_updates.py's module docstring. Two modes, both scoped to
    companies CURRENTLY held in `basket` (per the 2026-10-23 refinement):
      - Partial/merged: every held company with `sentResults` checked --
        that checkbox is what actually selects a company into this PDF, not
        a separate selection-only control.
      - Consolidated: pass `consolidated: true` instead -- every held
        company that is `received` AND `checked` AND `sentResults`. Unlike
        the old all-or-nothing version, this no longer blocks the whole
        generation just because some other held company isn't ready yet --
        it simply includes whichever ones already qualify."""
    _require_admin(request)
    basket = (body.get("basket") or "").strip()
    if not basket:
        raise HTTPException(status_code=400, detail="basket is required")

    data = _load()
    consolidated = bool(body.get("consolidated"))

    current_holdings = _current_holdings()
    held_codes = (current_holdings.get(basket) or {}).get("stocks", {})
    if not held_codes:
        raise HTTPException(status_code=400, detail="This basket currently holds no stocks.")

    rows = []
    for code in held_codes:
        row = data.get(code)
        if not row or not row.get("sentResults"):
            continue
        if consolidated and not (row.get("received") and row.get("checked")):
            continue
        rows.append(row)

    if not rows:
        detail = (
            "No company in this basket is marked received, checked, and sentResults yet."
            if consolidated else
            "No company in this basket has 'sent results' checked yet."
        )
        raise HTTPException(status_code=400, detail=detail)

    basket_label = (current_holdings.get(basket) or {}).get("label", basket)

    import result_update_pdf
    import tempfile
    suffix = "consolidated" if consolidated else f"{len(rows)}companies"
    out_path = Path(tempfile.gettempdir()) / f"result_update_{basket}_{suffix}.pdf"
    result_update_pdf.generate_merged_pdf(basket_label, rows, out_path, consolidated=consolidated)

    return FileResponse(str(out_path), filename=out_path.name, media_type="application/pdf")
