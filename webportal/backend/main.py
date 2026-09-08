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


# Daily smallcase auto-fetch: pulls each basket's latest index + benchmark
# values into historical_index.json once a day, using whatever session is
# already saved on disk (smallcase_session_profile/) -- no OTP involved here,
# since there's no admin present to type one in. If that session has expired,
# this just logs a warning and skips; an admin re-logging in via the
# "smallcase Login" button restores it for the next run. Runs in THIS
# process specifically (not backend/main.py) because Playwright's browser
# launch only works when cwd matches this file's own directory -- see
# smallcase_login.py's module docstring for the full story.
#
# Scheduled as a task on the app's OWN event loop (asyncio.create_task at
# startup), NOT a separate OS thread with its own asyncio.new_event_loop()
# -- smallcase_login.py's module-level asyncio.Lock() and browser/page state
# are singletons that bind to whichever event loop first awaits them. A
# background thread's independent loop grabbing that lock first (its 60s
# initial delay meant it usually won the race against a real button click)
# permanently bound it to that thread's loop, which then closed -- so every
# subsequent real HTTP request's attempt to acquire the SAME lock on the
# main loop failed with "bound to a different event loop", turning genuine
# "Fetch Latest Daily Values" clicks into 500s. Running on the main loop
# throughout avoids ever having two loops touch this shared state at all.
async def _smallcase_daily_fetch_loop():
    await asyncio.sleep(60)
    while True:
        try:
            import smallcase_login
            if not await smallcase_login.login_status():
                print("[BG] smallcase daily fetch skipped -- not logged in (session expired or never started).")
            else:
                print("[BG] Running daily smallcase fetch...")
                result = await smallcase_login.fetch_daily_values()
                print(f"[BG] smallcase daily fetch finished: {result}")
        except Exception as bg_err:
            print(f"[BG] Error in smallcase daily fetch: {bg_err}")
        await asyncio.sleep(86400)

@app.on_event("startup")
async def _start_smallcase_daily_fetch():
    asyncio.get_event_loop().create_task(_smallcase_daily_fetch_loop())


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
