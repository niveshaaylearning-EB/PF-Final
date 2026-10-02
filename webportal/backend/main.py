"""
FastAPI backend for the Equity Basket Performance Tracker.
# v3 — history derived from buy/sell events

Live data sources:
  - Yahoo Finance v8/chart  → CMP, Open1M, High1M, Low1M  (chart API, no auth required)
  - Screener.in HTML scrape → Market Cap (Cr), Stock P/E   (reliable for Indian stocks)

Both sources are fetched in parallel and merged.  Results cached for 15 minutes.

Endpoints:
  GET  /api/baskets          → { key: displayName, ... }
  GET  /api/basket/{key}     → { stocks, history, buyPriceDetails }
  PUT  /api/basket/{key}     → save updated basket
  GET  /api/live             → full live-data dict (cached 15 min)
  GET  /api/live/{nse_code}  → single-stock live data
  GET  /health               → { "status": "ok" }
"""

import asyncio
import csv
import io
import json
import re
import time
import urllib.parse
from datetime import date as _date, datetime, timezone
import os
from pathlib import Path
from typing import Optional

import httpx
import openpyxl
from fastapi import BackgroundTasks, Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from pypdf import PdfReader
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse


import base64 as _b64

# Make backend/common importable regardless of how this app is launched:
# when merged in-process (backend/main.py's importlib load), backend/main.py
# already puts backend/ on sys.path; but run.py's local-dev mode also runs
# this file standalone (`uvicorn main:app` with cwd=webportal/backend), where
# backend/ is never otherwise on sys.path.
import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'backend'))

# Load secrets (GITHUB_TOKEN, PORTFOLIO_PDF_PASSWORD, ...) from the repo-root
# .env -- the single source of truth for all credentials. When merged
# in-process, backend/auth.py already calls this; but run.py's local-dev
# mode runs this file standalone, where bare load_dotenv() would never find
# the root .env (it's two directories up, not an ancestor of the cwd).
from dotenv import load_dotenv as _load_dotenv
_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '.env'))

