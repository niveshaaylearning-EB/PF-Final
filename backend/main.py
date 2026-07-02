from fastapi import FastAPI, Depends, HTTPException, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
import asyncio
import threading
import tempfile
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import re
import json
import os
from io import BytesIO
from fastapi.responses import StreamingResponse

# Ensure backend/ itself is importable as a package root regardless of how
# uvicorn was launched, so `from common.xxx import ...` resolves for both
# this file and the webportal sub-app loaded further below (shared sys.path).
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database
from database import StockEvent
import sheet_service
# Lazy yfinance wrapper — defers the ~50MB import to first use, reducing startup memory
class _LazyYF:
    _mod = None
    def __getattr__(self, name):
        if _LazyYF._mod is None:
            import yfinance as _m
            _LazyYF._mod = _m
        return getattr(_LazyYF._mod, name)

yf = _LazyYF()
_YF_AVAILABLE = True

# Suppress yfinance 401 crumb errors — common on cloud IPs
import warnings
warnings.filterwarnings("ignore", message=".*crumb.*")
warnings.filterwarnings("ignore", message=".*401.*")

# ── NSE live price via nsepython (works on cloud, no Yahoo crumb needed) ──────
def _get_nse_ltp(nse_code: str) -> float | None:
    """Fetch live price for an NSE stock using NSE India's own API via nsepython."""
    try:
        from nsepython import nse_quote_ltp
        price = nse_quote_ltp(nse_code.upper().replace(".NS", ""))
        return float(price) if price else None
    except Exception:
        return None

def _get_nse_quote(nse_code: str) -> dict | None:
    """Fetch full quote (ltp, open, high, low, previousClose) from NSE."""
    try:
        from nsepython import nse_eq
        data = nse_eq(nse_code.upper().replace(".NS", ""))
        pd_data = data.get("priceInfo", {})
        return {
            "cmp":   float(pd_data.get("lastPrice", 0) or 0) or None,
            "open":  float(pd_data.get("open",      0) or 0) or None,
            "high":  float(pd_data.get("intraDayHighLow", {}).get("max", 0) or 0) or None,
            "low":   float(pd_data.get("intraDayHighLow", {}).get("min", 0) or 0) or None,
            "prev":  float(pd_data.get("previousClose", 0) or 0) or None,
        }
    except Exception:
        return None
from datetime import datetime, timedelta
from auth import verify_token, router as auth_router, get_location_from_ip, is_admin_email
from common.admin import ADMIN_EMAILS
import time as _time
from sqlalchemy import func as _sqf
import requests as _http

# ── Webportal ASGI sub-app (merged; no separate process) ─────────────────────
# importlib avoids the 'main' name clash: uvicorn already registered this file
# as sys.modules['main'], so a plain `import main` would return itself.
import importlib.util as _ilu

_wp_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'webportal', 'backend', 'main.py')
_sys.path.insert(0, os.path.dirname(_wp_path))
_wp_spec = _ilu.spec_from_file_location('webportal_main', _wp_path)
_wp_module = _ilu.module_from_spec(_wp_spec)
_wp_spec.loader.exec_module(_wp_module)

app = FastAPI()

# ── Slowapi rate-limiter (instance defined in auth.py) ────────────────────────
from auth import _limiter
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
app.state.limiter = _limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Security headers middleware ───────────────────────────────────────────────
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"]  = "nosniff"
    response.headers["X-Frame-Options"]         = "SAMEORIGIN"
    response.headers["Referrer-Policy"]         = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"]        = "1; mode=block"
    response.headers["Permissions-Policy"]      = "geolocation=(), microphone=(), camera=()"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

# ── JWT middleware — protects all /api/* routes ───────────────────────────────
class JWTMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # JWT check only for /api/* routes (not auth, not public endpoints)
        _PUBLIC_API = {"/api/health", "/api/access-requests"}
        if path.startswith("/api/") and path not in _PUBLIC_API:
            if request.method != "OPTIONS":
                header = request.headers.get("Authorization", "")
                if not header.startswith("Bearer "):
                    return JSONResponse({"detail": "Not authenticated"}, status_code=401)
                try:
                    email = verify_token(header.split(" ", 1)[1])
                    request.state.user = email
                except Exception:
                    return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)

        response = await call_next(request)

        # After successful login/logout → persist to GitHub (survives future deploys)
        if path in ("/auth/login", "/auth/direct-login", "/auth/logout") and response.status_code == 200:
            try:
                db_bg = database.SessionLocal()
                _dump_login_history(db_bg)
                _dump_audit_log(db_bg)
                db_bg.close()
            except Exception:
                pass

        return response

