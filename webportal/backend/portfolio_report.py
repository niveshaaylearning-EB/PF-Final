"""Upload-and-diff a portfolio-report PDF against the current basket: parses
additions/removals/weight-changes and applies them to portfolios + rebalance
history, matching company names to NSE codes via the cached symbol list.

Two ways to get the PDF into this, with two DIFFERENT parsers -- see
smallcase_login.py's long comment on why _parse_portfolio_pdf below is not
safe to reuse for the automated path:
  - manual upload (the original path): price_engine._parse_portfolio_pdf.
  - automated fetch straight from smallcase.com via Playwright
    (smallcase_login.fetch_rebalance_report): smallcase_login.
    parse_rebalance_update, reading only its one tightly-bounded section.
Both funnel into the same _apply_parsed_entries so the matching/applying
logic only exists once."""
import base64
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile

import price_engine
import smallcase_login
from buy_price_gains import _date_to_ts, _add_event, _recalc_basket_buy_prices, _refresh_gains_file
from persistence import (
    BASKET_DISPLAY_NAMES, _require_admin,
    _load_portfolios, _save_portfolios,
    _load_rebalance_history, _save_rebalance_history,
    _load_buy_price_data, _save_buy_price_data,
    _load_pending_rebalance_reports, _save_pending_rebalance_reports,
)
from price_engine import _parse_portfolio_pdf, _resolve_nse, _fetch_nse_symbols
from live_data import _fetch_rebalance_prices

router = APIRouter()


