"""Result Update tracking + merged-PDF generation for quarterly investor
updates. Admin-only end to end (visible as a home-page tile only to admins).

Tracked PER COMPANY (by nseCode), not per (basket, company) -- a company
held in multiple baskets (e.g. ACUTAAS in both Mid & Small Cap and Trends
Triology) shares ONE result date / concall date / received-checked-sent
state / snapshot / bullets, since it's the same real-world result
regardless of which basket holds it. Per the user's own spec (2026-10-05):
"Now suppose Green Energy Basket has... A... Now you have already tracked
A... then don't track it again for the basket." Each row instead carries a
`baskets` map of every basket it's EVER been tracked under, each with its
own `currentlyHeld` flag -- so a company removed from one basket but still
held in another keeps showing (and stays sendable) for the one that still
holds it, while a company removed from EVERY basket it was tracked under
drops out of reminders/generation entirely.

Format (see ACUTAA_1.DOC / MID&SM_1.PDF / MID&SM_2.PDF / RA Disclaimer.docx,
2026-10-02): basket name -> company name -> snapshot image (bordered) ->
"Operational Performance:" + bold-lead-in bullets -> "Outlook:" +
bold-lead-in bullets -> (once, at the very end of a merge) the fixed SEBI
research-analyst disclosure footer on its own fresh page.

Lifecycle rules (from the user's own spec):
  - A company currently held in ANY basket with no tracking row at all yet
    is a "new" reminder (dates need entering), listing every basket that
    currently holds it. This covers "added after its results/concall
    already happened" implicitly -- we track it regardless of whether the
    real-world result date is before or after the addition, since we are
    sending the update for it either way.
  - A basket that stops holding an already-tracked company has JUST that
    basket's `currentlyHeld` flipped to False -- other baskets still
    holding the same company are unaffected. Only once EVERY basket entry
    is `currentlyHeld: False` does the company drop out of reminders and
    the per-basket "available to merge" list (we don't chase an update for
    a position that's gone everywhere). A company already `received`
    before full removal is left alone (still sendable from its basket
    history) but naturally stops generating new reminders.
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
_UPLOAD_DIR = _DATA_DIR / "result_update_uploads"
_UPLOAD_DIR.mkdir(exist_ok=True)
_RAW_DOC_DIR = _DATA_DIR / "result_update_raw_docs"
_RAW_DOC_DIR.mkdir(exist_ok=True)


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
    return {
        "nseCode": nse_code, "securityName": security_name or nse_code,
        "resultDate": None, "concallDate": None,
        "received": False, "checked": False, "sent": False,
        "operationalPerformance": [], "outlook": [],
        "snapshotImage": None, "rawDocument": None,
        "baskets": {},
    }


def _sync_and_compute_reminders(data: dict) -> list:
    """Mutates `data` in place (adds newly-discovered basket associations,
    flips `currentlyHeld` per basket) and returns the reminders list. Does
    NOT create a tracking row for a brand-new company -- that only happens
    once the admin actually enters dates for it, via the upsert endpoint;
    "new" reminders just surface the gap."""
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
    for code, entry in new_by_code.items():
        baskets_str = ", ".join(entry["baskets"])
        reminders.append({
            "type": "new", "nseCode": code, "securityName": entry["name"],
            "baskets": entry["baskets"],
            "message": f"{entry['name']} ({code}) is held in {baskets_str} but has no result/concall dates tracked yet.",
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
                "baskets": held_labels,
                "message": f"{row['securityName']} ({row['nseCode']}) announced results on {row['resultDate']} -- held in: {baskets_str}. Mark Received once the update is in hand.",
            })
        elif row.get("concallDate") and row["concallDate"] <= today:
            reminders.append({
                "type": "overdue_concall", "nseCode": row["nseCode"], "securityName": row["securityName"],
                "baskets": held_labels,
                "message": f"{row['securityName']} ({row['nseCode']})'s concall was on {row['concallDate']} -- held in: {baskets_str}. Mark Received once the update is in hand.",
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

    for field in ("securityName", "resultDate", "concallDate", "received", "checked", "sent",
                  "operationalPerformance", "outlook"):
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
    for filename, directory in ((row.get("snapshotImage"), _UPLOAD_DIR), (row.get("rawDocument"), _RAW_DOC_DIR)):
        if filename:
            try:
                (directory / filename).unlink(missing_ok=True)
            except Exception:
                pass
    del data[nse_code]
    _save(data)
    return {"status": "removed"}


@router.post("/api/admin/result-updates/{nse_code}/snapshot")
async def upload_snapshot(nse_code: str, request: Request, file: UploadFile = File(...)):
    _require_admin(request)
    nse_code = nse_code.strip().upper()
    data = _load()
    if nse_code not in data:
        raise HTTPException(status_code=404, detail="Add the result/concall dates for this company first")

    ext = os.path.splitext(file.filename or "")[1].lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg"):
        raise HTTPException(status_code=400, detail="Only PNG/JPG images are supported")
    filename = f"{nse_code}{ext}"
    dest = _UPLOAD_DIR / filename

    old = data[nse_code].get("snapshotImage")
    if old and old != filename:
        try:
            (_UPLOAD_DIR / old).unlink(missing_ok=True)
        except Exception:
            pass

    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    data[nse_code]["snapshotImage"] = filename
    _save(data)
    return {"status": "ok", "snapshotImage": filename}


@router.get("/api/admin/result-updates/snapshot/{filename}")
def get_snapshot(filename: str, request: Request):
    _require_admin(request)
    path = _UPLOAD_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(path))


@router.post("/api/admin/result-updates/{nse_code}/raw-document")
async def upload_raw_document(nse_code: str, request: Request, file: UploadFile = File(...)):
    """Stores the original Word result-update document as received from the
    company/research team -- kept for reference/re-checking against what
    was actually parsed into the snapshot image + bullets, not itself used
    to generate the merged PDF."""
    _require_admin(request)
    nse_code = nse_code.strip().upper()
    data = _load()
    if nse_code not in data:
        raise HTTPException(status_code=404, detail="Add the result/concall dates for this company first")

    ext = os.path.splitext(file.filename or "")[1].lower() or ".docx"
    if ext not in (".doc", ".docx"):
        raise HTTPException(status_code=400, detail="Only .doc/.docx files are supported")
    filename = f"{nse_code}{ext}"
    dest = _RAW_DOC_DIR / filename

    old = data[nse_code].get("rawDocument")
    if old and old != filename:
        try:
            (_RAW_DOC_DIR / old).unlink(missing_ok=True)
        except Exception:
            pass

    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    data[nse_code]["rawDocument"] = filename
    _save(data)
    return {"status": "ok", "rawDocument": filename}


@router.get("/api/admin/result-updates/raw-document/{filename}")
def get_raw_document(filename: str, request: Request):
    _require_admin(request)
    path = _RAW_DOC_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(path), filename=filename)


@router.post("/api/admin/result-updates/generate-pdf")
def generate_pdf(body: dict, request: Request):
    """Merges companies (one basket at a time) into a single PDF, per
    routers/result_updates.py's module docstring. Two modes:
      - Partial: pass explicit `nseCodes`, e.g. whichever companies
        reported results this week.
      - Consolidated: pass `consolidated: true` instead -- automatically
        selects every company currently held in `basket` that is already
        `received`, for the final once-everyone's-reported send. Refuses
        if anything currently held in the basket is still un-received,
        since a consolidated send is supposed to be the complete set."""
    _require_admin(request)
    basket = (body.get("basket") or "").strip()
    if not basket:
        raise HTTPException(status_code=400, detail="basket is required")

    data = _load()
    consolidated = bool(body.get("consolidated"))

    if consolidated:
        # Check against the basket's ACTUAL current holdings, not just
        # already-tracked rows -- a company with no tracking row at all
        # definitely hasn't been received, and silently excluding it (as an
        # earlier version of this check did, by only ever looking at rows
        # that already existed) would let a consolidated send go out
        # missing companies nobody had even started tracking yet.
        current_holdings = _current_holdings()
        held_codes = (current_holdings.get(basket) or {}).get("stocks", {})
        if not held_codes:
            raise HTTPException(status_code=400, detail="This basket currently holds no stocks.")
        missing, not_received = [], []
        rows = []
        for code, name in held_codes.items():
            row = data.get(code)
            if not row or not row.get("received"):
                (missing if not row else not_received).append(f"{name} ({code})")
            else:
                rows.append(row)
        if missing or not_received:
            parts = []
            if missing:
                parts.append(f"not tracked yet: {', '.join(missing)}")
            if not_received:
                parts.append(f"not yet received: {', '.join(not_received)}")
            raise HTTPException(status_code=400, detail=f"Not every company in this basket has been received yet ({'; '.join(parts)}).")
    else:
        nse_codes = body.get("nseCodes") or []
        if not nse_codes:
            raise HTTPException(status_code=400, detail="nseCodes is required (or pass consolidated: true)")
        rows = []
        for code in nse_codes:
            row = data.get(code.strip().upper())
            if not row:
                raise HTTPException(status_code=404, detail=f"No tracking row for {code}")
            rows.append(row)

    holdings = _current_holdings()
    basket_label = (holdings.get(basket) or {}).get("label", basket)

    import result_update_pdf
    import tempfile
    suffix = "consolidated" if consolidated else f"{len(rows)}companies"
    out_path = Path(tempfile.gettempdir()) / f"result_update_{basket}_{suffix}.pdf"
    result_update_pdf.generate_merged_pdf(basket_label, rows, _UPLOAD_DIR, out_path, consolidated=consolidated)

    return FileResponse(str(out_path), filename=out_path.name, media_type="application/pdf")
