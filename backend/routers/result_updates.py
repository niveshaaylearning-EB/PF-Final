"""Result Update tracking + merged-PDF generation for quarterly investor
updates. Admin-only end to end (visible as a home-page tile only to admins).

Per (basket, nseCode) we track: result date, concall date, and three
checkboxes -- received / checked / sent -- plus (once received) the
financial-snapshot image and the Operational Performance / Outlook bullet
text, matching the real sample format (see ACUTAA_1.DOC, 2026-10-02):
  basket name -> company name -> snapshot image (bordered) ->
  "Operational Performance:" + bold-lead-in bullets ->
  "Outlook:" + bold-lead-in bullets -> (once, at the very end of a merge)
  the fixed SEBI research-analyst disclosure footer.

Lifecycle rules (from the user's own spec):
  - A company currently held in a basket with no tracking record yet is a
    "new" reminder (dates need entering) -- covers "added after its results/
    concall already happened" implicitly, since we can't know that without
    dates being entered first.
  - A tracked company no longer held in its basket AND not yet `received`
    is marked removedFromPortfolio and drops out of reminders/generation --
    we don't chase a result update for a position that's gone. One already
    `received` before removal is left alone (still sendable).
"""
import json
import os
import shutil
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


def _key(basket: str, nse_code: str) -> str:
    return f"{basket}|{nse_code.strip().upper()}"


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