async def _apply_parsed_entries(background_tasks: BackgroundTasks, basket: str, date_str: str, pdf_entries: list) -> dict:
    # Duplicate check
    rh = _load_rebalance_history()
    existing_dates = {e.get("date", "").strip() for e in rh.get(basket, [])}
    if date_str in existing_dates:
        return {"duplicate": True, "message": "Report for this date has already been uploaded"}

    if not pdf_entries:
        raise HTTPException(status_code=400, detail="No rebalance data found in PDF")

    # Resolve NSE codes — load portfolio + NSE symbols for matching
    portfolios    = _load_portfolios()
    curr_stocks   = portfolios.get(basket, [])
    nse_symbols   = price_engine._nse_symbols_cache  # use cached list (populated by /api/nse-symbols)
    if not nse_symbols:
        nse_symbols = await _fetch_nse_symbols()

    stk_map  = {s["nseCode"]: s for s in curr_stocks}
    sold     = portfolios.get(f"{basket}_sold", [])

    # Same buy/sell EVENT LOG the Excel-upload path (rebalance.py) writes to
    # -- NOT a new price fetch from smallcase. This is what the Buy Price
    # page's per-stock history and weighted-average price are actually
    # computed from; only writing to portfolios.json's flat allocation/
    # buyPrice (as this function used to) left that page's history blank for
    # anything applied through this path.
    bp_data   = _load_buy_price_data()
    basket_bp = bp_data.setdefault(basket, {})

    def _apply_bp_event(code: str, evt_type: str, delta: float, sec_name: str, segment: str, series_reset: bool = False):
        _add_event(basket_bp, code, f"{evt_type}Events", date_str, delta)
        det = basket_bp[code]
        if not det.get("securityName") and sec_name:
            det["securityName"] = sec_name
        if not det.get("segment") and segment:
            det["segment"] = segment
        if series_reset:
            det["prevBuyEvents"]  = det.get("buyEvents",  "")
            det["prevSellEvents"] = det.get("sellEvents", "")
            det["buyEvents"]  = ""
            det["sellEvents"] = ""

    # Build prev_snap from rebalance history (same as CSV upload)
    basket_history = rh.get(basket, [])
    by_date: dict  = {}
    for e in basket_history:
        by_date.setdefault(e.get("date", ""), []).append(e)
    latest_date = max(by_date, key=lambda d: _date_to_ts(d), default=None)
    prev_snap   = {e["nseCode"]: e for e in by_date.get(latest_date, [])} if latest_date else {}

    unmatched = []
    added_codes: list    = []
    removed_codes: list  = []
    increased_items: list = []
    decreased_items: list = []

    for entry in pdf_entries:
        section = entry["section"]
        if section == "no_change":
            continue  # nothing to update

        nse = _resolve_nse(entry["companyName"], curr_stocks, nse_symbols)
        if not nse:
            unmatched.append(entry["companyName"])
            continue

        w = entry["newWeight"]

        if section == "addition":
            if nse not in stk_map:
                new_entry = {"nseCode": nse, "allocation": round(w / 100, 6), "buyPrice": None,
                             "securityName": entry["companyName"], "segment": entry["holdingType"]}
                curr_stocks.append(new_entry)
                stk_map[nse] = new_entry
            else:
                stk_map[nse]["allocation"] = round(w / 100, 6)
            added_codes.append(nse)
            _apply_bp_event(nse, "buy", w, entry["companyName"], entry["holdingType"])
            rh.setdefault(basket, []).append({
                "date": date_str, "nseCode": nse,
                "securityName": entry["companyName"],
                "segment": entry["holdingType"], "weight": w,
            })

        elif section == "removal":
            # rebalance_history stores "weight" in percentage-point scale
            # (3.0 for 3%); portfolios.json stores "allocation" as a fraction
            # (0.03) -- confirmed live (2026-09-30) that falling back to the
            # latter without converting produced a 100x-too-small weightSold
            # (0.03 instead of 3.0) whenever the removed stock wasn't in the
            # LATEST recorded rebalance_history date (so prev_snap missed it)
            # but was still in the live portfolio.
            prev_entry = prev_snap.get(nse)
            if prev_entry is not None:
                old_weight_pct = float(prev_entry.get("weight", 0) or 0)
            else:
                old_weight_pct = float(stk_map.get(nse, {}).get("allocation", 0) or 0) * 100
            sold.append({
                "nseCode": nse,
                "securityName": entry["companyName"],
                "date": date_str, "action": "Wholly Sold",
                "weightSold": round(old_weight_pct, 2),
                "buyPrice": stk_map[nse].get("buyPrice") if nse in stk_map else None,
                "sellPrice": None,
            })
            curr_stocks = [s for s in curr_stocks if s["nseCode"] != nse]
            stk_map.pop(nse, None)
            removed_codes.append(nse)
            _apply_bp_event(nse, "sell", old_weight_pct, entry["companyName"], entry["holdingType"], series_reset=True)
            rh.setdefault(basket, []).append({
                "date": date_str, "nseCode": nse,
                "securityName": entry["companyName"],
                "segment": entry["holdingType"], "weight": 0,
            })

        elif section == "increase":
            old_w = float((prev_snap.get(nse) or {}).get("weight", 0) or 0)
            if nse in stk_map:
                stk_map[nse]["allocation"] = round(w / 100, 6)
            increased_items.append({"nseCode": nse, "from": old_w, "to": w})
            buy_delta = entry.get("delta") if entry.get("delta") is not None else round(w - old_w, 4)
            _apply_bp_event(nse, "buy", buy_delta, entry["companyName"], entry["holdingType"])
            rh.setdefault(basket, []).append({
                "date": date_str, "nseCode": nse,
                "securityName": entry["companyName"],
                "segment": entry["holdingType"], "weight": w,
            })

        elif section == "decrease":
            old_w = float((prev_snap.get(nse) or {}).get("weight", 0) or 0)
            if nse in stk_map:
                stk_map[nse]["allocation"] = round(w / 100, 6)
            decreased_items.append({"nseCode": nse, "from": old_w, "to": w})
            sold.append({
                "nseCode": nse, "securityName": entry["companyName"],
                "date": date_str, "action": "Partial Sell",
                "weightSold": round(max(old_w - w, 0), 2),
                "buyPrice": None, "sellPrice": None,
            })
            sell_delta = entry.get("delta") if entry.get("delta") is not None else round(old_w - w, 4)
            _apply_bp_event(nse, "sell", sell_delta, entry["companyName"], entry["holdingType"])
            rh.setdefault(basket, []).append({
                "date": date_str, "nseCode": nse,
                "securityName": entry["companyName"],
                "segment": entry["holdingType"], "weight": w,
            })

    portfolios[basket]             = curr_stocks
    portfolios[f"{basket}_sold"]   = sold
    _save_portfolios(portfolios)
    _save_rebalance_history(rh)
    _save_buy_price_data(bp_data)

    sell_codes = removed_codes + [i["nseCode"] for i in decreased_items]
    # Same background steps the Excel-upload path runs after writing events:
    # fetch real OHLC prices for the actual buy/sell dates, recompute each
    # stock's weighted-average buy price from its full event log, refresh
    # the gains statement.
    background_tasks.add_task(_fetch_rebalance_prices, basket, date_str, added_codes, sell_codes)
    background_tasks.add_task(_recalc_basket_buy_prices, basket)
    background_tasks.add_task(_refresh_gains_file)

    resp = {
        "ok": True,
        "basket": BASKET_DISPLAY_NAMES[basket],
        "date": date_str,
        "changes": {
            "added":     added_codes,
            "removed":   removed_codes,
            "increased": [f"{i['nseCode']} ({i['from']}% → {i['to']}%)" for i in increased_items],
            "decreased": [f"{i['nseCode']} ({i['from']}% → {i['to']}%)" for i in decreased_items],
        },
    }
    if unmatched:
        resp["unmatched"] = unmatched
    return resp