# Middleware order: innermost first, outermost last (Starlette applies in reverse)
app.add_middleware(GZipMiddleware, minimum_size=500)
cors_origins_str = os.environ.get("CORS_ORIGINS", "")
if cors_origins_str:
    cors_origins = [origin.strip() for origin in cors_origins_str.split(",")]
else:
    cors_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True if cors_origins != ["*"] else False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(JWTMiddleware)   # runs first on inbound requests

app.include_router(auth_router)

# Thread pool for running blocking I/O (yfinance, sheet fetches) without
# blocking FastAPI's async event loop.
_io_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="nia-io")

# ── Historic-return cache ─────────────────────────────────────────────────────
# yf.download(period='5y') for 15 stocks takes 5–15 seconds on a slow connection.
# Cache the result for 6 hours — prices don't move meaningfully for these
# analytics within a single trading session.
#
# IMPORTANT: The cache is PERSISTED to disk (historic_cache.json) so it
# survives server restarts. Without this, every restart would cause a
# 10-20 second cold-start delay while yfinance re-downloads 5 years of data.
_HISTORIC_TTL = 6 * 3600            # 6 hours in seconds
_HISTORIC_CACHE_FILE = os.path.join(os.path.dirname(__file__), 'historic_cache.json')

def _load_disk_cache() -> dict:
    """Load the persisted historic cache from disk at startup."""
    try:
        if os.path.exists(_HISTORIC_CACHE_FILE):
            with open(_HISTORIC_CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"[cache] Could not load historic_cache.json: {e}")
    return {}

def _save_disk_cache(cache: dict):
    """Persist the historic cache to disk atomically (temp file + rename, thread-safe)."""
    with _historic_cache_lock:
        try:
            dir_ = os.path.dirname(_HISTORIC_CACHE_FILE) or '.'
            fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix='.tmp')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(cache, f)
                os.replace(tmp_path, _HISTORIC_CACHE_FILE)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            print(f"[cache] Could not save historic_cache.json: {e}")

_historic_cache: dict = _load_disk_cache()   # key → {time, data}
_historic_sim_cache: dict = {}               # simulator key → {time, data} (not persisted)
_historic_cache_lock = threading.Lock()      # guards concurrent disk writes
_HIST_SIM_VER = "v4"                         # bump to bust old cached yfinance data

def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ── Persistent JSON storage for access requests & approved emails ─────────────
# SQLite resets on every Render deploy; JSON files persist via GitHub auto-push.
# Defined early (before feature routers are imported below) since several
# routers and the startup event both need these.

from common.json_store import save_json as _common_save_json, load_json as _load_json_file

_BACKEND_DIR        = os.path.dirname(os.path.abspath(__file__))
_ACCESS_REQ_FILE    = os.path.join(_BACKEND_DIR, "access_requests.json")
_ALLOWED_EMAIL_FILE = os.path.join(_BACKEND_DIR, "allowed_emails_data.json")
_LOGIN_HISTORY_FILE  = os.path.join(_BACKEND_DIR, "login_history.json")
_AUDIT_LOG_FILE      = os.path.join(_BACKEND_DIR, "audit_log.json")
_STOCK_EVENTS_FILE   = os.path.join(_BACKEND_DIR, "stock_events.json")

def _save_json_push(filepath: str, data, sync: bool = False, raise_on_error: bool = False):
    rel_path = f"backend/{os.path.basename(filepath)}"
    _common_save_json(filepath, data, rel_path, sync=sync, raise_on_error=raise_on_error)