from common.admin import is_admin_email
from persistence import (
    BASKET_DISPLAY_NAMES,
    _PORTFOLIOS_FILE, _BUY_PRICE_FILE, _RH_FILE, _GAINS_FILE, _HIST_INDEX_FILE,
    _UNDO_FILE, _ROLLBACK_FILE, _MAX_ROLLBACK_PTS, _ACTIVITY_LOG_FILE,
    _get_request_email, _require_admin, _log_activity,
    _load_portfolios, _save_portfolios, _save_and_push,
    _load_buy_price_data, _save_buy_price_data,
    _load_rebalance_history, _save_rebalance_history,
    _save_gains, _load_historical_index, _save_historical_index,
    _load_undo_snapshots, _save_undo_snapshots,
    _auto_save_rollback, _push_undo_snapshot, _all_nse_codes,
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

from config import LIVE_TTL, YF_HEADERS, YF_SYMBOL_MAP, SCREENER_HEADERS

# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Equity Basket API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Feature routers ────────────────────────────────────────────────────────
# NOTE: this file is loaded by backend/main.py via importlib with a custom
# spec name -- it is never registered in sys.modules as "main" (backend's
# own main.py already owns that name in the shared process). So these
# routers get shared state from persistence.py/config.py, never `from main
# import X` (that would silently resolve to the unrelated outer backend).
from price_engine import router as _price_engine_router
app.include_router(_price_engine_router)

from buy_price_gains import router as _buy_price_gains_router
app.include_router(_buy_price_gains_router)

from corporate_actions import router as _corporate_actions_router
app.include_router(_corporate_actions_router)

from historical_data import router as _historical_data_router
app.include_router(_historical_data_router)

from live_data import router as _live_data_router
app.include_router(_live_data_router)

from rebalance import router as _rebalance_router
app.include_router(_rebalance_router)


from portfolio_report import router as _portfolio_report_router
app.include_router(_portfolio_report_router)

from rollback import router as _rollback_router
app.include_router(_rollback_router)

from watchlist import router as _watchlist_router
app.include_router(_watchlist_router)


# Daily smallcase auto-fetch + rebalance CHECK, both at 11:00 (server's local
# clock), one after the other in the same run:
#   1. pulls each basket's latest index + benchmark values into
#      historical_index.json (smallcase_login.fetch_daily_values) --
#      unconditionally writes, same as the manual "Fetch Latest Daily
#      Values" button.
#   2. fetches+parses the same "Latest Rebalance Update" report the manual
#      "Fetch from smallcase" button uses for every subscribed basket, and
#      -- if it's a new (non-duplicate) date -- writes the resulting preview
#      into pending_rebalance_reports.json and stops there. Nothing is ever
#      written to portfolios/rebalance_history/buy_price_data from this
#      step; the admin still has to open the preview and click Confirm,
#      same safety gate as the manual button (explicit user choice:
#      "Auto-check only, notify me" over full auto-apply).
#   3. refreshes all 8 tracked COMPETITOR smallcases (competitor_login.py --
#      its own separate server-side session/profile) via real Playwright
#      navigation to each one's /constituents page, same as the manual
#      "Fetch Competitor Data" button. Pure read-only data caching into
#      competitor_data.json, no confirm-box needed since nothing of ours is
#      written/changed.
# Steps 1-2 use whatever smallcase session is already saved on disk
# (smallcase_session_profile/) -- no OTP involved here, since there's no
# admin present to type one in. If that session has expired, this just logs
# a warning and skips both steps; an admin re-logging in via the "smallcase
# Login" button restores it for the next run. Runs in THIS process
# specifically (not backend/main.py) because Playwright's browser launch
# only works when cwd matches this file's own directory -- see
# smallcase_login.py's module docstring for the full story.
#
# Scheduled as a task on the app's OWN event loop (asyncio.create_task at
# startup), NOT a separate OS thread with its own asyncio.new_event_loop()
# -- smallcase_login.py's module-level asyncio.Lock() and browser/page state
# are singletons that bind to whichever event loop first awaits them. A
# background thread's independent loop grabbing that lock first permanently
# bound it to that thread's loop, which then closed -- so every subsequent
# real HTTP request's attempt to acquire the SAME lock on the main loop
# failed with "bound to a different event loop", turning genuine "Fetch
# Latest Daily Values" clicks into 500s. Running on the main loop throughout
# avoids ever having two loops touch this shared state at all. (Previously
# two separate loops on two separate schedules -- merged into one run at
# 11:00 per request, so both use the same freshly-confirmed login state.)
def _seconds_until_next_11am() -> float:
    from datetime import datetime, timedelta
    now = datetime.now()
    target = now.replace(hour=11, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()

async def _smallcase_rebalance_check_loop():
    await asyncio.sleep(_seconds_until_next_11am())
    while True:
        try:
            import smallcase_login
            from portfolio_report import _fetch_and_parse_smallcase_report, _build_preview
            from persistence import _load_rebalance_history, _load_pending_rebalance_reports, _save_pending_rebalance_reports

            if not await smallcase_login.login_status():
                print("[BG] smallcase daily check skipped -- not logged in (session expired or never started).")
            else:
                print("[BG] Running daily smallcase fetch (index/benchmark values)...")
                try:
                    values_result = await smallcase_login.fetch_daily_values()
                    print(f"[BG] smallcase daily values fetch finished: {values_result}")
                except Exception as values_err:
                    print(f"[BG] Error in smallcase daily values fetch: {values_err}")

                print("[BG] Running daily smallcase rebalance check...")
                rh = _load_rebalance_history()
                pending = _load_pending_rebalance_reports()
                changed = False
                for basket in smallcase_login.BASKET_SMALLCASE_MAP:
                    try:
                        date_str, pdf_entries = await _fetch_and_parse_smallcase_report(basket, auth_header=None)
                        existing_dates = {e.get("date", "").strip() for e in rh.get(basket, [])}
                        if date_str in existing_dates:
                            if pending.pop(basket, None) is not None:
                                changed = True
                            continue
                        preview = await _build_preview(basket, date_str, pdf_entries)
                        if preview.get("date") != pending.get(basket, {}).get("date"):
                            print(f"[BG] New pending rebalance found for {basket}: {date_str}")
                        pending[basket] = preview
                        changed = True
                    except Exception as basket_err:
                        print(f"[BG] Rebalance check failed for {basket}: {basket_err}")
                if changed:
                    _save_pending_rebalance_reports(pending)
                print("[BG] Daily smallcase rebalance check finished.")

            import competitor_login
            import price_engine
            from persistence import _load_competitor_data, _save_competitor_data
            print("[BG] Running daily competitor smallcase fetch...")
            try:
                outcome = await competitor_login.fetch_all_competitors()
                if not outcome.get("ok"):
                    print(f"[BG] competitor fetch skipped: {outcome.get('error')}")
                else:
                    nse_symbols = price_engine._nse_symbols_cache
                    if not nse_symbols:
                        nse_symbols = await price_engine._fetch_nse_symbols()
                    cached = _load_competitor_data()
                    from datetime import datetime as _dt, timezone as _tz
                    now_str = _dt.now(_tz.utc).strftime("%d %b %Y %H:%M UTC")
                    for key, snap in outcome.get("results", {}).items():
                        if not snap.get("ok"):
                            print(f"[BG] competitor fetch failed for {key}: {snap.get('error')}")
                            continue
                        cfg = competitor_login.COMPETITOR_SMALLCASE_MAP[key]
                        stocks = []
                        for s in (snap.get("stocks") or []):
                            nse = price_engine._resolve_nse(s.get("name", ""), [], nse_symbols)
                            stocks.append({"name": s.get("name"), "weight": s.get("weight"), "nseCode": nse, "sector": s.get("sector"), "capSegment": s.get("capSegment")})
                        full_history = competitor_login.diff_historical_constituents(snap.get("historicalConstituents") or [])
                        for h in full_history:
                            for c in h["changes"]:
                                c["nseCode"] = price_engine._resolve_nse(c["name"], [], nse_symbols)

                        # WhatsApp alert on a COMPETITOR rebalance (not ours --
                        # per explicit correction, this is the one case that
                        # should notify). full_history is newest-first (see
                        # diff_historical_constituents), so [0] is the latest
                        # period; only fires when that date is NEW compared to
                        # what was cached before THIS fetch overwrites it below
                        # -- old_entry being absent (this competitor's very
                        # first successful fetch, nothing to compare against
                        # yet) deliberately does NOT fire, to avoid an initial
                        # flood of alerts for all 8 competitors at once.
                        old_entry = cached.get(key)
                        old_latest_date = ((old_entry or {}).get("fullRebalanceHistory") or [{}])[0].get("date") if old_entry else None
                        new_latest_date = full_history[0]["date"] if full_history else None
                        if old_entry and new_latest_date and new_latest_date != old_latest_date:
                            print(f"[BG] New competitor rebalance found for {key}: {new_latest_date}")
                            try:
                                import doubletick
                                changes_by_status = {"new": [], "increased": [], "decreased": [], "removed": []}
                                for c in full_history[0]["changes"]:
                                    status = c.get("status")
                                    if status not in changes_by_status:
                                        continue
                                    old_w, new_w = c.get("oldWeight"), c.get("newWeight")
                                    delta = (new_w - old_w) if (old_w is not None and new_w is not None) else None
                                    changes_by_status[status].append({"companyName": c.get("name"), "newWeight": new_w, "delta": delta})
                                comp_preview = {
                                    "added": changes_by_status["new"], "increased": changes_by_status["increased"],
                                    "decreased": changes_by_status["decreased"], "removed": changes_by_status["removed"],
                                }
                                recipients = doubletick.get_recipients()
                                for r in recipients:
                                    wa_result = await doubletick.send_rebalance_alert(r["phone"], cfg["label"], comp_preview)
                                    print(f"[BG] WhatsApp alert ({key}) to {r.get('name') or r['phone']}: {wa_result}")
                            except Exception as wa_err:
                                print(f"[BG] WhatsApp alert failed for competitor {key}: {wa_err}")

                        cached[key] = {
                            "key": key, "label": cfg["label"], "manager": cfg["manager"],
                            "stocks": stocks, "cagr": snap.get("cagr"),
                            "rebalanceTimeline": snap.get("rebalanceTimeline"),
                            "fullRebalanceHistory": full_history,
                            "marketCapMix": snap.get("marketCapMix"),
                            "performanceSeries": snap.get("performanceSeries"),
                            "launchDate": snap.get("launchDate"),
                            "latestRebalanceDetail": snap.get("latestRebalanceDetail"),
                            "lastFetched": now_str,
                        }
                    _save_competitor_data(cached)
                    print("[BG] Daily competitor smallcase fetch finished.")
            except Exception as comp_err:
                print(f"[BG] Error in competitor smallcase fetch: {comp_err}")
        except Exception as bg_err:
            print(f"[BG] Error in smallcase rebalance check: {bg_err}")
        await asyncio.sleep(_seconds_until_next_11am())

@app.on_event("startup")
async def _start_smallcase_rebalance_check():
    asyncio.get_event_loop().create_task(_smallcase_rebalance_check_loop())


# ─────────────────────────────────────────────────────────────────────────────
# Serve React frontend (SPA) from ../frontend/dist
# ─────────────────────────────────────────────────────────────────────────────

_DIST        = Path(__file__).parent.parent / "frontend" / "dist"

_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}

if _DIST.is_dir():
    # Assets served at both /assets/ (local) and /wp/assets/ (cloud via proxy)
    async def _serve_asset_file(asset_path: str):
        file = _DIST / "assets" / asset_path
        if not file.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(str(file), headers=_NO_CACHE)

    @app.get("/assets/{asset_path:path}", include_in_schema=False)
    async def serve_asset(asset_path: str):
        return await _serve_asset_file(asset_path)

    @app.get("/wp/assets/{asset_path:path}", include_in_schema=False)
    async def serve_wp_asset(asset_path: str):
        return await _serve_asset_file(asset_path)

    @app.get("/{_path:path}", include_in_schema=False)
    async def spa_fallback(_path: str = ""):
        if _path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(str(_DIST / "index.html"), headers=_NO_CACHE)