@router.post("/api/upload-portfolio-report")
async def upload_portfolio_report(
    background_tasks: BackgroundTasks,
    basket: str = Form(...),
    date: str = Form(...),
    file: UploadFile = File(...),
):
    if basket not in BASKET_DISPLAY_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown basket: {basket}")
    try:
        date_str = datetime.strptime(date.strip(), "%d %b %Y").strftime("%d %b %Y")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date. Use format: 15 Jan 2025")
    raw = await file.read()
    try:
        pdf_entries = _parse_portfolio_pdf(raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read PDF: {e}")
    return await _apply_parsed_entries(background_tasks, basket, date_str, pdf_entries)


async def _fetch_and_parse_smallcase_report(basket: str, auth_header: str | None) -> tuple[str, list]:
    """Shared by preview + (nothing else now, but kept separate so confirm
    never has to re-fetch/re-decrypt): logs into smallcase, clicks through to
    the password-protected 'portfolio report' PDF, decrypts it, and parses
    ONLY the safe bounded section (see smallcase_login.py's long comment on
    why _parse_portfolio_pdf is not used here). Raises HTTPException on any
    failure -- callers don't need their own error handling."""
    result = await smallcase_login.fetch_rebalance_report(basket, auth_header=auth_header)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "Could not fetch the report from smallcase."))

    date_str = result.get("date")
    if not date_str:
        raise HTTPException(status_code=502, detail="Could not read the report's issue date from the PDF.")

    raw = base64.b64decode(result["pdf_base64"])
    from pypdf import PdfReader
    import io as _io
    reader = PdfReader(_io.BytesIO(raw))
    if reader.is_encrypted:
        import os as _os
        if reader.decrypt(_os.environ.get("PORTFOLIO_PDF_PASSWORD", "")) == 0:
            raise HTTPException(status_code=502, detail="Report PDF password did not work -- check PORTFOLIO_PDF_PASSWORD.")
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    pdf_entries = smallcase_login.parse_rebalance_update(text)
    return date_str, pdf_entries