def _sync_access_requests_to_db(db):
    """Load persisted access requests into DB if they don't exist yet."""
    data = _load_json_file(_ACCESS_REQ_FILE, [])
    for r in data:
        if not db.query(database.AccessRequest).filter_by(email=r["email"], status=r["status"]).first():
            db.add(database.AccessRequest(email=r["email"], requested_at=r.get("requested_at",""),
                                          status=r["status"], processed_at=r.get("processed_at")))
    db.commit()

def _dump_access_requests(db):
    rows = db.query(database.AccessRequest).filter(database.AccessRequest.status.in_(["pending","approved","rejected"])).all()
    data = [{"email": r.email, "requested_at": r.requested_at, "status": r.status,
             "processed_at": r.processed_at} for r in rows]
    _save_json_push(_ACCESS_REQ_FILE, data)

def _dump_allowed_emails(db, raise_on_error: bool = False):
    rows = db.query(database.AllowedEmail).all()
    data = [{
        "email":         r.email,
        "added_by":      r.added_by,
        "added_at":      r.added_at,
        "totp_secret":   r.totp_secret,
        "totp_enabled":  r.totp_enabled,
        "backup_codes":  r.backup_codes,
        "first_name":    r.first_name,
        "last_name":     r.last_name,
        "password_hash": r.password_hash,
        "is_approved":   r.is_approved if r.is_approved is not None else 1,
    } for r in rows]
    _save_json_push(_ALLOWED_EMAIL_FILE, data, sync=True, raise_on_error=raise_on_error)


def _dump_login_history(db):
    """Persist last 500 login events to GitHub."""
    rows = db.query(database.LoginHistory).order_by(database.LoginHistory.id.desc()).limit(500).all()
    data = [{"email": r.email, "logged_at": r.logged_at, "ip_address": r.ip_address, "location": r.location} for r in rows]
    _save_json_push(_LOGIN_HISTORY_FILE, data)


def _dump_audit_log(db):
    """Persist last 300 audit events to GitHub."""
    rows = db.query(database.AuditLog).order_by(database.AuditLog.id.desc()).limit(300).all()
    data = [{"user_email": r.user_email, "event_type": r.event_type,
             "details": r.details, "created_at": r.created_at,
             "ip_address": r.ip_address, "location": r.location} for r in rows]
    _save_json_push(_AUDIT_LOG_FILE, data)


def _dump_stock_events(db):
    """Persist last 1000 stock events to GitHub."""
    rows = db.query(StockEvent).order_by(StockEvent.id.desc()).limit(1000).all()
    data = [{"basket_id": r.basket_id, "stock_code": r.stock_code,
             "event_type": r.event_type, "description": r.description,
             "old_value": r.old_value, "new_value": r.new_value,
             "event_date": r.event_date, "user_email": getattr(r, "user_email", None)} for r in rows]
    _save_json_push(_STOCK_EVENTS_FILE, data)