def _sync_and_compute_reminders(data: dict) -> list:
    """Mutates `data` in place (removedFromPortfolio flag only) and returns
    the reminders list. Does NOT delete/create tracking rows for new
    companies -- that only happens once the admin actually enters dates for
    one, via the upsert endpoint; "new" reminders just surface the gap."""
    holdings = _current_holdings()
    reminders = []

    # Removed-from-portfolio detection: a tracked row whose stock is no
    # longer in that basket's current holdings.
    for k, row in data.items():
        basket_stocks = (holdings.get(row["basket"]) or {}).get("stocks", {})
        currently_held = row["nseCode"] in basket_stocks
        row["removedFromPortfolio"] = not currently_held

    from datetime import date
    today = date.today().isoformat()

    # New-company reminders: held now, no tracking row at all.
    tracked_pairs = {(r["basket"], r["nseCode"]) for r in data.values()}
    for basket_key, info in holdings.items():
        for code, name in info["stocks"].items():
            if (basket_key, code) not in tracked_pairs:
                reminders.append({
                    "type": "new", "basket": basket_key, "basketLabel": info["label"],
                    "nseCode": code, "securityName": name,
                    "message": f"{name} ({code}) is held in {info['label']} but has no result/concall dates tracked yet.",
                })

    # Overdue reminders for already-tracked, still-held, not-yet-received rows.
    for row in data.values():
        if row.get("removedFromPortfolio") or row.get("received"):
            continue
        basket_label = (holdings.get(row["basket"]) or {}).get("label", row["basket"])
        if row.get("resultDate") and row["resultDate"] <= today:
            reminders.append({
                "type": "overdue_result", "basket": row["basket"], "basketLabel": basket_label,
                "nseCode": row["nseCode"], "securityName": row["securityName"],
                "message": f"{row['securityName']} ({row['nseCode']}) announced results on {row['resultDate']} -- mark Received once the update is in hand.",
            })
        elif row.get("concallDate") and row["concallDate"] <= today:
            reminders.append({
                "type": "overdue_concall", "basket": row["basket"], "basketLabel": basket_label,
                "nseCode": row["nseCode"], "securityName": row["securityName"],
                "message": f"{row['securityName']} ({row['nseCode']})'s concall was on {row['concallDate']} -- mark Received once the update is in hand.",
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
    """Create or update a tracking row. Only the fields present in `body`
    are changed on an existing row -- send just {"received": true} to flip
    one checkbox without re-sending everything else."""
    _require_admin(request)
    basket = (body.get("basket") or "").strip()
    nse_code = (body.get("nseCode") or "").strip().upper()
    if not basket or not nse_code:
        raise HTTPException(status_code=400, detail="basket and nseCode are required")

    data = _load()
    k = _key(basket, nse_code)
    row = data.get(k) or {
        "basket": basket, "nseCode": nse_code,
        "securityName": body.get("securityName") or nse_code,
        "resultDate": None, "concallDate": None,
        "received": False, "checked": False, "sent": False,
        "operationalPerformance": [], "outlook": [],
        "snapshotImage": None, "removedFromPortfolio": False,
    }
    for field in ("securityName", "resultDate", "concallDate", "received", "checked", "sent",
                  "operationalPerformance", "outlook"):
        if field in body:
            row[field] = body[field]
    from datetime import datetime, timezone
    row["updatedAt"] = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    data[k] = row
    _save(data)
    return row


@router.delete("/api/admin/result-updates/{basket}/{nse_code}")
def delete_result_update(basket: str, nse_code: str, request: Request):
    _require_admin(request)
    data = _load()
    k = _key(basket, nse_code)
    if k not in data:
        raise HTTPException(status_code=404, detail="Not found")
    snapshot = data[k].get("snapshotImage")
    if snapshot:
        try:
            (_UPLOAD_DIR / snapshot).unlink(missing_ok=True)
        except Exception:
            pass
    del data[k]
    _save(data)
    return {"status": "removed"}


@router.post("/api/admin/result-updates/{basket}/{nse_code}/snapshot")
async def upload_snapshot(basket: str, nse_code: str, request: Request, file: UploadFile = File(...)):
    _require_admin(request)
    data = _load()
    k = _key(basket, nse_code)
    if k not in data:
        raise HTTPException(status_code=404, detail="Add the result/concall dates for this company first")

    ext = os.path.splitext(file.filename or "")[1].lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg"):
        raise HTTPException(status_code=400, detail="Only PNG/JPG images are supported")
    filename = f"{nse_code}_{basket}{ext}".replace(" ", "_")
    dest = _UPLOAD_DIR / filename

    old = data[k].get("snapshotImage")
    if old and old != filename:
        try:
            (_UPLOAD_DIR / old).unlink(missing_ok=True)
        except Exception:
            pass

    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    data[k]["snapshotImage"] = filename
    _save(data)
    return {"status": "ok", "snapshotImage": filename}


@router.get("/api/admin/result-updates/snapshot/{filename}")
def get_snapshot(filename: str, request: Request):
    _require_admin(request)
    path = _UPLOAD_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(path))


@router.post("/api/admin/result-updates/generate-pdf")
def generate_pdf(body: dict, request: Request):
    """Merges the selected companies (one basket at a time) into a single
    PDF in the order given, per routers/result_updates.py's module
    docstring. Used for BOTH of the user's two sending patterns -- a
    partial merge of whichever companies reported this week, or (once every
    currently-tracked, non-removed company in the basket is `received`) the
    full consolidated basket update -- the caller just decides which
    nseCodes to pass; the PDF itself doesn't need to know which case it is."""
    _require_admin(request)
    basket = (body.get("basket") or "").strip()
    nse_codes = body.get("nseCodes") or []
    if not basket or not nse_codes:
        raise HTTPException(status_code=400, detail="basket and nseCodes are required")

    data = _load()
    rows = []
    for code in nse_codes:
        row = data.get(_key(basket, code))
        if not row:
            raise HTTPException(status_code=404, detail=f"No tracking row for {code} in {basket}")
        rows.append(row)

    holdings = _current_holdings()
    basket_label = (holdings.get(basket) or {}).get("label", basket)

    import result_update_pdf
    import tempfile
    out_path = Path(tempfile.gettempdir()) / f"result_update_{basket}_{len(rows)}companies.pdf"
    result_update_pdf.generate_merged_pdf(basket_label, rows, _UPLOAD_DIR, out_path)

    return FileResponse(str(out_path), filename=out_path.name, media_type="application/pdf")