async def _build_preview(basket: str, date_str: str, pdf_entries: list) -> dict:
    """Shared by the on-demand preview endpoint and the daily background
    check -- turns raw parsed entries into the added/removed/increased/
    decreased/unmatched shape the confirm-box modal renders."""
    portfolios  = _load_portfolios()
    curr_stocks = portfolios.get(basket, [])
    nse_symbols = price_engine._nse_symbols_cache
    if not nse_symbols:
        nse_symbols = await _fetch_nse_symbols()

    added, removed, increased, decreased, unmatched = [], [], [], [], []
    for entry in pdf_entries:
        if entry["section"] == "no_change":
            continue
        nse = _resolve_nse(entry["companyName"], curr_stocks, nse_symbols)
        if not nse:
            unmatched.append(entry["companyName"])
            continue
        row = {"nseCode": nse, "companyName": entry["companyName"], "newWeight": entry["newWeight"], "delta": entry.get("delta")}
        {"addition": added, "removal": removed, "increase": increased, "decrease": decreased}[entry["section"]].append(row)

    return {
        "duplicate": False,
        "basket": basket,
        "basketLabel": BASKET_DISPLAY_NAMES[basket],
        "date": date_str,
        "added": added, "removed": removed, "increased": increased, "decreased": decreased,
        "unmatched": unmatched,
        "rawEntries": pdf_entries,  # sent back unchanged on confirm -- no re-fetch needed
    }


@router.post("/api/preview-portfolio-report/{basket}")
async def preview_portfolio_report(basket: str, request: Request):
    """Fetch + parse only -- writes NOTHING. Returns exactly what would
    change (added/removed/increased/decreased, with company names and
    weights) so the admin can review and explicitly confirm before anything
    is applied, same pattern as the Excel-upload preview/confirm flow."""
    _require_admin(request)
    if basket not in BASKET_DISPLAY_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown basket: {basket}")

    auth_header = request.headers.get("Authorization")
    date_str, pdf_entries = await _fetch_and_parse_smallcase_report(basket, auth_header)

    rh = _load_rebalance_history()
    existing_dates = {e.get("date", "").strip() for e in rh.get(basket, [])}
    if date_str in existing_dates:
        return {"duplicate": True, "basket": basket, "date": date_str,
                "message": "This basket's data is already up to date for this report's date."}

    return await _build_preview(basket, date_str, pdf_entries)


@router.get("/api/pending-rebalance-reports")
async def pending_rebalance_reports(request: Request):
    """What the daily background check (main.py's
    _smallcase_rebalance_check_loop) found waiting for review. Read-only --
    the frontend polls this to show a notification badge and opens the same
    PortfolioReportPreviewModal against one of these entries."""
    _require_admin(request)
    pending = _load_pending_rebalance_reports()
    return {"pending": list(pending.values())}


@router.post("/api/dismiss-pending-rebalance-report/{basket}")
async def dismiss_pending_rebalance_report(basket: str, request: Request):
    """Admin looked at it and doesn't want to apply it (yet) -- clears the
    notification without touching any real data. It'll reappear on the next
    daily check if still un-applied by then."""
    _require_admin(request)
    pending = _load_pending_rebalance_reports()
    pending.pop(basket, None)
    _save_pending_rebalance_reports(pending)
    return {"ok": True}


@router.post("/api/confirm-portfolio-report")
async def confirm_portfolio_report(background_tasks: BackgroundTasks, request: Request):
    """Applies a report previously returned by /api/preview-portfolio-report
    -- the actual write step, only reached after the admin has seen the diff
    and explicitly confirmed it."""
    _require_admin(request)
    body      = await request.json()
    basket    = body.get("basket", "")
    date_str  = body.get("date", "")
    pdf_entries = body.get("rawEntries", [])
    if basket not in BASKET_DISPLAY_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown basket: {basket}")
    if not date_str or not pdf_entries:
        raise HTTPException(status_code=400, detail="Missing date or entries to apply -- preview again first.")
    result = await _apply_parsed_entries(background_tasks, basket, date_str, pdf_entries)
    pending = _load_pending_rebalance_reports()
    if pending.pop(basket, None) is not None:
        _save_pending_rebalance_reports(pending)
    return result