# ── Startup pre-warm ──────────────────────────────────────────────────────────
@app.on_event("startup")
async def _startup_prewarm():
    """Warm basket + historic caches immediately on startup in background.
    Also ensures admin email is always in the allowed_emails table."""
    # Fetch latest files from GitHub before restoring (ensures passwords survive deploys
    # even when local Docker image has a stale copy of the JSON files)
    _gh_token = os.environ.get("GITHUB_TOKEN", "")
    _gh_repo  = os.environ.get("GITHUB_REPO", "")
    if _gh_token and _gh_repo:
        import urllib.request as _ur_startup
        import base64 as _b64_startup
        for _fname in ("allowed_emails_data.json", "login_history.json", "audit_log.json", "stock_events.json"):
            try:
                _api = f"https://api.github.com/repos/{_gh_repo}/contents/backend/{_fname}"
                _hdrs = {"Authorization": f"Bearer {_gh_token}", "Accept": "application/vnd.github+json"}
                _resp = _ur_startup.urlopen(_ur_startup.Request(_api, headers=_hdrs), timeout=10)
                _gh_data = json.loads(_resp.read())
                _content = _b64_startup.b64decode(_gh_data["content"].replace("\n", "")).decode()
                _local = os.path.join(_BACKEND_DIR, _fname)
                with open(_local, "w", encoding="utf-8") as _f:
                    _f.write(_content)
                print(f"[startup] Fetched fresh {_fname} from GitHub")
            except Exception as _e:
                print(f"[startup] Could not fetch {_fname} from GitHub: {_e}")
    else:
        print("[startup] WARNING: GITHUB_TOKEN or GITHUB_REPO not set — using local JSON files (passwords may be stale)")

    # Restore persisted allowed_emails + access_requests from JSON (survives deploys)
    try:
        db_s = database.SessionLocal()
        # Restore allowed emails
        for rec in _load_json_file(_ALLOWED_EMAIL_FILE, []):
            if rec.get("email"):
                existing = db_s.query(database.AllowedEmail).filter_by(email=rec["email"]).first()
                if not existing:
                    db_s.add(database.AllowedEmail(
                        email=rec["email"],
                        added_by=rec.get("added_by","restored"),
                        added_at=rec.get("added_at",""),
                        totp_secret=rec.get("totp_secret"),
                        totp_enabled=rec.get("totp_enabled", 0),
                        backup_codes=rec.get("backup_codes"),
                        first_name=rec.get("first_name"),
                        last_name=rec.get("last_name"),
                        password_hash=rec.get("password_hash"),
                        is_approved=rec.get("is_approved", 1),
                    ))
                else:
                    if "totp_secret" in rec:
                        existing.totp_secret = rec["totp_secret"]
                    if "totp_enabled" in rec:
                        existing.totp_enabled = rec["totp_enabled"]
                    if "backup_codes" in rec:
                        existing.backup_codes = rec["backup_codes"]
                    if "first_name" in rec:
                        existing.first_name = rec["first_name"]
                    if "last_name" in rec:
                        existing.last_name = rec["last_name"]
                    if "password_hash" in rec:
                        existing.password_hash = rec["password_hash"]
                    if "is_approved" in rec:
                        existing.is_approved = rec["is_approved"]
        # Restore access requests
        _sync_access_requests_to_db(db_s)
        # Restore login history
        for rec in _load_json_file(_LOGIN_HISTORY_FILE, []):
            if rec.get("logged_at") and not db_s.query(database.LoginHistory).filter_by(
                    email=rec.get("email"), logged_at=rec.get("logged_at")).first():
                db_s.add(database.LoginHistory(email=rec.get("email",""),
                    logged_at=rec.get("logged_at",""), ip_address=rec.get("ip_address"),
                    location=rec.get("location","")))
        # Restore audit log
        for rec in _load_json_file(_AUDIT_LOG_FILE, []):
            if rec.get("created_at") and not db_s.query(database.AuditLog).filter_by(
                    user_email=rec.get("user_email"), created_at=rec.get("created_at")).first():
                db_s.add(database.AuditLog(user_email=rec.get("user_email",""),
                    event_type=rec.get("event_type",""), details=rec.get("details"),
                    created_at=rec.get("created_at",""), ip_address=rec.get("ip_address"),
                    location=rec.get("location","")))
        # Restore stock events
        for rec in _load_json_file(_STOCK_EVENTS_FILE, []):
            if rec.get("event_date") and not db_s.query(StockEvent).filter_by(
                    basket_id=rec.get("basket_id"), stock_code=rec.get("stock_code"),
                    event_date=rec.get("event_date"), event_type=rec.get("event_type")).first():
                db_s.add(StockEvent(
                    basket_id=rec.get("basket_id",""), stock_code=rec.get("stock_code",""),
                    event_type=rec.get("event_type",""), description=rec.get("description"),
                    old_value=rec.get("old_value"), new_value=rec.get("new_value"),
                    event_date=rec.get("event_date",""), user_email=rec.get("user_email")))
        # Always ensure admins exist and are approved
        for adm in ADMIN_EMAILS:
            adm_row = db_s.query(database.AllowedEmail).filter_by(email=adm).first()
            if not adm_row:
                db_s.add(database.AllowedEmail(email=adm, added_by="system",
                                               added_at=datetime.utcnow().isoformat(), is_approved=1))
            elif not adm_row.is_approved:
                adm_row.is_approved = 1
        db_s.commit()
        db_s.close()
        print("[startup] Restored allowed_emails and access_requests from JSON")
    except Exception as e:
        print(f"[startup] Could not restore persisted data: {e}")

    # Background: periodically dump all backlog data to GitHub every 5 min
    import threading as _t
    def _periodic_dump():
        import time as _time2
        while True:
            _time2.sleep(300)  # 5 minutes
            try:
                _db = database.SessionLocal()
                _dump_stock_events(_db)
                _dump_login_history(_db)
                _dump_audit_log(_db)
                _db.close()
            except Exception:
                pass
    _t.Thread(target=_periodic_dump, daemon=True).start()

    loop = asyncio.get_running_loop()
    async def _warm():
        try:
            print("[prewarm] Warming all caches in parallel...")
            await asyncio.gather(
                loop.run_in_executor(_io_pool, sheet_service.get_all_baskets),
                loop.run_in_executor(_io_pool, _fetch_all_webportal_baskets),
                loop.run_in_executor(_io_pool, _fetch_index_history),
                return_exceptions=True,
            )
            print("[prewarm] All caches ready.")
        except Exception as e:
            print(f"[prewarm] Error: {e}")
    loop.create_task(_warm())

    # Start Results Calendar background daily refresher thread
    import threading
    def _calendar_bg_refresh_worker():
        # Wait 45 seconds to let other startup prewarm tasks run
        _time.sleep(45)
        while True:
            try:
                print("[BG] Running daily results calendar cache update...")
                loop_bg = asyncio.new_event_loop()
                async def _run():
                    db = database.SessionLocal()
                    try:
                        await _refresh_results_calendar_data(db)
                        print("[BG] Daily results calendar cache update finished successfully.")
                    finally:
                        db.close()
                loop_bg.run_until_complete(_run())
                loop_bg.close()
            except Exception as bg_err:
                print(f"[BG] Error in daily results calendar refresh: {bg_err}")
            # Sleep for 24 hours
            _time.sleep(86400)

    bg_thread = threading.Thread(target=_calendar_bg_refresh_worker, daemon=True, name="results-calendar-refresh")
    bg_thread.start()

class RationaleCreate(BaseModel):
    stock_code: str
    rationale_text: str

class SimulationModCreate(BaseModel):
    basket_id: str
    stock_code: str
    override_type: str # 'add', 'modify', 'remove', 'delete'
    formula: Optional[str] = None
    allocation: Optional[float] = None
    buy_price: Optional[float] = None
    cmp: Optional[float] = None

class SimulationSipCreate(BaseModel):
    sip_date: str
    amount: float

# ── Feature routers ────────────────────────────────────────────────────────
# Split out of this file for organization; each does `from main import X` for
# shared state (get_db, caches, etc.) defined above, which is safe here since
# main.py has already finished defining everything these routers import by
# this point in its own top-to-bottom execution.
from routers.actual_portfolio_bridge import (
    router as _actual_portfolio_bridge_router,
    _fetch_all_webportal_baskets,
    _fetch_index_history,
)
app.include_router(_actual_portfolio_bridge_router)

from routers.stocks import router as _stocks_router, _stock_hist_price_cache, _STOCK_HIST_PRICE_TTL
app.include_router(_stocks_router)

@app.api_route("/api/health", methods=["GET", "HEAD"])
def health(): return {"status": "ok"}

from routers.historic import router as _historic_router
app.include_router(_historic_router)

from routers.simulator import router as _simulator_router
app.include_router(_simulator_router)

from routers.exports import router as _exports_router
app.include_router(_exports_router)


from routers.holdings import router as _holdings_router, _resolve_basket
app.include_router(_holdings_router)


# ── Admin: force-push all persistent data to GitHub ──────────────────────────

@app.post("/api/admin/sync-to-github")
def admin_sync_to_github(request: Request, db: Session = Depends(get_db)):
    """Force-push all JSON data to GitHub and report success/failure per file."""
    user = getattr(request.state, "user", None)
    if not is_admin_email(user):
        raise HTTPException(status_code=403, detail="Admin only")

    token = os.environ.get("GITHUB_TOKEN", "")
    repo  = os.environ.get("GITHUB_REPO", "")
    results = {
        "GITHUB_TOKEN_set": bool(token),
        "GITHUB_REPO_set":  bool(repo),
        "GITHUB_REPO":      repo or "(not set)",
        "files": {}
    }

    if not token or not repo:
        results["error"] = "GITHUB_TOKEN or GITHUB_REPO not set on Render. Update env vars."
        return results

    import urllib.request as _sync_ur
    import base64 as _sync_b64

    def _direct_push(filename: str, data_fn) -> str:
        """Push file synchronously and return 'ok:<sha>' or 'error:<msg>'."""
        try:
            content = json.dumps(data_fn(db), indent=2, ensure_ascii=False)
            api_url = f"https://api.github.com/repos/{repo}/contents/backend/{filename}"
            hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"}
            try:
                with _sync_ur.urlopen(_sync_ur.Request(api_url, headers=hdrs), timeout=8) as r:
                    sha = json.loads(r.read())["sha"]
            except Exception as e:
                sha = None
                results.setdefault("warnings", []).append(f"GET {filename}: {e}")
            body = json.dumps({"message": f"auto: update {filename}",
                               "content": _sync_b64.b64encode(content.encode()).decode(),
                               **( {"sha": sha} if sha else {})}).encode()
            with _sync_ur.urlopen(_sync_ur.Request(api_url, data=body, headers=hdrs, method="PUT"), timeout=15) as r:
                resp_data = json.loads(r.read())
                new_sha = resp_data.get("content", {}).get("sha", "?")
                return f"ok:{new_sha[:8]}"
        except Exception as e:
            return f"error: {e}"

    def _allowed_emails_data(db_):
        rows = db_.query(database.AllowedEmail).all()
        return [{"email": r.email, "added_by": r.added_by, "added_at": r.added_at,
                 "totp_secret": r.totp_secret, "totp_enabled": r.totp_enabled,
                 "backup_codes": r.backup_codes, "first_name": r.first_name,
                 "last_name": r.last_name, "password_hash": r.password_hash,
                 "is_approved": r.is_approved if r.is_approved is not None else 1}
                for r in rows]

    def _login_history_data(db_):
        rows = db_.query(database.LoginHistory).order_by(database.LoginHistory.id.desc()).limit(500).all()
        return [{"email": r.email, "logged_at": r.logged_at, "ip_address": r.ip_address, "location": r.location} for r in rows]

    def _audit_log_data(db_):
        rows = db_.query(database.AuditLog).order_by(database.AuditLog.id.desc()).limit(300).all()
        return [{"user_email": r.user_email, "event_type": r.event_type, "details": r.details,
                 "created_at": r.created_at, "ip_address": r.ip_address, "location": r.location} for r in rows]

    def _stock_events_data(db_):
        rows = db_.query(StockEvent).order_by(StockEvent.id.desc()).limit(1000).all()
        return [{"basket_id": r.basket_id, "stock_code": r.stock_code, "event_type": r.event_type,
                 "description": r.description, "old_value": r.old_value, "new_value": r.new_value,
                 "event_date": r.event_date, "user_email": getattr(r, "user_email", None)} for r in rows]

    results["files"]["allowed_emails_data.json"] = _direct_push("allowed_emails_data.json", _allowed_emails_data)
    results["files"]["login_history.json"]        = _direct_push("login_history.json",        _login_history_data)
    results["files"]["audit_log.json"]             = _direct_push("audit_log.json",             _audit_log_data)
    results["files"]["stock_events.json"]          = _direct_push("stock_events.json",          _stock_events_data)
    return results


# ── Admin audit log ───────────────────────────────────────────────────────────

@app.get("/api/admin/audit-log")
def get_audit_log(request: Request, db: Session = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not is_admin_email(user):
        raise HTTPException(status_code=403, detail="Admin only")

    try:
        events = db.query(StockEvent).order_by(StockEvent.id.desc()).limit(500).all()
        logins = db.query(database.LoginHistory).order_by(database.LoginHistory.id.desc()).limit(300).all()
        uploads = db.query(database.AuditLog).filter(
            database.AuditLog.event_type == "rebalance_upload"
        ).order_by(database.AuditLog.id.desc()).limit(100).all()
        auth_audit = db.query(database.AuditLog).filter(
            database.AuditLog.event_type != "rebalance_upload"
        ).order_by(database.AuditLog.id.desc()).limit(500).all()
    except Exception as e:
        print(f"[admin/audit-log] DB error: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")

    # Merge LoginHistory logins + AuditLog auth events into one sorted list
    auth_events = [
        {
            "id": f"lh-{l.id}", "event_type": "login", "user_email": l.email,
            "details": f"Login from {getattr(l, 'location', None) or 'unknown'}",
            "created_at": l.logged_at,
            "ip_address": l.ip_address, "location": getattr(l, "location", None),
        }
        for l in logins
    ] + [
        {
            "id": f"al-{a.id}", "event_type": a.event_type, "user_email": a.user_email,
            "details": a.details, "created_at": a.created_at,
            "ip_address": getattr(a, "ip_address", None), "location": getattr(a, "location", None),
        }
        for a in auth_audit
    ]
    auth_events.sort(key=lambda x: (x.get("created_at") or ""), reverse=True)

    return {
        "events": [
            {
                "id": e.id, "basket_id": e.basket_id, "stock_code": e.stock_code,
                "event_type": e.event_type, "description": e.description,
                "old_value": e.old_value, "new_value": e.new_value,
                "event_date": e.event_date, "user_email": getattr(e, "user_email", None),
            }
            for e in events
        ],
        "auth_events": auth_events,
        "uploads": [
            {
                "id": u.id, "user_email": u.user_email, "details": u.details, "created_at": u.created_at,
                "ip_address": getattr(u, "ip_address", None), "location": getattr(u, "location", None)
            }
            for u in uploads
        ],
        # kept for backward compat — frontend no longer uses these separately
        "logins": [], "logouts": [],
    }


from routers.alerts_market import router as _alerts_market_router
app.include_router(_alerts_market_router)


from routers.notes_targets_snapshots import router as _notes_targets_snapshots_router
app.include_router(_notes_targets_snapshots_router)


from routers.benchmarks import router as _benchmarks_router
app.include_router(_benchmarks_router)


from routers.rebalance_alerts import router as _rebalance_alerts_router
app.include_router(_rebalance_alerts_router)


# (SPA fallback moved to the end of the file)

from routers.results_calendar import router as _results_calendar_router, _refresh_results_calendar_data
app.include_router(_results_calendar_router)

# ── Actual Portfolio proxy — moved to routers/actual_portfolio_bridge.py ─────

class AllowedEmailBody(BaseModel):
    email: str

@app.get("/api/allowed-emails")
def list_allowed_emails(request: Request, db: Session = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not is_admin_email(user):
        raise HTTPException(status_code=403, detail="Admin only")
    rows = db.query(database.AllowedEmail).order_by(database.AllowedEmail.added_at.desc()).all()
    return [{"email": r.email, "added_by": r.added_by, "added_at": r.added_at} for r in rows]

@app.post("/api/allowed-emails")
def add_allowed_email(body: AllowedEmailBody, request: Request, db: Session = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not is_admin_email(user):
        raise HTTPException(status_code=403, detail="Admin only")
    email = body.email.lower().strip()
    if not email.endswith("@niveshaay.com"):
        raise HTTPException(status_code=400, detail="Only @niveshaay.com emails can be added")
    existing = db.query(database.AllowedEmail).filter_by(email=email).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already approved")
    db.add(database.AllowedEmail(email=email, added_by=user, added_at=datetime.utcnow().isoformat()))
    db.commit()
    _dump_allowed_emails(db)
    from auth import _log_audit as _la
    _la(user, "email_added", f"Added allowed email: {email}")
    return {"status": "added", "email": email}

@app.delete("/api/allowed-emails/{email_addr}")
def remove_allowed_email(email_addr: str, request: Request, db: Session = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not is_admin_email(user):
        raise HTTPException(status_code=403, detail="Admin only")
    email = email_addr.lower().strip()
    if is_admin_email(email):
        raise HTTPException(status_code=400, detail="Cannot remove admin email")
    row = db.query(database.AllowedEmail).filter_by(email=email).first()
    if not row:
        raise HTTPException(status_code=404, detail="Email not found")
    db.delete(row)
    db.commit()
    _dump_allowed_emails(db)
    from auth import _log_audit as _la
    _la(user, "email_removed", f"Removed allowed email: {email}")
    return {"status": "removed", "email": email}


# ── Access Requests (public — no auth required) ───────────────────────────────

@app.post("/api/access-requests")
def submit_access_request(body: dict, db: Session = Depends(get_db)):
    """Anyone can request access. No auth needed — they don't have a token yet."""
    email = (body.get("email") or "").lower().strip()
    if not email or not email.endswith("@niveshaay.com"):
        raise HTTPException(status_code=400, detail="Only @niveshaay.com email addresses are allowed.")

    # Already approved (in AllowedEmail) — can always log in, no need to request
    if db.query(database.AllowedEmail).filter_by(email=email).first():
        return {"status": "already_approved", "message": "Your email is already approved. You can sign in directly."}

    # Previously rejected — allow re-request (clear old rejected entry first)
    rejected = db.query(database.AccessRequest).filter_by(email=email, status="rejected").first()
    if rejected:
        db.delete(rejected)
        db.commit()

    # Already has a pending request?
    existing = db.query(database.AccessRequest).filter_by(email=email, status="pending").first()
    if existing:
        return {"status": "already_requested", "message": "You have already requested access. Please wait for admin approval."}

    db.add(database.AccessRequest(
        email        = email,
        requested_at = datetime.now().isoformat(),
        status       = "pending",
    ))
    db.commit()
    try:
        _dump_access_requests(db)   # persist to JSON → GitHub
    except Exception as e:
        print(f"[access-request] dump failed: {e}")
    return {"status": "submitted", "message": "Access request submitted. The admin will review it shortly."}


@app.get("/api/access-requests")
def list_access_requests(request: Request, db: Session = Depends(get_db)):
    """List pending access requests — admin only."""
    user = getattr(request.state, "user", None)
    if not is_admin_email(user):
        raise HTTPException(status_code=403, detail="Admin only")
    reqs = (db.query(database.AccessRequest)
              .filter_by(status="pending")
              .order_by(database.AccessRequest.requested_at.desc())
              .all())
    return [{"id": r.id, "email": r.email, "requested_at": r.requested_at} for r in reqs]


@app.post("/api/access-requests/{req_id}/approve")
def approve_access_request(req_id: int, request: Request, db: Session = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not is_admin_email(user):
        raise HTTPException(status_code=403, detail="Admin only")
    req = db.query(database.AccessRequest).filter_by(id=req_id).first()
    if not req:
        raise HTTPException(status_code=404)
    req.status       = "approved"
    req.processed_at = datetime.now().isoformat()
    if not db.query(database.AllowedEmail).filter_by(email=req.email).first():
        db.add(database.AllowedEmail(email=req.email, added_by=user, added_at=datetime.now().isoformat()))
    db.commit()
    _dump_access_requests(db)
    _dump_allowed_emails(db)    # persist approved emails → GitHub
    return {"ok": True, "email": req.email}


@app.post("/api/access-requests/{req_id}/reject")
def reject_access_request(req_id: int, request: Request, db: Session = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not is_admin_email(user):
        raise HTTPException(status_code=403, detail="Admin only")
    req = db.query(database.AccessRequest).filter_by(id=req_id).first()
    if not req:
        raise HTTPException(status_code=404)
    req.status       = "rejected"
    req.processed_at = datetime.now().isoformat()
    db.commit()
    _dump_access_requests(db)
    return {"ok": True}


# ── /wp → webportal ASGI sub-app (merged; no separate process) ───────────────
app.mount('/wp', _wp_module.app)


# ── Serve built frontend (production / internal hosting) ─────────────────────
_DIST = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'dist')
if os.path.isdir(_DIST):
    app.mount('/assets', StaticFiles(directory=os.path.join(_DIST, 'assets')), name='assets')

    @app.get('/{full_path:path}', include_in_schema=False)
    async def _spa_fallback(full_path: str = ""):
        """Return index.html for any unknown path so React Router works."""
        return FileResponse(os.path.join(_DIST, 'index.html'))


