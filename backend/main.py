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

import database
from database import StockEvent
import sheet_service
try:
    import yfinance as yf
    _YF_AVAILABLE = True
except Exception:
    _YF_AVAILABLE = False

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
from auth import verify_token, ADMIN_EMAIL, router as auth_router, get_location_from_ip
import time as _time
from sqlalchemy import func as _sqf
import requests as _http

app = FastAPI()

# ── JWT middleware — protects all /api/* routes ───────────────────────────────
class JWTMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # JWT check only for /api/* routes (not auth endpoints)
        if path.startswith("/api/") and path != "/api/health":
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
        if path in ("/auth/direct-login", "/auth/logout") and response.status_code == 200:
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

# ── Startup pre-warm ──────────────────────────────────────────────────────────
@app.on_event("startup")
async def _startup_prewarm():
    """Warm basket + historic caches immediately on startup in background.
    Also ensures admin email is always in the allowed_emails table."""
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
                        backup_codes=rec.get("backup_codes")
                    ))
                else:
                    if "totp_secret" in rec:
                        existing.totp_secret = rec["totp_secret"]
                    if "totp_enabled" in rec:
                        existing.totp_enabled = rec["totp_enabled"]
                    if "backup_codes" in rec:
                        existing.backup_codes = rec["backup_codes"]
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
        # Always ensure admin is approved
        if not db_s.query(database.AllowedEmail).filter_by(email=ADMIN_EMAIL).first():
            db_s.add(database.AllowedEmail(email=ADMIN_EMAIL, added_by="system", added_at=datetime.utcnow().isoformat()))
        db_s.commit()
        db_s.close()
        print("[startup] Restored allowed_emails and access_requests from JSON")
    except Exception as e:
        print(f"[startup] Could not restore persisted data: {e}")

    # Background: periodically dump stock events to GitHub every 5 min
    import threading as _t
    def _periodic_dump():
        import time as _time2
        while True:
            _time2.sleep(300)  # 5 minutes
            try:
                _db = database.SessionLocal()
                _dump_stock_events(_db)
                _db.close()
            except Exception:
                pass
    _t.Thread(target=_periodic_dump, daemon=True).start()

    loop = asyncio.get_running_loop()
    async def _warm():
        try:
            # Wait for webportal (port 8001) to be ready before warming caches
            import socket as _sock
            for _ in range(15):
                try:
                    s = _sock.create_connection(("127.0.0.1", 8001), timeout=2)
                    s.close()
                    break
                except Exception:
                    await asyncio.sleep(2)
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

@app.get("/api/health")
def health(): return {"status": "ok"}

@app.get("/api/baskets")
def get_baskets():
    """All baskets with holdings — sourced from webportal (actual portfolio)."""
    data = _fetch_all_webportal_baskets()
    if not data:
        raise HTTPException(status_code=503, detail="Webportal unreachable or no basket data")
    return data

@app.get("/api/stocks/search")
def search_stocks(q: str = "", db: Session = Depends(get_db)):
    if not q or len(q) < 1:
        return []
    stocks = db.query(database.NseStock).filter(
        database.NseStock.code.ilike(f"%{q}%") | database.NseStock.name.ilike(f"%{q}%")
    ).limit(10).all()
    return [{"code": s.code, "name": s.name} for s in stocks]

# ── Per-stock info cache (24 h TTL) ──────────────────────────────────────────
_stock_info_cache: dict = {}
_STOCK_INFO_TTL = 86400  # 24 hours

@app.get("/api/stocks/info")
async def get_stock_info(code: str):
    """
    Return {code, name, sector, industry} for an NSE stock code.
    Fetched from Yahoo Finance — used to auto-fill the Add Stock form.
    Cached 24 h per stock to keep the UI snappy.
    """
    upper_code = re.sub(r'\s+', '', code.strip().upper())
    cached = _stock_info_cache.get(upper_code)
    if cached and (_time.time() - cached['time']) < _STOCK_INFO_TTL:
        return cached['data']

    def _fetch():
        ticker_sym = f"{upper_code}.NS"
        tk = yf.Ticker(ticker_sym)
        info = tk.info or {}
        return {
            "code":     upper_code,
            "name":     info.get("longName") or info.get("shortName") or upper_code,
            "sector":   info.get("sector") or info.get("industry") or "",
            "industry": info.get("industry") or "",
        }

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(_io_pool, _fetch)
    except Exception as e:
        print(f"stock/info error for {upper_code}: {e}")
        result = {"code": upper_code, "name": upper_code, "sector": "", "industry": ""}

    _stock_info_cache[upper_code] = {'time': _time.time(), 'data': result}
    return result

_stock_hist_price_cache: dict = {}  # "{code}:{date}" → {time, price}
_STOCK_HIST_PRICE_TTL = 12 * 3600   # 12 h — historical prices don't change

@app.get("/api/stocks/history")
async def get_stock_history_price(code: str, date: str):
    """
    Fetch the OHLC-average price for a stock on the given date.
    OHLC average = (Open + High + Low + Close) / 4

    If the requested date is a market holiday or weekend (no OHLC data),
    returns the OHLC average for the next available trading day.
    For today/future dates, falls back to the live CMP from the sheet cache.
    """
    cache_key = f"{code.upper()}:{date}"
    cached_hp = _stock_hist_price_cache.get(cache_key)
    if cached_hp and (_time.time() - cached_hp['time']) < _STOCK_HIST_PRICE_TTL:
        return {"price": cached_hp['price']}

    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        today = datetime.now().date()

        # ── Current / future: use live CMP from sheet cache ─────────────────
        if dt.date() >= today:
            price = sheet_service.get_live_cmp(code)
            if price and price > 0:
                return {"price": price}

        # ── Historical: fetch a window around the date ───────────────────────
        # end_date extends 10 days forward so we can step to the next trading
        # day when the requested date is a holiday / weekend.
        ticker     = f"{code}.NS" if not code.endswith(".NS") else code
        start_date = dt - timedelta(days=2)
        end_date   = dt + timedelta(days=10)

        def _fetch():
            return yf.download(ticker, start=start_date.strftime("%Y-%m-%d"),
                               end=end_date.strftime("%Y-%m-%d"), progress=False)

        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(_io_pool, _fetch)

        if data.empty:
            return {"price": 0.0}

        # Strip timezone for naive comparison
        idx    = data.index.tz_localize(None) if data.index.tz is not None else data.index
        cutoff = pd.to_datetime(dt)

        # 1st choice: exact date  2nd choice: next trading day  3rd: most recent past
        exact  = data[idx == cutoff]
        if not exact.empty:
            row = exact.iloc[0]
        else:
            future = data[idx > cutoff]
            if not future.empty:
                row = future.iloc[0]        # next trading day (holiday fallback)
            else:
                past = data[idx < cutoff]
                if past.empty:
                    return {"price": 0.0}
                row = past.iloc[-1]

        # OHLC average
        def _f(v):
            if hasattr(v, 'item'): v = v.item()
            try: return float(v) if pd.notna(v) else None
            except: return None

        vals = [_f(row[c]) for c in ('Open', 'High', 'Low', 'Close')]
        vals = [v for v in vals if v is not None and v > 0]
        if not vals:
            return {"price": 0.0}
        price = round(sum(vals) / len(vals), 2)
        _stock_hist_price_cache[cache_key] = {'time': _time.time(), 'price': price}
        return {"price": price}

    except Exception as e:
        print(f"History price lookup error: {e}")
        return {"price": 0.0}
@app.get("/api/baskets/{basket_id}/historic")
async def get_basket_historic(basket_id: str, db: Session = Depends(get_db)):
    # ── Cache hit: return instantly ───────────────────────────────────────────
    cached = _historic_cache.get(basket_id)
    if cached and (_time.time() - cached['time']) < _HISTORIC_TTL:
        return cached['data']

    loop    = asyncio.get_running_loop()
    baskets = await loop.run_in_executor(_io_pool, sheet_service.get_all_baskets)
    if basket_id not in baskets:
        raise HTTPException(status_code=404, detail="Basket not found")

    basket_name = baskets[basket_id]['name']
    holdings    = baskets[basket_id].get("holdings", [])
    if not holdings:
        return {}

    # ── Inception date: earliest first_seen across active + sold stocks ───────
    try:
        a_min = db.query(_sqf.min(database.BasketHistory.first_seen_date)).filter_by(basket_id=basket_name).scalar()
        s_min = db.query(_sqf.min(database.SoldStock.buy_date)).filter_by(basket_id=basket_name).scalar()
        inception_date = min(d for d in [a_min, s_min] if d) if any([a_min, s_min]) else None
    except Exception:
        inception_date = None

    symbols = [f"{h['code']}.NS" if not h['code'].endswith(".NS") else h['code'] for h in holdings]
    weights = {f"{h['code']}.NS" if not h['code'].endswith(".NS") else h['code']: h['allocation']/100.0 for h in holdings}

    try:
        raw  = await loop.run_in_executor(_io_pool,
                   lambda: yf.download(symbols, period="max", progress=False))
        close_data = raw['Close'] if 'Close' in raw else raw
        if isinstance(close_data, pd.Series):
            close_data = close_data.to_frame(name=symbols[0])
        if close_data.empty:
            return {}

        # Strip timezone for naive datetime comparisons
        if close_data.index.tz is not None:
            close_data.index = close_data.index.tz_localize(None)
        now = close_data.index[-1]

        def calc_ret(target_dt):
            past = close_data.loc[close_data.index <= target_dt]
            if past.empty:
                return None
            past_prices = past.iloc[-1]
            now_prices  = close_data.iloc[-1]
            pf_return = total_weight = 0.0
            for sym in symbols:
                if sym not in close_data.columns:
                    continue
                pp = past_prices.get(sym)
                pn = now_prices.get(sym)
                if pp is None or pn is None:
                    continue
                if hasattr(pp, 'iloc'):
                    pp = pp.iloc[0]
                if hasattr(pn, 'iloc'):
                    pn = pn.iloc[0]
                if pd.isna(pp) or pd.isna(pn) or float(pp) <= 0:
                    continue
                pf_return    += (float(pn) - float(pp)) / float(pp) * weights.get(sym, 0)
                total_weight += weights.get(sym, 0)
            if total_weight == 0:
                return None
            total_w = sum(weights.values())
            return pf_return * (total_w / total_weight) if total_w > 0 else pf_return

        # Build period list — only include periods the basket is actually old enough for.
        # If a basket is 87 days old, showing "1Y" / "3Y" / "5Y" returns meaningless
        # numbers (yfinance just returns the earliest available row for those baskets).
        inception_days = None
        if inception_date:
            try:
                inc_dt = datetime.strptime(inception_date, "%Y-%m-%d")
                inception_days = (datetime.now() - inc_dt).days
            except Exception:
                pass

        all_periods = [("1M", 30), ("6M", 182), ("1Y", 365), ("3Y", 1095), ("5Y", 1825)]
        # Keep a standard period only if the basket is at least that old
        if inception_days is not None:
            periods = [(lbl, d) for lbl, d in all_periods if inception_days >= d]
        else:
            periods = all_periods

        # Always add the Inception period for baskets older than 30 days
        if inception_days is not None and inception_days > 30:
            periods.append(("Inception", inception_days))

        res = {}
        for label, days in periods:
            target_dt = pd.to_datetime(now - timedelta(days=days))
            net_ret   = calc_ret(target_dt)
            if net_ret is not None:
                cagr = ((1 + net_ret) ** (365 / days)) - 1 if days >= 365 and net_ret > -1 else None
                res[label] = {
                    "net":  round(net_ret * 100, 2),
                    "cagr": round(cagr    * 100, 2) if cagr is not None else None,
                }

        if inception_date:
            res["_inception_date"]  = inception_date
            res["_inception_days"]  = inception_days

        # Benchmark returns since inception — fetch max-period data in parallel
        if inception_date and inception_days and inception_days > 30:
            inc_ts = pd.Timestamp(datetime.strptime(inception_date, "%Y-%m-%d"))
            bench_closes = await asyncio.gather(*[
                loop.run_in_executor(_io_pool, _fetch_bench_close_max, sym)
                for sym in _BENCHMARKS.values()
            ])
            bench_inception = {}
            for (bname, _), bclose in zip(_BENCHMARKS.items(), bench_closes):
                if bclose is None or bclose.empty:
                    continue
                if bclose.index.tz is not None:
                    bclose.index = bclose.index.tz_localize(None)
                past = bclose[bclose.index <= inc_ts]
                if past.empty:
                    continue
                p_start = float(past.iloc[-1])
                p_now   = float(bclose.iloc[-1])
                if p_start <= 0:
                    continue
                bnet  = round((p_now - p_start) / p_start * 100, 2)
                bcagr = round(((p_now / p_start) ** (365 / inception_days) - 1) * 100, 2) if inception_days >= 365 else None
                bench_inception[bname] = {"net": bnet, "cagr": bcagr}
            if bench_inception:
                res["_benchmark_inception"] = bench_inception

        _historic_cache[basket_id] = {'time': _time.time(), 'data': res}
        loop.run_in_executor(_io_pool, _save_disk_cache, dict(_historic_cache))
        return res
    except Exception as e:
        print(f"Historic calc error for {basket_id}: {e}")
        return {}

@app.get("/api/simulator/{basket_id}/historic")
async def get_simulator_historic(basket_id: str, db: Session = Depends(get_db)):
    # ── Simulator cache key includes the basket + mod fingerprint ─────────────
    mods_raw = db.query(database.SimulationMod).filter(database.SimulationMod.basket_id == basket_id).all()
    mod_key = f"{_HIST_SIM_VER}:{basket_id}|" + ','.join(sorted(f"{m.stock_code}:{m.allocation}:{m.buy_price}:{m.cmp}" for m in mods_raw))
    cached = _historic_sim_cache.get(mod_key)
    if cached and (_time.time() - cached['time']) < 3600:  # 1-hour cache (index-history changes daily)
        return cached['data']

    baskets_data = _fetch_all_webportal_baskets()
    if not baskets_data or basket_id not in baskets_data:
        raise HTTPException(status_code=404, detail="Basket not found")

    loop = asyncio.get_running_loop()
    actual_holdings = baskets_data[basket_id].get("holdings", [])
    if not actual_holdings:
        return {"actual": {}, "simulated": {}}

    # ── Actual returns: exclusively from webportal index-history ─────────────
    # Same source and formula as the webportal's CalculateReturnPage.
    # basket_id IS already the webportal key (e.g. Mid_Small_Cap), so look up directly.
    actual_hist = {}
    hi = _fetch_index_history()
    if hi:
        idx_entry = hi.get(basket_id) or {}
        idx_data  = idx_entry.get("data", [])
        if idx_data:
            def _find_closest_pt(pts, target):
                exact = next((d for d in pts if d["date"] == target), None)
                if exact: return exact
                after = [d for d in pts if d["date"] >= target]
                if after: return min(after, key=lambda d: d["date"])
                return max(pts, key=lambda d: d["date"])

            latest_pt = max(idx_data, key=lambda d: d["date"])
            lv = float(latest_pt["value"])
            today_str = datetime.now().date()
            for label, days in [("1M", 30), ("6M", 182), ("1Y", 365), ("3Y", 1095), ("5Y", 1825)]:
                base_date = (today_str - timedelta(days=days)).isoformat()
                base_pt = _find_closest_pt(idx_data, base_date)
                bv = float(base_pt["value"])
                if bv <= 0:
                    continue
                # Use same formula as CalculateReturnPage: years = day_diff / 365.25
                years = (datetime.strptime(latest_pt["date"], "%Y-%m-%d") - datetime.strptime(base_pt["date"], "%Y-%m-%d")).days / 365.25
                net_pct  = round((lv - bv) / bv * 100, 2)
                cagr_pct = round((pow(lv / bv, 1 / years) - 1) * 100, 2) if years > 0 else None
                actual_hist[label] = {"net": net_pct, "cagr": cagr_pct}

    # ── No mods → simulated == actual, skip yfinance entirely ───────────────
    if not mods_raw:
        result = {"actual": actual_hist, "simulated": actual_hist}
        _historic_sim_cache[mod_key] = {'time': _time.time(), 'data': result}
        return result

    # ── Simulated returns: apply overrides then compute via yfinance ─────────
    sim_holdings = [h.copy() for h in actual_holdings]
    for mod in mods_raw:
        if mod.override_type == 'modify':
            for h in sim_holdings:
                if h['code'] == mod.stock_code:
                    if mod.allocation is not None: h['allocation'] = mod.allocation
                    if mod.buy_price is not None: h['buy_price'] = mod.buy_price
                    if mod.cmp is not None: h['cmp'] = mod.cmp
                    break
        elif mod.override_type == 'add':
            if mod.stock_code not in [h['code'] for h in sim_holdings]:
                sim_holdings.append({'code': mod.stock_code, 'allocation': mod.allocation or 0})
        elif mod.override_type == 'delete':
            sim_holdings = [h for h in sim_holdings if h['code'] != mod.stock_code]

    def compute_sim_historic(holdings):
        if not holdings:
            return {}
        symbols = [f"{h['code']}.NS" if not h['code'].endswith(".NS") else h['code'] for h in holdings]
        total_alloc = sum(float(h.get('allocation', 0)) for h in holdings)
        if total_alloc > 0:
            weights = {(f"{h['code']}.NS" if not h['code'].endswith(".NS") else h['code']): float(h.get('allocation', 0)) / 100.0 for h in holdings}
        else:
            weights = {sym: 1.0 / len(symbols) for sym in symbols}
        try:
            raw = yf.download(symbols, period="5y", progress=False)
            if raw.empty:
                return {}
            data = raw['Close']
            if isinstance(data, pd.Series):
                data = data.to_frame(name=symbols[0])
            now = data.index[-1]

            def calc_ret(days):
                target = now - timedelta(days=days)
                past = data.loc[data.index <= target]
                if past.empty:
                    return None
                past_prices = past.iloc[-1]
                now_prices  = data.iloc[-1]
                pf_return = 0.0
                weight_used = 0.0
                for sym in symbols:
                    if sym not in data.columns:
                        continue
                    p_past = past_prices.get(sym)
                    p_now  = now_prices.get(sym)
                    if p_past is not None and p_now is not None and pd.notna(p_past) and pd.notna(p_now) and float(p_past) > 0:
                        ret = (float(p_now) - float(p_past)) / float(p_past)
                        pf_return += ret * weights.get(sym, 0)
                        weight_used += weights.get(sym, 0)
                if weight_used == 0:
                    return None
                total_w = sum(weights.values())
                if total_w > 0:
                    pf_return = pf_return * (total_w / weight_used)
                return pf_return

            res = {}
            for label, days in [("1M", 30), ("6M", 182), ("1Y", 365), ("3Y", 1095), ("5Y", 1825)]:
                net_ret = calc_ret(days)
                if net_ret is not None:
                    cagr = ((1 + net_ret) ** (365 / days)) - 1 if days >= 365 and net_ret > -1 else net_ret
                    res[label] = {"net": round(net_ret * 100, 2), "cagr": round(cagr * 100, 2) if days >= 365 else None}
            return res
        except Exception as e:
            print(f"Simulator historic error: {e}")
            return {}

    sim_hist = await loop.run_in_executor(_io_pool, compute_sim_historic, sim_holdings)
    result = {"actual": actual_hist, "simulated": sim_hist}

    # ── Store in simulator cache ──────────────────────────────────────────────
    _historic_sim_cache[mod_key] = {'time': _time.time(), 'data': result}
    return result

class AnalystUpdate(BaseModel):
    analyst_name: str

@app.get("/api/baskets/{basket_id}/analyst")
def get_basket_analyst(basket_id: str, db: Session = Depends(get_db)):
    rec = db.query(database.BasketAnalyst).filter_by(basket_id=basket_id).first()
    return {"analyst_name": rec.analyst_name if rec else ""}

@app.post("/api/baskets/{basket_id}/analyst")
def set_basket_analyst(basket_id: str, body: AnalystUpdate, request: Request, db: Session = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if user != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Admin only")
    rec = db.query(database.BasketAnalyst).filter_by(basket_id=basket_id).first()
    now = datetime.utcnow().isoformat()
    if rec:
        rec.analyst_name = body.analyst_name.strip()
        rec.updated_by   = user
        rec.updated_at   = now
    else:
        db.add(database.BasketAnalyst(
            basket_id=basket_id,
            analyst_name=body.analyst_name.strip(),
            updated_by=user,
            updated_at=now,
        ))
    db.commit()
    return {"status": "ok", "analyst_name": body.analyst_name.strip()}


@app.get("/api/rationales/{stock_code}")
def get_rationale(stock_code: str, db: Session = Depends(get_db)):
    db_obj = db.query(database.Rationale).filter(database.Rationale.stock_code == stock_code.upper()).first()
    if db_obj:
        return {"stock_code": stock_code, "rationale_text": db_obj.rationale_text}
    return {"stock_code": stock_code, "rationale_text": ""}

@app.post("/api/rationales")
def save_rationale(item: RationaleCreate, db: Session = Depends(get_db)):
    db_obj = db.query(database.Rationale).filter(database.Rationale.stock_code == item.stock_code.upper()).first()
    if db_obj:
        db_obj.rationale_text = item.rationale_text
    else:
        db_obj = database.Rationale(stock_code=item.stock_code.upper(), rationale_text=item.rationale_text)
        db.add(db_obj)
    db.commit()
    return {"status": "success"}

class SimulatorCalculateRequest(BaseModel):
    holdings: list
    sips: list

@app.post("/api/simulator/calculate-return")
async def calculate_simulator_return(req: SimulatorCalculateRequest):
    """
    Calculates absolute return based on 10L initial investment + SIPs.
    """
    INITIAL_INVESTMENT = 1000000.0
    holdings = req.holdings
    sips = req.sips

    if not holdings:
        total_invested = INITIAL_INVESTMENT + sum(s.get('amount', 0) for s in sips)
        return {
            "absolute_return": 0.0,
            "total_invested": round(total_invested, 2),
            "current_value": round(total_invested, 2)
        }

    # Parse holdings
    total_shares = {}
    total_initial_alloc = sum(h.get('allocation', 0) for h in holdings)

    # 1. Distribute Initial 10L
    for h in holdings:
        code    = h['code']
        alloc   = h.get('allocation', 0)
        cmp_val = h.get('cmp', 0) or 0
        perf    = h.get('performance', 0) or 0   # percent, e.g. 97.0 for 97%

        # Always derive buy price from performance + CMP so the investment model
        # stays consistent with the weighted-performance basket_return formula.
        # When the user modifies a stock in the simulator the client recomputes
        # performance = (cmp − bp) / bp * 100, so this inversion gives back the
        # exact buy price the user intended. Stale DB buy prices are ignored.
        bp = (cmp_val / (1 + perf / 100)) if (cmp_val > 0 and perf != -100) else 0

        if total_initial_alloc > 0 and bp > 0:
            invested = INITIAL_INVESTMENT * (alloc / total_initial_alloc)
            total_shares[code] = invested / bp
        else:
            total_shares[code] = 0

    total_invested = INITIAL_INVESTMENT

    if not sips:
        # Fast path if no SIPs
        current_value = sum(total_shares.get(h['code'], 0) * h.get('cmp', 0) for h in holdings)
        abs_ret = ((current_value - total_invested) / total_invested) * 100 if total_invested > 0 else 0
        return {"absolute_return": round(abs_ret, 2), "total_invested": total_invested,
                "current_value": round(current_value, 2), "sip_details": []}

    # ── Helper: fetch OHLC-avg price for one stock on a given date ──────────────
    # Reuses _stock_hist_price_cache so repeated calls are free.
    async def _price_on_date(code: str, date_str: str, loop) -> tuple:
        """Returns (price_float, actual_date_str). actual_date_str is the trading day used."""
        cache_key = f"{code.upper()}:{date_str}"
        cached_hp = _stock_hist_price_cache.get(cache_key)
        if cached_hp and (_time.time() - cached_hp['time']) < _STOCK_HIST_PRICE_TTL:
            return cached_hp['price'], date_str

        dt         = datetime.strptime(date_str, "%Y-%m-%d")
        ticker     = f"{code}.NS" if not code.endswith(".NS") else code
        start_date = dt - timedelta(days=2)
        end_date   = dt + timedelta(days=10)  # exclusive in yfinance, covers ~9 days ahead

        def _fetch():
            return yf.download(ticker, start=start_date.strftime("%Y-%m-%d"),
                               end=end_date.strftime("%Y-%m-%d"), progress=False)

        data = await loop.run_in_executor(_io_pool, _fetch)
        if data is None or (hasattr(data, 'empty') and data.empty):
            return 0.0, date_str

        idx    = data.index.tz_localize(None) if data.index.tz is not None else data.index
        cutoff = pd.to_datetime(dt)

        # 1st choice: exact date  2nd choice: next trading day  3rd: most recent past
        exact  = data[idx == cutoff]
        if not exact.empty:
            row         = exact.iloc[0]
            actual_date = exact.index[0].strftime("%Y-%m-%d")
        else:
            future = data[idx > cutoff]
            if not future.empty:
                row         = future.iloc[0]
                actual_date = future.index[0].strftime("%Y-%m-%d")
            else:
                past = data[idx < cutoff]
                if past.empty:
                    return 0.0, date_str
                row         = past.iloc[-1]
                actual_date = past.index[-1].strftime("%Y-%m-%d")

        def _f(v):
            if hasattr(v, 'item'): v = v.item()
            try: return float(v) if pd.notna(v) else None
            except: return None

        vals = [_f(row[c]) for c in ('Open', 'High', 'Low', 'Close') if c in row.index]
        vals = [v for v in vals if v is not None and v > 0]
        if not vals:
            return 0.0, date_str

        price = round(sum(vals) / len(vals), 2)
        _stock_hist_price_cache[cache_key] = {'time': _time.time(), 'price': price}
        return price, actual_date

    # ── 2. Process SIPs chronologically ────────────────────────────────────────
    sips_sorted = sorted(sips, key=lambda x: x['sip_date'])
    loop        = asyncio.get_running_loop()

    # Fetch prices for every (stock, sip_date) pair in parallel
    all_combos  = [(h['code'], sip['sip_date']) for sip in sips_sorted for h in holdings]
    price_tasks = [_price_on_date(code, date_str, loop) for code, date_str in all_combos]
    price_results = await asyncio.gather(*price_tasks, return_exceptions=True)

    # Restructure into {sip_date: {code: (price, actual_date)}}
    prices_map: dict = {}   # sip_date → {code: (price, actual_date)}
    for (code, date_str), result in zip(all_combos, price_results):
        if isinstance(result, Exception) or result is None:
            result = (0.0, date_str)
        price_val, actual_date = result
        prices_map.setdefault(date_str, {})[code] = (float(price_val), actual_date)

    sip_details = []
    for sip in sips_sorted:
        amount    = sip['amount']
        date_str  = sip['sip_date']
        total_invested += amount

        stock_prices = prices_map.get(date_str, {})

        # Stocks that had a valid price on this trading day
        valid_stocks = [(h, stock_prices[h['code']][0]) for h in holdings
                        if stock_prices.get(h['code'], (0,))[0] > 0]
        valid_alloc  = sum(h.get('allocation', 0) for h, _ in valid_stocks)

        # Determine "actual date used" — first stock's actual date (all should be same trading day)
        actual_date_used = date_str
        for h in holdings:
            if stock_prices.get(h['code'], (0,))[0] > 0:
                actual_date_used = stock_prices[h['code']][1]
                break

        sip_stock_breakdown = []
        if valid_alloc > 0:
            for h, price in valid_stocks:
                code           = h['code']
                alloc          = h.get('allocation', 0)
                # Distribute proportionally among stocks that had prices
                amount_invested = round(amount * (alloc / valid_alloc), 2)
                shares_bought   = amount_invested / price
                total_shares[code] = total_shares.get(code, 0) + shares_bought
                sip_stock_breakdown.append({
                    "code":            code,
                    "allocation":      round(alloc, 2),
                    "price":           round(price, 2),
                    "amount_invested": round(amount_invested, 2),
                    "shares":          round(shares_bought, 4),
                })

        sip_details.append({
            "input_date":  date_str,
            "actual_date": actual_date_used,
            "amount":      amount,
            "distributed": valid_alloc > 0,
            "stocks":      sip_stock_breakdown,
        })

    current_value   = sum(total_shares.get(h['code'], 0) * h.get('cmp', 0) for h in holdings)
    uninvested_cash = sum(sip['amount'] for sip in sips_sorted
                          if prices_map.get(sip['sip_date'], {}) == {} or
                             not any(v[0] > 0 for v in prices_map.get(sip['sip_date'], {}).values()))
    current_value  += uninvested_cash

    abs_ret = ((current_value - total_invested) / total_invested) * 100 if total_invested > 0 else 0
    return {
        "absolute_return": round(abs_ret, 2),
        "total_invested":  round(total_invested, 2),
        "current_value":   round(current_value, 2),
        "sip_details":     sip_details,
    }

@app.post("/api/simulator/{basket_id}/reset")
def reset_simulation(basket_id: str, db: Session = Depends(get_db)):
    db.query(database.SimulationMod).filter(database.SimulationMod.basket_id == basket_id).delete()
    db.query(database.SimulationSip).filter(database.SimulationSip.basket_id == basket_id).delete()
    db.commit()
    return {"status": "success"}

@app.delete("/api/sold-stocks/cleanup")
def cleanup_false_sold_stocks(db: Session = Depends(get_db)):
    """Remove sold stock records for stocks that still exist in basket_history (still active holdings)."""
    active_codes = db.query(database.BasketHistory.basket_id, database.BasketHistory.stock_code).all()
    active_set = set((b, s) for b, s in active_codes)
    sold = db.query(database.SoldStock).all()
    removed = 0
    for s in sold:
        if (s.basket_id, s.stock_code) in active_set:
            db.delete(s)
            removed += 1
    db.commit()
    return {"status": "success", "removed": removed}

@app.post("/api/download")
def download_excel(data: list = Body(...)):
    if not data:
        raise HTTPException(status_code=400, detail="No data provided")

    df = pd.DataFrame(data)
    df = df.drop(columns=["tracker"], errors="ignore")

    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Portfolio')

    output.seek(0)
    headers = {'Content-Disposition': 'attachment; filename="portfolio.xlsx"'}
    return StreamingResponse(output, headers=headers, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.post("/api/download/actual-full")
def download_actual_full(data: dict = Body(...)):
    """Export Actual Portfolio Excel: Summary + Holdings + Top5 sheets + Historical Analytics."""
    basket_name = data.get("basket_name", "Portfolio")
    holdings    = data.get("holdings", [])
    stats       = data.get("stats", {})
    historic    = data.get("historic", {})

    # ── Sheet 1: Summary ──────────────────────────────────────────────────────
    summary_rows = [
        {"Metric": "Basket",                        "Value": basket_name},
        {"Metric": "Basket Return (%)",              "Value": round(float(stats.get("basket_return", 0)), 2)},
        {"Metric": "Stock Count",                    "Value": stats.get("stock_count", 0)},
        {"Metric": "Total Market Cap (Cr)",          "Value": round(float(stats.get("total_mcap", 0)), 2)},
        {"Metric": "", "Value": ""},
        {"Metric": "--- Historical Analytics ---",   "Value": ""},
    ]
    for period in ["1M", "6M", "1Y", "3Y", "5Y"]:
        h = historic.get(period) or {}
        if not h:
            continue
        net  = h.get("net")
        cagr = h.get("cagr")
        summary_rows.append({"Metric": f"{period} — Net Return (%)", "Value": net  if net  is not None else "N/A"})
        if cagr is not None:
            summary_rows.append({"Metric": f"{period} — CAGR (%)", "Value": cagr})
        summary_rows.append({"Metric": "", "Value": ""})

    # ── Sheet 2: Holdings ────────────────────────────────────────────────────
    holdings_clean = [{k: v for k, v in h.items() if k != "tracker"} for h in holdings]

    # ── Sheets 3–5: Top 5 Gainers / Losers / Contributors ────────────────────
    def _top5_df(lst):
        rows = []
        for s in lst:
            rows.append({
                "Stock Code":       s.get("code", ""),
                "Name":             s.get("stock_name", s.get("code", "")),
                "Sector":           s.get("sector", ""),
                "Allocation %":     round(float(s.get("allocation", 0)), 2),
                "Buy Price":        round(float(s.get("buy_price", 0)), 2),
                "CMP":              round(float(s.get("cmp", 0)), 2),
                "1M Return %":      round(float(s.get("performance", 0)), 2),
                "Overall Return %": round(float(s.get("overall_performance", 0)), 2),
                "Contribution %":   round(float(s.get("contribution", 0)), 4),
            })
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="Summary", index=False)
        if holdings_clean:
            pd.DataFrame(holdings_clean).to_excel(writer, sheet_name="Holdings", index=False)
        gainers = _top5_df(stats.get("top_gainers", []))
        if not gainers.empty:
            gainers.to_excel(writer, sheet_name="Top Gainers", index=False)
        losers = _top5_df(stats.get("top_losers", []))
        if not losers.empty:
            losers.to_excel(writer, sheet_name="Top Losers", index=False)
        contribs = _top5_df(stats.get("top_contributors", []))
        if not contribs.empty:
            contribs.to_excel(writer, sheet_name="Top Contributors", index=False)

    output.seek(0)
    safe_name = basket_name.replace("/", "-").replace("\\", "-")
    headers = {'Content-Disposition': f'attachment; filename="{safe_name}_Actual.xlsx"'}
    return StreamingResponse(output, headers=headers,
                             media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.post("/api/download/simulator-full")
def download_simulator_full(data: dict = Body(...)):
    """Export full simulator Excel with Summary + Holdings sheets."""
    basket_name   = data.get("basket_name", "Portfolio")
    actual_return = float(data.get("actual_return", 0))
    sim_return    = float(data.get("sim_return", 0))
    alpha         = float(data.get("alpha", 0))
    holdings      = data.get("holdings", [])
    historic      = data.get("historic", {})   # {actual: {...}, simulated: {...}}

    actual_hist = historic.get("actual", {})
    sim_hist    = historic.get("simulated", {})

    # ── Sheet 1: Summary ──────────────────────────────────────────────────────
    summary_rows = [
        {"Metric": "Basket",                  "Value": basket_name},
        {"Metric": "Actual Portfolio Return (%)",   "Value": round(actual_return, 2)},
        {"Metric": "Simulated Portfolio Return (%)", "Value": round(sim_return, 2)},
        {"Metric": "Alpha / Difference (%)",  "Value": round(alpha, 2)},
        {"Metric": "", "Value": ""},
        {"Metric": "--- Historical Comparison ---", "Value": ""},
    ]
    for period in ["1M", "6M", "1Y", "3Y", "5Y"]:
        a = actual_hist.get(period, {}) or {}
        s = sim_hist.get(period, {}) or {}
        if not a and not s:
            continue
        a_net  = a.get("net")
        s_net  = s.get("net")
        delta  = round(s_net - a_net, 2) if a_net is not None and s_net is not None else None
        summary_rows.append({"Metric": f"{period} — Actual Net (%)",   "Value": a_net  if a_net  is not None else "N/A"})
        summary_rows.append({"Metric": f"{period} — Sim Net (%)",      "Value": s_net  if s_net  is not None else "N/A"})
        summary_rows.append({"Metric": f"{period} — Delta (%)",        "Value": delta  if delta  is not None else "N/A"})
        if a.get("cagr") is not None or s.get("cagr") is not None:
            a_cagr = a.get("cagr"); s_cagr = s.get("cagr")
            d_cagr = round(s_cagr - a_cagr, 2) if a_cagr is not None and s_cagr is not None else None
            summary_rows.append({"Metric": f"{period} — Actual CAGR (%)", "Value": a_cagr if a_cagr is not None else "N/A"})
            summary_rows.append({"Metric": f"{period} — Sim CAGR (%)",    "Value": s_cagr if s_cagr is not None else "N/A"})
            summary_rows.append({"Metric": f"{period} — CAGR Delta (%)",  "Value": d_cagr if d_cagr is not None else "N/A"})
        summary_rows.append({"Metric": "", "Value": ""})

    # ── Sheet 2: Holdings ────────────────────────────────────────────────────
    holdings_clean = []
    for h in holdings:
        row = {k: v for k, v in h.items() if k not in ("tracker",)}
        holdings_clean.append(row)

    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="Summary", index=False)
        if holdings_clean:
            pd.DataFrame(holdings_clean).to_excel(writer, sheet_name="Holdings", index=False)

    output.seek(0)
    safe_name = basket_name.replace("/", "-").replace("\\", "-")
    headers = {'Content-Disposition': f'attachment; filename="{safe_name}_Simulated.xlsx"'}
    return StreamingResponse(output, headers=headers, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ── Portfolio Management (sheet-free CRUD) ────────────────────────────────────
# These endpoints let you manage holdings directly in the DB so the system
# continues to work after the Google Sheet is removed.

class HoldingUpsert(BaseModel):
    stock_code: str
    allocation: Optional[float] = None
    buy_price: Optional[float] = None
    buy_date: Optional[str] = None      # YYYY-MM-DD
    stock_name: Optional[str] = None
    sector: Optional[str] = None

@app.get("/api/portfolio/{basket_id}/holdings")
def list_portfolio_holdings(basket_id: str, db: Session = Depends(get_db)):
    """List all holdings stored in DB for a basket (sheet-independent view)."""
    # Convert slug back to sheet name
    basket_name = next(
        (s for s in sheet_service.BASKET_SHEETS
         if re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-') == basket_id),
        None
    )
    if not basket_name:
        raise HTTPException(status_code=404, detail="Basket not found")
    rows = db.query(database.BasketHistory).filter_by(basket_id=basket_name).all()
    return [
        {
            "stock_code":    r.stock_code,
            "stock_name":    r.stock_name or r.stock_code,
            "sector":        r.sector or "",
            "allocation":    r.allocation or 0.0,
            "buy_price":     r.buy_price or 0.0,
            "last_cmp":      r.last_cmp or 0.0,
            "buy_date":      r.first_seen_date or "",
            "last_seen":     r.last_seen_date or "",
        }
        for r in rows
    ]

@app.post("/api/portfolio/{basket_id}/holdings")
def upsert_portfolio_holding(basket_id: str, item: HoldingUpsert, request: Request, db: Session = Depends(get_db)):
    """
    Create or update a holding in the DB.
    Use this to manually manage the portfolio after removing the Google Sheet.
    """
    basket_name = next(
        (s for s in sheet_service.BASKET_SHEETS
         if re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-') == basket_id),
        None
    )
    if not basket_name:
        raise HTTPException(status_code=404, detail="Basket not found")

    code       = re.sub(r'\s+', '', item.stock_code.upper())
    today_str  = datetime.now().strftime("%Y-%m-%d")
    user_email = getattr(request.state, "user", "unknown")

    row = db.query(database.BasketHistory).filter_by(
        basket_id=basket_name, stock_code=code
    ).first()

    if row:
        if item.allocation is not None and row.allocation is not None:
            if abs(item.allocation - row.allocation) >= 0.01:
                db.add(StockEvent(
                    basket_id   = basket_name,
                    stock_code  = code,
                    event_type  = 'allocation_changed',
                    description = f"Allocation changed from {row.allocation:.2f}% to {item.allocation:.2f}% via dashboard",
                    old_value   = f"{row.allocation:.2f}",
                    new_value   = f"{item.allocation:.2f}",
                    event_date  = today_str,
                    user_email  = user_email,
                ))
        if item.buy_price is not None and row.buy_price is not None:
            if abs(item.buy_price - row.buy_price) >= 0.01:
                db.add(StockEvent(
                    basket_id   = basket_name,
                    stock_code  = code,
                    event_type  = 'price_changed',
                    description = f"Buy price updated from Rs.{row.buy_price:.2f} to Rs.{item.buy_price:.2f} via dashboard",
                    old_value   = f"{row.buy_price:.2f}",
                    new_value   = f"{item.buy_price:.2f}",
                    event_date  = today_str,
                    user_email  = user_email,
                ))
        if item.allocation is not None:
            row.allocation = item.allocation
        if item.buy_price is not None:
            row.buy_price = item.buy_price
        if item.buy_date:
            row.first_seen_date = item.buy_date
        if item.stock_name:
            row.stock_name = item.stock_name
        if item.sector:
            row.sector = item.sector
        row.last_seen_date = today_str
    else:
        cmp_val = sheet_service.get_live_cmp(code)
        row = database.BasketHistory(
            basket_id       = basket_name,
            stock_code      = code,
            last_cmp        = cmp_val,
            last_seen_date  = today_str,
            first_seen_date = item.buy_date or today_str,
            buy_price       = item.buy_price,
            allocation      = item.allocation,
            stock_name      = item.stock_name or code,
            sector          = item.sector or "",
        )
        db.add(row)
        db.add(StockEvent(
            basket_id   = basket_name,
            stock_code  = code,
            event_type  = 'added',
            description = f"Stock added via dashboard" + (f" with {item.allocation:.2f}% allocation" if item.allocation else "") + (f" at Rs.{item.buy_price:.2f}" if item.buy_price else ""),
            old_value   = None,
            new_value   = f"alloc={item.allocation or 0:.2f}%, buy_px={item.buy_price or 0:.2f}",
            event_date  = today_str,
            user_email  = user_email,
        ))

    sheet_service._cache.pop(basket_name, None)
    db.commit()
    return {"status": "success", "stock_code": code}

@app.delete("/api/portfolio/{basket_id}/holdings/{stock_code}")
def delete_portfolio_holding(basket_id: str, stock_code: str, db: Session = Depends(get_db)):
    """
    Remove a holding from the DB (equivalent to deleting it from the sheet).
    Also archives it in sold_stocks with sell_date = today.
    """
    basket_name = next(
        (s for s in sheet_service.BASKET_SHEETS
         if re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-') == basket_id),
        None
    )
    if not basket_name:
        raise HTTPException(status_code=404, detail="Basket not found")

    code = re.sub(r'\s+', '', stock_code.upper())
    row = db.query(database.BasketHistory).filter_by(
        basket_id=basket_name, stock_code=code
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Holding not found")

    today_str = datetime.now().strftime("%Y-%m-%d")
    sell_cmp  = sheet_service.get_live_cmp(code) or row.last_cmp or row.buy_price or 0.0
    recorded_buy = row.buy_price if (row.buy_price and row.buy_price > 0) else sell_cmp

    # Archive as sold
    existing_sold = db.query(database.SoldStock).filter_by(
        basket_id=basket_name, stock_code=code
    ).first()
    if not existing_sold:
        db.add(database.SoldStock(
            basket_id  = basket_name,
            stock_code = code,
            buy_price  = recorded_buy,
            sell_price = sell_cmp,
            sell_date  = today_str,
            buy_date   = row.first_seen_date,
            sector     = row.sector or "",
            stock_name = row.stock_name or code,
        ))

    db.delete(row)
    sheet_service._cache.pop(basket_name, None)
    db.commit()
    return {"status": "sold", "stock_code": code}

def _resolve_basket(basket_id: str):
    """Convert a basket slug to its full sheet name, or raise 404."""
    name = next(
        (s for s in sheet_service.BASKET_SHEETS
         if re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-') == basket_id),
        None
    )
    if not name:
        raise HTTPException(status_code=404, detail="Basket not found")
    return name


@app.post("/api/portfolio/{basket_id}/sell/{stock_code}")
def sell_stock(basket_id: str, stock_code: str, request: Request, db: Session = Depends(get_db)):
    """
    Sell a stock via the dashboard:
      1. Fetch live CMP as sell price.
      2. Archive to sold_stocks.
      3. Add to hidden_stocks (reason='sold') so it never reappears from the sheet.
      4. Remove from basket_history (it's sold, not missing).
    """
    basket_name = _resolve_basket(basket_id)
    code        = re.sub(r'\s+', '', stock_code.upper())
    today_str   = datetime.now().strftime("%Y-%m-%d")
    user_email  = getattr(request.state, "user", "unknown")

    row = db.query(database.BasketHistory).filter_by(
        basket_id=basket_name, stock_code=code
    ).first()

    sell_cmp     = sheet_service.get_live_cmp(code) or (row.last_cmp if row else 0.0)
    recorded_buy = (row.buy_price if row and row.buy_price and row.buy_price > 0 else sell_cmp) if row else sell_cmp

    existing = db.query(database.SoldStock).filter_by(
        basket_id=basket_name, stock_code=code
    ).first()
    if not existing:
        db.add(database.SoldStock(
            basket_id  = basket_name,
            stock_code = code,
            buy_price  = recorded_buy,
            sell_price = sell_cmp,
            sell_date  = today_str,
            buy_date   = row.first_seen_date if row else None,
            sector     = (row.sector  or "") if row else "",
            stock_name = (row.stock_name or code) if row else code,
        ))
    db.add(StockEvent(
        basket_id   = basket_name,
        stock_code  = code,
        event_type  = 'sold',
        description = f"Stock sold via dashboard at CMP Rs.{sell_cmp:.2f} (buy Rs.{recorded_buy:.2f})",
        old_value   = f"{recorded_buy:.2f}",
        new_value   = f"{sell_cmp:.2f}",
        event_date  = today_str,
        user_email  = user_email,
    ))

    # Hide from dashboard (permanent — no expiry)
    hidden = db.query(database.HiddenStock).filter_by(
        basket_id=basket_name, stock_code=code
    ).first()
    if not hidden:
        db.add(database.HiddenStock(
            basket_id    = basket_name,
            stock_code   = code,
            hidden_reason= 'sold',
            stock_name   = (row.stock_name or code) if row else code,
            buy_price    = recorded_buy,
            last_cmp     = sell_cmp,
            sector       = (row.sector or "") if row else "",
            allocation   = (row.allocation or 0.0) if row else 0.0,
            hidden_at    = today_str,
            expires_at   = None,
        ))
    else:
        hidden.hidden_reason = 'sold'
        hidden.expires_at    = None

    # Remove from active holdings
    if row:
        db.delete(row)

    sheet_service._cache.pop(basket_name, None)
    db.commit()
    return {"status": "sold", "stock_code": code, "sell_price": round(sell_cmp, 2)}


@app.post("/api/portfolio/{basket_id}/dashboard-delete/{stock_code}")
def dashboard_delete_stock(basket_id: str, stock_code: str, request: Request, db: Session = Depends(get_db)):
    """
    Soft-delete a stock from the dashboard (not a sell):
      - Adds to hidden_stocks with reason='deleted' and expires_at = today+7 days.
      - After 7 days the background thread clears it and it reappears from the sheet.
      - Does NOT create a sold_stocks entry.
    """
    basket_name = _resolve_basket(basket_id)
    code        = re.sub(r'\s+', '', stock_code.upper())
    today_str   = datetime.now().strftime("%Y-%m-%d")
    expires_str = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    user_email  = getattr(request.state, "user", "unknown")

    row = db.query(database.BasketHistory).filter_by(
        basket_id=basket_name, stock_code=code
    ).first()

    hidden = db.query(database.HiddenStock).filter_by(
        basket_id=basket_name, stock_code=code
    ).first()
    if hidden:
        hidden.hidden_reason = 'deleted'
        hidden.expires_at    = expires_str
        hidden.hidden_at     = today_str
    else:
        db.add(database.HiddenStock(
            basket_id    = basket_name,
            stock_code   = code,
            hidden_reason= 'deleted',
            stock_name   = (row.stock_name or code) if row else code,
            buy_price    = (row.buy_price  or 0.0)  if row else 0.0,
            last_cmp     = (row.last_cmp   or 0.0)  if row else 0.0,
            sector       = (row.sector     or "")   if row else "",
            allocation   = (row.allocation or 0.0)  if row else 0.0,
            hidden_at    = today_str,
            expires_at   = expires_str,
        ))

    db.add(StockEvent(
        basket_id   = basket_name,
        stock_code  = code,
        event_type  = 'deleted',
        description = f"Stock hidden from dashboard for 7 days (restores {expires_str})",
        old_value   = None,
        new_value   = expires_str,
        event_date  = today_str,
        user_email  = user_email,
    ))
    sheet_service._cache.pop(basket_name, None)
    db.commit()
    return {"status": "deleted", "stock_code": code, "expires_at": expires_str}


@app.get("/api/portfolio/{basket_id}/events/{stock_code}")
def get_stock_events(basket_id: str, stock_code: str, db: Session = Depends(get_db)):
    """Return full event history for a stock in a basket, newest first."""
    basket_name = _resolve_basket(basket_id)
    code = re.sub(r'\s+', '', stock_code.upper())
    rows = (
        db.query(StockEvent)
        .filter(StockEvent.basket_id == basket_name, StockEvent.stock_code == code)
        .order_by(StockEvent.event_date.desc(), StockEvent.id.desc())
        .all()
    )
    return [
        {
            "event_type":  r.event_type,
            "description": r.description,
            "old_value":   r.old_value,
            "new_value":   r.new_value,
            "event_date":  r.event_date,
        }
        for r in rows
    ]


@app.get("/api/portfolio/{basket_id}/deleted")
def get_deleted_stocks(basket_id: str, db: Session = Depends(get_db)):
    """Return all stocks currently hidden with reason='deleted' for a basket."""
    basket_name = _resolve_basket(basket_id)
    today_str   = datetime.now().strftime("%Y-%m-%d")
    rows = db.query(database.HiddenStock).filter(
        database.HiddenStock.basket_id     == basket_name,
        database.HiddenStock.hidden_reason == 'deleted',
        database.HiddenStock.expires_at    >= today_str,
    ).all()
    return [
        {
            "stock_code": r.stock_code,
            "stock_name": r.stock_name or r.stock_code,
            "sector":     r.sector     or "",
            "buy_price":  r.buy_price  or 0.0,
            "last_cmp":   r.last_cmp   or 0.0,
            "allocation": r.allocation or 0.0,
            "hidden_at":  r.hidden_at,
            "expires_at": r.expires_at,
        }
        for r in rows
    ]


@app.post("/api/portfolio/refresh-metrics")
def refresh_yf_metrics():
    """
    Clear the yfinance metrics cache so the next /api/baskets call
    re-fetches CMP, PE and MCap for all stocks from Yahoo Finance.
    """
    sheet_service._yf_metrics_cache.clear()
    sheet_service._cache.clear()
    return {"status": "cache_cleared"}


# ── Admin audit log ───────────────────────────────────────────────────────────

@app.get("/api/admin/audit-log")
def get_audit_log(request: Request, db: Session = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if user != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail=f"Admin only. Authenticated as: {user!r}")

    try:
        events = db.query(StockEvent).order_by(StockEvent.id.desc()).limit(500).all()
        logins = db.query(database.LoginHistory).order_by(database.LoginHistory.id.desc()).limit(300).all()
        uploads = db.query(database.AuditLog).filter(
            database.AuditLog.event_type == "rebalance_upload"
        ).order_by(database.AuditLog.id.desc()).limit(100).all()
        logouts = db.query(database.AuditLog).filter(
            database.AuditLog.event_type == "logout"
        ).order_by(database.AuditLog.id.desc()).limit(100).all()
    except Exception as e:
        print(f"[admin/audit-log] DB error: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")

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
        "logins": [
            {
                "id": l.id, "email": l.email, "logged_at": l.logged_at, 
                "ip_address": l.ip_address, "location": getattr(l, "location", None)
            }
            for l in logins
        ],
        "uploads": [
            {
                "id": u.id, "user_email": u.user_email, "details": u.details, "created_at": u.created_at,
                "ip_address": getattr(u, "ip_address", None), "location": getattr(u, "location", None)
            }
            for u in uploads
        ],
        "logouts": [
            {
                "id": u.id, "user_email": u.user_email, "created_at": u.created_at,
                "ip_address": getattr(u, "ip_address", None), "location": getattr(u, "location", None)
            }
            for u in logouts
        ],
    }


# ── Target / Stoploss alerts (cross-basket) ───────────────────────────────────

@app.get("/api/alerts")
async def get_alerts(db: Session = Depends(get_db)):
    """
    Return list of stocks across all baskets that have hit their target
    or breached their stoploss based on current CMP.
    """
    loop    = asyncio.get_running_loop()
    baskets = await loop.run_in_executor(_io_pool, sheet_service.get_all_baskets)

    alerts = []
    for basket_id, basket in baskets.items():
        basket_name = basket['name']
        targets_rows = db.query(database.StockTarget).filter_by(basket_id=basket_name).all()
        tmap = {t.stock_code: t for t in targets_rows}

        for h in basket.get('holdings', []):
            code = h.get('code', '')
            cmp  = float(h.get('cmp', 0) or 0)
            t    = tmap.get(code)
            if not t or cmp <= 0:
                continue

            if t.target_price and cmp >= t.target_price:
                alerts.append({
                    'type':         'target_hit',
                    'basket_id':    basket_id,
                    'basket_name':  basket_name,
                    'stock_code':   code,
                    'stock_name':   h.get('stock_name', code),
                    'cmp':          cmp,
                    'target_price': t.target_price,
                    'stoploss':     t.stoploss,
                    'pct':          round((cmp - t.target_price) / t.target_price * 100, 2),
                })
            elif t.stoploss and cmp <= t.stoploss:
                alerts.append({
                    'type':         'stoploss_hit',
                    'basket_id':    basket_id,
                    'basket_name':  basket_name,
                    'stock_code':   code,
                    'stock_name':   h.get('stock_name', code),
                    'cmp':          cmp,
                    'target_price': t.target_price,
                    'stoploss':     t.stoploss,
                    'pct':          round((t.stoploss - cmp) / t.stoploss * 100, 2),
                })

    return alerts


# ── Rebalance Excel upload ────────────────────────────────────────────────────

from fastapi import UploadFile, File as FastAPIFile
import tempfile

REBALANCE_ALLOWED = {"jay.chaudhari@niveshaay.com", "nukul.madaan@niveshaay.com"}

@app.post("/api/upload-rebalance")
async def upload_rebalance(request: Request, file: UploadFile = FastAPIFile(...)):
    """Upload a new rebalance Excel file and import all 7 baskets."""
    import os as _os
    content  = await file.read()
    summary  = {}
    user_email = getattr(request.state, "user", "unknown")

    if user_email not in REBALANCE_ALLOWED:
        raise HTTPException(status_code=403, detail="You do not have permission to upload rebalance files.")

    with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        from rebalance_utils import parse_excel, import_basket
        baskets_data = parse_excel(tmp_path)
        db_up = database.SessionLocal()
        try:
            for basket_name, data in baskets_data.items():
                result = import_basket(db_up, basket_name, data, log=lambda _: None)
                summary[basket_name] = result
            # Audit log entry
            ip = request.client.host if request.client else None
            loc = get_location_from_ip(ip)
            db_up.add(database.AuditLog(
                user_email = user_email,
                event_type = "rebalance_upload",
                details    = json.dumps({"file": file.filename, "baskets": summary}),
                created_at = datetime.now().isoformat(),
                ip_address = ip,
                location   = loc,
            ))
            db_up.commit()
            _dump_audit_log(db_up)   # persist rebalance upload to GitHub immediately
        finally:
            db_up.close()

        sheet_service._cache.clear()
        sheet_service._assembled_cache['time'] = 0
        _historic_cache.clear()

        return {"status": "success", "baskets": summary}
    finally:
        _os.unlink(tmp_path)


@app.get("/api/network-info")
def get_network_info(request: Request):
    """Return the machine's LAN IP so the admin can see the share URL."""
    user = getattr(request.state, "user", None)
    if user != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Admin only")
    import socket as _socket
    try:
        _s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        _s.settimeout(0)
        _s.connect(("10.254.254.254", 1))
        ip = _s.getsockname()[0]
        _s.close()
    except Exception:
        ip = "127.0.0.1"
    return {"ip": ip, "share_url": f"http://{ip}:8000"}


def _fetch_nse_index_csv() -> dict:
    """Fetch NSE index close prices from daily index CSV — works on any cloud IP."""
    import requests as _req
    from datetime import date, timedelta
    TARGET = {"Nifty 50": "NIFTY 50", "Nifty 200": "NIFTY 200"}
    for i in range(1, 7):
        d = (date.today() - timedelta(days=i)).strftime("%d%m%Y")
        url = f"https://archives.nseindia.com/content/indices/ind_close_all_{d}.csv"
        try:
            r = _req.get(url, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                continue
            lines = r.text.strip().split("\n")
            rows = {}
            for line in lines[1:]:
                parts = line.split(",")
                if len(parts) >= 3:
                    name = parts[0].strip().strip('"')
                    try:
                        close = float(parts[1].strip())
                        prev  = float(parts[2].strip())
                        rows[name] = (close, prev)
                    except Exception:
                        pass
            if rows:
                return rows
        except Exception:
            continue
    return {}


@app.get("/api/market")
def get_market_data():
    """Fetch current price + day change for key Indian market indices."""
    result = {}

    # Primary: NSE index CSV archive (no IP blocking)
    idx_csv = _fetch_nse_index_csv()

    INDEX_MAP = {
        "Nifty 50":  ("NIFTY 50",  "^NSEI"),
        "Sensex":    ("SENSEX",    "^BSESN"),
        "Nifty 200": ("NIFTY 200", "^CNX200"),
    }

    for name, (nse_key, yf_symbol) in INDEX_MAP.items():
        if nse_key in idx_csv:
            curr, prev = idx_csv[nse_key]
            pct = round((curr - prev) / prev * 100, 2) if prev > 0 else 0.0
            result[name] = {"price": round(curr, 2), "change_pct": pct}
        else:
            # Fallback: nsepython
            try:
                from nsepython import nse_get_index_quote
                d = nse_get_index_quote(nse_key)
                curr = float(d.get("last", 0) or 0)
                prev = float(d.get("previousClose", curr) or curr)
                if curr > 0:
                    result[name] = {"price": round(curr, 2),
                                    "change_pct": round((curr - prev)/prev*100, 2) if prev else 0.0}
                    continue
            except Exception:
                pass
            result[name] = {"price": 0.0, "change_pct": 0.0}

    return result


# ── Basket Notes ──────────────────────────────────────────────────────────────

@app.get("/api/basket-notes/{basket_id}")
def get_basket_note(basket_id: str, db: Session = Depends(get_db)):
    basket_name = _resolve_basket(basket_id)
    row = db.query(database.BasketNote).filter_by(basket_id=basket_name).first()
    return {"basket_id": basket_id, "note_text": row.note_text if row else "", "updated_at": row.updated_at if row else ""}

class NoteBody(BaseModel):
    note_text: str

@app.post("/api/basket-notes/{basket_id}")
def save_basket_note(basket_id: str, body: NoteBody, db: Session = Depends(get_db)):
    basket_name = _resolve_basket(basket_id)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    row = db.query(database.BasketNote).filter_by(basket_id=basket_name).first()
    if row:
        row.note_text  = body.note_text
        row.updated_at = now_str
    else:
        db.add(database.BasketNote(basket_id=basket_name, note_text=body.note_text, updated_at=now_str))
    db.commit()
    return {"status": "saved", "updated_at": now_str}


# ── Stock Targets & Stoploss ──────────────────────────────────────────────────

class TargetBody(BaseModel):
    stock_code:   str
    target_price: Optional[float] = None
    stoploss:     Optional[float] = None

@app.get("/api/portfolio/{basket_id}/targets")
def get_targets(basket_id: str, db: Session = Depends(get_db)):
    basket_name = _resolve_basket(basket_id)
    rows = db.query(database.StockTarget).filter_by(basket_id=basket_name).all()
    return {r.stock_code: {"target_price": r.target_price, "stoploss": r.stoploss} for r in rows}

@app.post("/api/portfolio/{basket_id}/targets")
def upsert_target(basket_id: str, body: TargetBody, db: Session = Depends(get_db)):
    basket_name = _resolve_basket(basket_id)
    code = re.sub(r'\s+', '', body.stock_code.upper())
    row  = db.query(database.StockTarget).filter_by(basket_id=basket_name, stock_code=code).first()
    if row:
        if body.target_price is not None: row.target_price = body.target_price
        if body.stoploss     is not None: row.stoploss     = body.stoploss
    else:
        db.add(database.StockTarget(basket_id=basket_name, stock_code=code,
                                    target_price=body.target_price, stoploss=body.stoploss))
    db.commit()
    return {"status": "saved"}

@app.delete("/api/portfolio/{basket_id}/targets/{stock_code}")
def delete_target(basket_id: str, stock_code: str, db: Session = Depends(get_db)):
    basket_name = _resolve_basket(basket_id)
    code = re.sub(r'\s+', '', stock_code.upper())
    db.query(database.StockTarget).filter_by(basket_id=basket_name, stock_code=code).delete()
    db.commit()
    return {"status": "deleted"}


# ── Portfolio Snapshots ───────────────────────────────────────────────────────

class SnapshotBody(BaseModel):
    snapshot_name: str
    holdings_json: str   # pre-serialised JSON string from client

@app.get("/api/snapshots/{basket_id}")
def list_snapshots(basket_id: str, db: Session = Depends(get_db)):
    basket_name = _resolve_basket(basket_id)
    rows = (db.query(database.PortfolioSnapshot)
              .filter_by(basket_id=basket_name)
              .order_by(database.PortfolioSnapshot.snapshot_date.desc())
              .all())
    return [{"id": r.id, "name": r.snapshot_name, "date": r.snapshot_date} for r in rows]

@app.post("/api/snapshots/{basket_id}")
def save_snapshot(basket_id: str, body: SnapshotBody, db: Session = Depends(get_db)):
    basket_name = _resolve_basket(basket_id)
    db.add(database.PortfolioSnapshot(
        basket_id=basket_name, snapshot_name=body.snapshot_name,
        snapshot_date=datetime.now().strftime("%Y-%m-%d"), holdings_json=body.holdings_json
    ))
    db.commit()
    return {"status": "saved"}

@app.get("/api/snapshots/{basket_id}/{snapshot_id}")
def get_snapshot(basket_id: str, snapshot_id: int, db: Session = Depends(get_db)):
    basket_name = _resolve_basket(basket_id)
    row = db.query(database.PortfolioSnapshot).filter_by(id=snapshot_id, basket_id=basket_name).first()
    if not row:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return {"id": row.id, "name": row.snapshot_name, "date": row.snapshot_date, "holdings_json": row.holdings_json}

@app.delete("/api/snapshots/{basket_id}/{snapshot_id}")
def delete_snapshot(basket_id: str, snapshot_id: int, db: Session = Depends(get_db)):
    basket_name = _resolve_basket(basket_id)
    db.query(database.PortfolioSnapshot).filter_by(id=snapshot_id, basket_id=basket_name).delete()
    db.commit()
    return {"status": "deleted"}


# ── Benchmark Returns ─────────────────────────────────────────────────────────
# Returns 1M/6M/1Y/3Y/5Y net + CAGR for Nifty50, Nifty200, Nifty MidSmall
# Cached in benchmark_cache table (24 h TTL — one row per symbol+period).
# Each symbol is fetched individually via yf.Ticker to avoid multi-level
# column ambiguity that occurs with yf.download(multiple_symbols).

_BENCHMARKS = {
    "Nifty 50":       "^NSEI",
    "Nifty 200":      "^CNX200",
    "Nifty MidSmall": "^NSMIDCP400",
}
_BENCH_PERIODS = [("1M", 30), ("6M", 182), ("1Y", 365), ("3Y", 1095), ("5Y", 1825)]


def _fetch_benchmark_symbol(symbol: str) -> "pd.Series | None":
    """Fetch 5-year daily Close prices for a single Yahoo Finance symbol."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="5y", auto_adjust=True)
        if hist is None or hist.empty:
            print(f"[benchmark] No data for {symbol}")
            return None
        close = hist["Close"].dropna()
        return close if not close.empty else None
    except Exception as e:
        print(f"[benchmark] Fetch error for {symbol}: {e}")
        return None


def _fetch_bench_close_max(symbol: str) -> "pd.Series | None":
    """Fetch maximum available daily Close prices — used for inception-period benchmark returns."""
    try:
        hist = yf.Ticker(symbol).history(period="max", auto_adjust=True)
        if hist is None or hist.empty:
            return None
        close = hist["Close"].dropna()
        return close if not close.empty else None
    except Exception as e:
        print(f"[benchmark_max] Fetch error for {symbol}: {e}")
        return None


def _calc_benchmark_returns(close: "pd.Series") -> dict:
    """Given a Close price series, compute net & CAGR for all standard periods."""
    now_price = float(close.iloc[-1])
    now_dt    = close.index[-1]
    out = {}
    for pname, days in _BENCH_PERIODS:
        target_dt = now_dt - timedelta(days=days)
        past = close[close.index <= target_dt]
        if past.empty:
            out[pname] = {"net": None, "cagr": None}
            continue
        past_price = float(past.iloc[-1])
        if past_price <= 0:
            out[pname] = {"net": None, "cagr": None}
            continue
        net  = round((now_price - past_price) / past_price * 100, 2)
        cagr = round(((now_price / past_price) ** (365 / days) - 1) * 100, 2) if days >= 365 else None
        out[pname] = {"net": net, "cagr": cagr}
    return out


@app.get("/api/benchmarks")
async def get_benchmarks(db: Session = Depends(get_db)):
    today_str = datetime.now().strftime("%Y-%m-%d")
    result: dict = {}

    # Which symbols need a fresh fetch? (cache miss = any period missing for today)
    symbols_to_fetch: list[tuple[str, str]] = []  # (label, symbol)

    for label, symbol in _BENCHMARKS.items():
        result[label] = {}
        missing = False
        for pname, _ in _BENCH_PERIODS:
            cached = db.query(database.BenchmarkCache).filter_by(
                symbol=symbol, period=pname
            ).first()
            if cached and cached.fetched_at == today_str and cached.net is not None:
                result[label][pname] = {"net": cached.net, "cagr": cached.cagr}
            else:
                missing = True
        if missing:
            symbols_to_fetch.append((label, symbol))

    if not symbols_to_fetch:
        return result

    loop = asyncio.get_running_loop()

    for label, symbol in symbols_to_fetch:
        close = await loop.run_in_executor(_io_pool, _fetch_benchmark_symbol, symbol)
        if close is None:
            # Leave as empty dict (already initialised above)
            continue
        period_data = _calc_benchmark_returns(close)
        result[label] = period_data
        # Upsert cache rows
        for pname, vals in period_data.items():
            cached = db.query(database.BenchmarkCache).filter_by(symbol=symbol, period=pname).first()
            if cached:
                cached.net = vals["net"]; cached.cagr = vals["cagr"]; cached.fetched_at = today_str
            else:
                db.add(database.BenchmarkCache(
                    symbol=symbol, period=pname,
                    net=vals["net"], cagr=vals["cagr"], fetched_at=today_str
                ))

    db.commit()
    return result


# ── Multi-Basket Comparison ───────────────────────────────────────────────────

_BASKET_INCEPTION_MAP = {
    "mid & small cap":   "2019-09-19",
    "mid and small cap": "2019-09-19",
    "green energy":      "2021-03-23",
    "make in india":     "2021-08-16",
    "trends trilogy":    "2022-02-07",
    "trends triology":   "2022-02-07",
    "consumer trends":   "2023-11-13",
    "ipo basket":        "2025-02-13",
    "techstack":         "2026-01-27",
}
_BASKET_EXCLUDE_NAMES = {"ipo recommendations", "ipo recommendation"}

@app.get("/api/baskets/comparison")
def get_basket_comparison():
    """
    Returns all baskets with historic returns from webportal index-history.
    Only shows returns for periods after the basket's inception date.
    """
    baskets = _fetch_all_webportal_baskets()
    hi      = _fetch_index_history()
    today   = datetime.now().date()

    def _get_inception(basket_name: str):
        key = basket_name.lower().replace("nia ", "").strip()
        return _BASKET_INCEPTION_MAP.get(key)

    def _period_returns(basket_key: str, basket_name: str) -> dict:
        if not hi or basket_key not in hi:
            return {}
        idx_data = hi[basket_key].get("data", [])
        if not idx_data:
            return {}
        latest_pt = max(idx_data, key=lambda d: d["date"])
        lv = float(latest_pt["value"])
        inception_str = _get_inception(basket_name)
        result = {}
        for label, days in [("1M", 30), ("3M", 91), ("6M", 182), ("1Y", 365), ("3Y", 1095), ("5Y", 1825)]:
            period_start = (today - timedelta(days=days)).isoformat()
            # Skip if basket wasn't launched at the start of this period
            if inception_str and inception_str > period_start:
                continue
            exact = next((d for d in idx_data if d["date"] == period_start), None)
            if exact:
                base_pt = exact
            else:
                after = [d for d in idx_data if d["date"] >= period_start]
                base_pt = min(after, key=lambda d: d["date"]) if after else None
            if not base_pt:
                continue
            bv = float(base_pt["value"])
            if bv <= 0:
                continue
            years = max(
                (datetime.strptime(latest_pt["date"], "%Y-%m-%d") - datetime.strptime(base_pt["date"], "%Y-%m-%d")).days / 365.25,
                1 / 365.25
            )
            net  = round((lv - bv) / bv * 100, 2)
            cagr = round((pow(lv / bv, 1 / years) - 1) * 100, 2) if years >= 0.083 else None
            result[label] = {"net": net, "cagr": cagr}
        # Since-inception return using provided inception date
        if inception_str:
            inc_exact = next((d for d in idx_data if d["date"] == inception_str), None)
            if not inc_exact:
                after_inc = [d for d in idx_data if d["date"] >= inception_str]
                inc_exact = min(after_inc, key=lambda d: d["date"]) if after_inc else None
            if inc_exact:
                bv_inc = float(inc_exact["value"])
                if bv_inc > 0:
                    years_inc = max(
                        (datetime.strptime(latest_pt["date"], "%Y-%m-%d") - datetime.strptime(inc_exact["date"], "%Y-%m-%d")).days / 365.25,
                        1 / 365.25
                    )
                    result["Overall"] = {
                        "net":  round((lv - bv_inc) / bv_inc * 100, 2),
                        "cagr": round((pow(lv / bv_inc, 1 / years_inc) - 1) * 100, 2) if years_inc > 0 else None,
                    }
        return result

    out = []
    for bid, bdata in baskets.items():
        clean_name = bdata["name"].lower().replace("nia ", "").strip()
        if clean_name in _BASKET_EXCLUDE_NAMES:
            continue
        inception = _get_inception(bdata["name"])
        out.append({
            "id":        bid,
            "name":      bdata["name"],
            "stats":     bdata["stats"],
            "inception": inception,
            "historic":  _period_returns(bid, bdata["name"]),
        })
    return out


# ── Rebalance Impact Alerts ──────────────────────────────────────────────────

_WEBPORTAL_PORTFOLIOS = os.path.join(os.path.dirname(__file__), '..', 'webportal', 'backend', 'portfolios.json')
_WEBPORTAL_REBAL_HIST = os.path.join(os.path.dirname(__file__), '..', 'webportal', 'backend', 'rebalance_history.json')

_BASKET_ALERT_KEYS = {
    'Mid_Small_Cap': 'Mid & Small Cap',
    'Green_Energy':  'Green Energy',
    'IPO_Basket':    'IPO Basket',
    'Trends_Triology': 'Trends Triology',
    'Techstack':     'Techstack',
    'Make_in_India': 'Make in India',
    'Consumer_Trends': 'Consumer Trends',
}

def _parse_rebal_date(ds: str):
    """Parse 'DD Mon YYYY' → date object, or return None."""
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(ds.strip(), fmt).date()
        except Exception:
            pass
    return None

def _build_rebalance_alert(basket_id: str, rebal_date: str, portfolios: dict, rh: dict) -> dict | None:
    """Build a single rebalance alert dict for basket_id on rebal_date."""
    sold_key = f"{basket_id}_sold"
    sold_all = portfolios.get(sold_key, [])
    hist_all = rh.get(basket_id, [])

    # ── Exits & partial sells on this date ───────────────────────────────────
    sold_on_date = [s for s in sold_all if s.get('date', '').strip() == rebal_date]
    wholly_sold  = [s for s in sold_on_date if s.get('action') == 'Wholly Sold']
    partly_sold  = [s for s in sold_on_date if s.get('action') == 'Partially Sold']

    # ── Rebalance history: stocks entering on this date (additions/increases) ─
    added_on_date = [e for e in hist_all if e.get('date', '').strip() == rebal_date]

    # Find previous rebalance date to compute weight diffs
    dates_before = sorted(
        set(e['date'] for e in hist_all if _parse_rebal_date(e['date']) and
            _parse_rebal_date(e['date']) < _parse_rebal_date(rebal_date)),
        key=lambda d: _parse_rebal_date(d),
        reverse=True,
    )
    prev_date = dates_before[0] if dates_before else None
    prev_map = {e['nseCode']: e for e in hist_all if e.get('date') == prev_date} if prev_date else {}
    curr_map = {e['nseCode']: e for e in added_on_date}

    new_additions   = []
    weight_increased = []
    for nse, entry in curr_map.items():
        if nse not in prev_map:
            new_additions.append({'nseCode': nse, 'securityName': entry.get('securityName',''), 'weight': entry.get('weight', 0)})
        else:
            old_w = prev_map[nse].get('weight', 0)
            new_w = entry.get('weight', 0)
            if new_w > old_w + 0.01:
                weight_increased.append({'nseCode': nse, 'securityName': entry.get('securityName',''), 'oldWeight': old_w, 'newWeight': new_w})

    # Skip if nothing changed
    if not wholly_sold and not partly_sold and not new_additions and not weight_increased:
        return None

    def _pnl(s):
        bp = s.get('buyPrice') or 0
        sp = s.get('sellPrice') or 0
        pct = round((sp - bp) / bp * 100, 2) if bp > 0 else None
        return {'nseCode': s.get('nseCode'), 'securityName': s.get('securityName',''),
                'weight': s.get('weightSold', 0), 'buyPrice': bp, 'sellPrice': sp,
                'returnPct': pct, 'gain': sp > bp if bp > 0 else None}

    return {
        'basketId':        basket_id,
        'basketLabel':     _BASKET_ALERT_KEYS.get(basket_id, basket_id),
        'rebalanceDate':   rebal_date,
        'fullExits':       [_pnl(s) for s in wholly_sold],
        'partialSells':    [_pnl(s) for s in partly_sold],
        'newAdditions':    new_additions,
        'weightIncreased': weight_increased,
    }


@app.get("/api/rebalance-alerts")
def get_rebalance_alerts(request: Request, db: Session = Depends(get_db)):
    """Return rebalance events the current user has not yet acknowledged."""
    current_user = getattr(request.state, "user", None)
    if not current_user:
        return []
    try:
        with open(_WEBPORTAL_PORTFOLIOS, encoding='utf-8') as f:
            portfolios = json.load(f)
        with open(_WEBPORTAL_REBAL_HIST, encoding='utf-8') as f:
            rh = json.load(f)
    except Exception:
        return []

    # Get dates the user has already seen
    seen = set(
        (r.basket_id, r.rebalance_date)
        for r in db.query(database.RebalanceAck).filter_by(user_email=current_user).all()
    )

    alerts = []
    for basket_id in _BASKET_ALERT_KEYS:
        sold_all = portfolios.get(f"{basket_id}_sold", [])
        hist_all = rh.get(basket_id, [])

        # Collect all rebalance dates for this basket
        all_dates = set(s.get('date', '').strip() for s in sold_all) | \
                    set(e.get('date', '').strip() for e in hist_all)

        # Find the single MOST RECENT rebalance date only
        valid_dates = [(d, _parse_rebal_date(d)) for d in all_dates if _parse_rebal_date(d)]
        if not valid_dates:
            continue
        latest_date, _ = max(valid_dates, key=lambda x: x[1])

        # Skip if user has already seen this rebalance
        if (basket_id, latest_date) in seen:
            continue

        alert = _build_rebalance_alert(basket_id, latest_date, portfolios, rh)
        if alert:
            alerts.append(alert)

    return alerts


@app.post("/api/rebalance-alerts/ack")
def ack_rebalance_alerts(request: Request, body: dict, db: Session = Depends(get_db)):
    """Mark rebalance events as seen. Body: [{basketId, rebalanceDate}, ...]"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    items = body.get('items', [])
    now_iso = datetime.now().isoformat()
    for item in items:
        basket_id  = item.get('basketId', '')
        rebal_date = item.get('rebalanceDate', '')
        if not basket_id or not rebal_date:
            continue
        exists = db.query(database.RebalanceAck).filter_by(
            user_email=user, basket_id=basket_id, rebalance_date=rebal_date
        ).first()
        if not exists:
            db.add(database.RebalanceAck(
                user_email=user, basket_id=basket_id,
                rebalance_date=rebal_date, acknowledged_at=now_iso,
            ))
    db.commit()
    return {'ok': True, 'acked': len(items)}


# (SPA fallback moved to the end of the file)

_RESULTS_CACHE_FILE = os.path.join(os.path.dirname(__file__), 'results_calendar_cache.json')
_RESULTS_TTL = 12 * 3600  # 12 hours

def _load_results_cache() -> dict:
    try:
        if os.path.exists(_RESULTS_CACHE_FILE):
            with open(_RESULTS_CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"[cache] Could not load results_calendar_cache.json: {e}")
    return {}

def _save_results_cache(cache: dict):
    try:
        with open(_RESULTS_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f)
    except Exception as e:
        print(f"[cache] Could not save results_calendar_cache.json: {e}")

_results_cache: dict = _load_results_cache()

async def _refresh_results_calendar_data(db: Session) -> list:
    from datetime import date
    now = _time.time()
    today_str = date.today().isoformat()

    # Fetch active hidden stocks to exclude them
    hidden_objs = db.query(database.HiddenStock).all()
    hidden_set = set()
    for hs in hidden_objs:
        if hs.hidden_reason == 'sold':
            hidden_set.add((hs.basket_id, hs.stock_code.strip().upper()))
        elif hs.hidden_reason == 'deleted':
            if hs.expires_at and hs.expires_at >= today_str:
                hidden_set.add((hs.basket_id, hs.stock_code.strip().upper()))

    holdings = db.query(database.BasketHistory).filter(database.BasketHistory.stock_code != None).all()
    if not holdings:
        _results_cache["calendar"] = {
            "time": now,
            "data": []
        }
        _save_results_cache(_results_cache)
        return []

    stocks_map = {}
    for h in holdings:
        code = h.stock_code.strip().upper()
        if not code:
            continue
        # Skip if hidden/deleted/sold
        if (h.basket_id, code) in hidden_set:
            continue
        basket_name = re.sub(r'^NIA\s*', '', h.basket_id).strip()
        if code not in stocks_map:
            stocks_map[code] = {
                "name": h.stock_name or code,
                "baskets": {basket_name}
            }
        else:
            stocks_map[code]["baskets"].add(basket_name)

    # Supplement with stocks from actual portfolio (webportal) — falls back silently
    try:
        for key, basket_obj in _fetch_all_webportal_baskets().items():
            basket_label = basket_obj.get("name", key)
            for h in basket_obj.get("holdings", []):
                code = (h.get("code") or "").strip().upper()
                if not code:
                    continue
                if code not in stocks_map:
                    stocks_map[code] = {
                        "name": h.get("stock_name") or code,
                        "baskets": {basket_label}
                    }
                else:
                    stocks_map[code]["baskets"].add(basket_label)
    except Exception:
        pass

    unique_codes = set(stocks_map.keys())

    upcoming_events = []
    seen_events = set()  # (code, date) dedup

    # ── Source 1: NSE board meetings (nse_events) — real announced dates ──────
    try:
        from nsepython import nse_events
        events_df = await asyncio.get_running_loop().run_in_executor(_io_pool, nse_events)
        if events_df is not None and not events_df.empty:
            results_df = events_df[
                events_df['purpose'].str.contains('result|financial', case=False, na=False)
            ]
            for _, row in results_df.iterrows():
                code = str(row.get('symbol', '')).strip().upper()
                if code not in unique_codes:
                    continue
                raw_date = str(row.get('date', '')).strip()
                try:
                    date_obj = datetime.strptime(raw_date, '%d-%b-%Y')
                except Exception:
                    try:
                        date_obj = datetime.strptime(raw_date, '%d-%m-%Y')
                    except Exception:
                        continue
                date_str = date_obj.strftime('%Y-%m-%d')
                if date_str < today_str:
                    continue
                key = (code, date_str)
                if key in seen_events:
                    continue
                seen_events.add(key)
                info = stocks_map[code]
                upcoming_events.append({
                    "stock_code": code,
                    "stock_name": info["name"],
                    "baskets": sorted(list(info["baskets"])),
                    "date": date_str,
                    "purpose": str(row.get('purpose', 'Financial Results')),
                })
    except Exception as nse_err:
        print(f"[Results Calendar] NSE events fetch error: {nse_err}")

    # ── Source 1b: NSE corporate actions via direct HTTP (if nsepython geo-blocked) ──
    if not upcoming_events:
        try:
            import httpx as _hx, urllib.parse as _up
            from datetime import date as _date, timedelta as _td
            _today = _date.today()
            _to    = (_today + _td(days=90)).strftime("%d-%m-%Y")
            _from  = _today.strftime("%d-%m-%Y")
            _hdrs  = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                      "Referer": "https://www.nseindia.com/", "Accept": "*/*"}
            async with _hx.AsyncClient(headers=_hdrs, timeout=10, follow_redirects=True) as _c:
                await _c.get("https://www.nseindia.com/")
                _r = await _c.get(
                    f"https://www.nseindia.com/api/corporates-corporateActions"
                    f"?index=equities&from_date={_from}&to_date={_to}&type=boardMeeting"
                )
            if _r.status_code == 200:
                for item in _r.json():
                    code = str(item.get("symbol","")).strip().upper()
                    if code not in unique_codes:
                        continue
                    subj = str(item.get("subject","")).lower()
                    if "result" not in subj and "quarterly" not in subj and "financial" not in subj:
                        continue
                    raw = str(item.get("exDate","") or item.get("bm_date","")).strip()
                    try:
                        ds = datetime.strptime(raw, "%d-%b-%Y").strftime("%Y-%m-%d")
                    except Exception:
                        continue
                    if ds < today_str or (code, ds) in seen_events:
                        continue
                    seen_events.add((code, ds))
                    upcoming_events.append({"stock_code": code, "stock_name": stocks_map[code]["name"],
                                            "baskets": sorted(list(stocks_map[code]["baskets"])),
                                            "date": ds, "purpose": "Financial Results"})
        except Exception as _e:
            print(f"[Results Calendar] NSE direct fetch error: {_e}")

    # ── Source 2: yfinance calendar — covers large caps NSE may not list yet ──
    remaining_codes = [c for c in unique_codes if not any(e['stock_code'] == c for e in upcoming_events)]

    def _fetch_earnings_yf(code):
        try:
            tk = yf.Ticker(f"{code}.NS" if not code.endswith('.NS') else code)
            cal = tk.calendar
            if cal and 'Earnings Date' in cal:
                dates = cal['Earnings Date']
                if isinstance(dates, list) and dates:
                    iso_dates = []
                    for d in dates:
                        if hasattr(d, 'isoformat'):
                            iso_dates.append(d.isoformat()[:10])
                        elif hasattr(d, 'strftime'):
                            iso_dates.append(d.strftime('%Y-%m-%d'))
                        else:
                            iso_dates.append(str(d)[:10])
                    return code, iso_dates
        except Exception:
            pass
        return code, None

    loop = asyncio.get_running_loop()
    yf_tasks = [loop.run_in_executor(_io_pool, _fetch_earnings_yf, code) for code in remaining_codes]
    yf_results = await asyncio.gather(*yf_tasks)

    for code, dates in yf_results:
        if not dates:
            continue
        info = stocks_map[code]
        for date_str in dates:
            if date_str < today_str:
                continue
            key = (code, date_str)
            if key in seen_events:
                continue
            seen_events.add(key)
            upcoming_events.append({
                "stock_code": code,
                "stock_name": info["name"],
                "baskets": sorted(list(info["baskets"])),
                "date": date_str,
                "purpose": "Financial Results",
            })

    upcoming_events.sort(key=lambda x: x["date"])

    _results_cache["calendar"] = {
        "time": now,
        "data": upcoming_events
    }
    _save_results_cache(_results_cache)

    return upcoming_events

@app.get("/api/portfolio/results-calendar")
async def get_results_calendar(db: Session = Depends(get_db)):
    from datetime import date
    now = _time.time()
    cached = _results_cache.get("calendar")
    today_str = date.today().isoformat()

    if cached and (now - cached['time']) < _RESULTS_TTL:
        holdings = db.query(database.BasketHistory).filter(database.BasketHistory.stock_code != None).all()
        hidden_objs = db.query(database.HiddenStock).all()
        
        # Build active set and hidden set
        active_set = set()
        hidden_set = set()
        for hs in hidden_objs:
            if hs.hidden_reason == 'sold':
                hidden_set.add((hs.basket_id, hs.stock_code.strip().upper()))
            elif hs.hidden_reason == 'deleted':
                if hs.expires_at and hs.expires_at >= today_str:
                    hidden_set.add((hs.basket_id, hs.stock_code.strip().upper()))

        for h in holdings:
            code = h.stock_code.strip().upper()
            if not code:
                continue
            basket_name = re.sub(r'^NIA\s*', '', h.basket_id).strip()
            if (h.basket_id, code) not in hidden_set:
                active_set.add((basket_name, code))

        upcoming = []
        for e in cached['data']:
            if e['date'] < today_str:
                continue
            active_baskets = []
            for b in e['baskets']:
                if (b, e['stock_code']) in active_set:
                    active_baskets.append(b)
            if active_baskets:
                event_copy = e.copy()
                event_copy['baskets'] = active_baskets
                upcoming.append(event_copy)
                
        return upcoming

    return await _refresh_results_calendar_data(db)

@app.get("/api/simulator/{basket_id}")
def get_simulation_mods(basket_id: str, db: Session = Depends(get_db)):
    mods = db.query(database.SimulationMod).filter(database.SimulationMod.basket_id == basket_id).all()
    return mods

@app.post("/api/simulator/{basket_id}")
def override_simulation(basket_id: str, item: SimulationModCreate, db: Session = Depends(get_db)):
    db_obj = db.query(database.SimulationMod).filter(
        database.SimulationMod.basket_id == basket_id,
        database.SimulationMod.stock_code == item.stock_code.upper()
    ).first()
    
    if item.override_type == "remove":
        if db_obj:
            db.delete(db_obj)
    else:
        if db_obj:
            db_obj.override_type = item.override_type
            if item.formula is not None: db_obj.formula = item.formula
            if item.allocation is not None: db_obj.allocation = item.allocation
            if item.buy_price is not None: db_obj.buy_price = item.buy_price
            if item.cmp is not None: db_obj.cmp = item.cmp
        else:
            db_obj = database.SimulationMod(
                basket_id=basket_id,
                stock_code=item.stock_code.upper(),
                override_type=item.override_type,
                formula=item.formula,
                allocation=item.allocation,
                buy_price=item.buy_price,
                cmp=item.cmp
            )
            db.add(db_obj)
            
    db.commit()
    return {"status": "success"}

@app.get("/api/simulator/{basket_id}/sips")
def get_simulation_sips(basket_id: str, db: Session = Depends(get_db)):
    sips = db.query(database.SimulationSip).filter(database.SimulationSip.basket_id == basket_id).order_by(database.SimulationSip.sip_date.asc()).all()
    return [{"id": s.id, "sip_date": s.sip_date, "amount": s.amount} for s in sips]

@app.post("/api/simulator/{basket_id}/sips")
def add_simulation_sip(basket_id: str, item: SimulationSipCreate, db: Session = Depends(get_db)):
    db_obj = database.SimulationSip(
        basket_id=basket_id,
        sip_date=item.sip_date,
        amount=item.amount
    )
    db.add(db_obj)
    db.commit()
    return {"status": "success", "id": db_obj.id}

@app.delete("/api/simulator/{basket_id}/sips/{sip_id}")
def remove_simulation_sip(basket_id: str, sip_id: int, db: Session = Depends(get_db)):
    db.query(database.SimulationSip).filter(
        database.SimulationSip.basket_id == basket_id,
        database.SimulationSip.id == sip_id
    ).delete()
    db.commit()
    return {"status": "success"}


# ── Actual Portfolio proxy (webportal on port 8001) ─────────────────────────

_WEBPORTAL = "http://127.0.0.1:8001"
_wp_all_cache: dict = {"data": None, "ts": 0.0}
_WP_ALL_TTL = 60 * 60  # 60 min — cache basket+live data longer on cloud

_wp_index_cache: dict = {"data": None, "ts": 0.0}

_WP_BASKET_LABELS = {
    'Green_Energy':    'Green Energy',
    'Mid_Small_Cap':   'Mid & Small Cap',
    'Consumer_Trends': 'Consumer Trends',
    'IPO_Basket':      'IPO Basket',
    'Trends_Triology': 'Trends Triology',
    'Techstack':       'Techstack',
    'Make_in_India':   'Make in India',
}

def _fetch_index_history() -> dict:
    """Fetch webportal historical index data. Cached 5 min."""
    global _wp_index_cache
    now = _time.time()
    if _wp_index_cache["data"] and (now - _wp_index_cache["ts"]) < _WP_ALL_TTL:
        return _wp_index_cache["data"]
    try:
        r = _http.get(f"{_WEBPORTAL}/api/index-history", timeout=8)
        r.raise_for_status()
        _wp_index_cache = {"data": r.json(), "ts": now}
        return _wp_index_cache["data"]
    except Exception:
        return _wp_index_cache["data"] or {}

def _fetch_all_webportal_baskets() -> dict:
    """Internal helper — fetch all webportal baskets with holdings. Cached 5 min."""
    global _wp_all_cache
    now = _time.time()
    if _wp_all_cache["data"] and (now - _wp_all_cache["ts"]) < _WP_ALL_TTL:
        return _wp_all_cache["data"]

    def _get_wb():
        return _http.get(f"{_WEBPORTAL}/api/baskets", timeout=6).json()
    def _get_live():
        try:
            return _http.get(f"{_WEBPORTAL}/api/live", timeout=8).json()
        except Exception:
            return {}

    from concurrent.futures import ThreadPoolExecutor as _BPE2
    with _BPE2(max_workers=2) as p2:
        f_wb   = p2.submit(_get_wb)
        f_live = p2.submit(_get_live)
        try:
            wb = f_wb.result()
        except Exception:
            return _wp_all_cache["data"] or {}
        live_map = f_live.result()

    def _fetch_one_basket(kv):
        key, label = kv
        try:
            return key, label, _http.get(f"{_WEBPORTAL}/api/basket/{key}", timeout=8).json()
        except Exception:
            return key, label, None

    from concurrent.futures import ThreadPoolExecutor as _BPE
    with _BPE(max_workers=min(len(wb), 10)) as pool:
        fetched = list(pool.map(_fetch_one_basket, wb.items()))

    result = {}
    for key, label, bdata in fetched:
        if bdata is None:
            continue
        stocks = bdata.get("stocks") or []   # webportal returns a list
        bpd    = bdata.get("buyPriceDetails") or {}
        holdings = []
        for stock in stocks:
            nse   = (stock.get("nseCode") or stock.get("nse") or "").strip().upper()
            if not nse:
                continue
            alloc = float(stock.get("allocation") or 0) * 100  # webportal stores as 0-1 fraction
            bp    = float((bpd.get(nse) or {}).get("buyPrice") or stock.get("buyPrice") or 0)
            cmp_v = float((live_map.get(nse) or {}).get("cmp") or 0)
            perf  = round(((cmp_v - bp) / bp * 100) if bp > 0 else 0, 2)
            sname = stock.get("securityName") or stock.get("name") or nse
            holdings.append({
                "code": nse, "stock_name": sname,
                "allocation": alloc, "buy_price": bp, "cmp": cmp_v,
                "performance": perf, "sector": "", "theme": label,
            })
        total_alloc = sum(h["allocation"] for h in holdings)
        if total_alloc > 0:
            basket_ret = sum(h["performance"] * h["allocation"] for h in holdings) / total_alloc
        elif holdings:
            basket_ret = sum(h["performance"] for h in holdings) / len(holdings)
        else:
            basket_ret = 0.0
        result[key] = {
            "id": key, "name": label, "holdings": holdings,
            "stats": {"basket_return": round(basket_ret, 2), "stock_count": len(holdings)},
        }

    # Only cache if we got valid CMP data — if all 0, webportal wasn't ready yet
    # so don't cache (next request will retry and get real prices)
    total_holdings = sum(len(v.get("holdings", [])) for v in result.values())
    valid_cmps     = sum(1 for v in result.values()
                        for h in v.get("holdings", []) if h.get("cmp", 0) > 0)
    if total_holdings == 0 or valid_cmps > 0:
        _wp_all_cache = {"data": result, "ts": now}

    return result


@app.get("/api/actual-portfolio-baskets")
def get_actual_portfolio_baskets():
    """Return the list of basket keys available in the webportal."""
    try:
        r = _http.get(f"{_WEBPORTAL}/api/baskets", timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Webportal unreachable: {e}")


@app.get("/api/basket-period-returns")
def get_basket_period_returns(period: str = "1M"):
    """
    Compute basket-level period returns from the webportal's historical index data.
    Source: webportal GET /api/index-history (historical_index.json).
    period: 1W=7d, 1M=30d, 3M=90d, 6M=182d, 1Y=365d
    Returns {basket_key: {name, net, cagr, base_date, latest_date}}
    """
    period_days = {'1W': 7, '1M': 30, '3M': 90, '6M': 182, '1Y': 365}
    days = period_days.get(period.upper(), 30)
    today = datetime.now().date()
    base_date_str = (today - timedelta(days=days)).isoformat()

    hi = _fetch_index_history()
    if not hi:
        raise HTTPException(status_code=503, detail="Webportal index history unavailable")

    def _find_closest(data, target):
        exact = next((d for d in data if d["date"] == target), None)
        if exact:
            return exact
        after = [d for d in data if d["date"] >= target]
        if after:
            return min(after, key=lambda d: d["date"])
        return max(data, key=lambda d: d["date"]) if data else None

    result = {}
    for basket_key, info in hi.items():
        data = info.get("data", [])
        if not data:
            continue
        base_pt   = _find_closest(data, base_date_str)
        latest_pt = max(data, key=lambda d: d["date"])
        if not base_pt or not latest_pt:
            continue
        bv = float(base_pt["value"])
        lv = float(latest_pt["value"])
        if bv <= 0:
            continue
        years = max(
            (datetime.strptime(latest_pt["date"], "%Y-%m-%d") - datetime.strptime(base_pt["date"], "%Y-%m-%d")).days / 365.25,
            1 / 365.25
        )
        net  = round((lv - bv) / bv * 100, 2)
        cagr = round((pow(lv / bv, 1 / years) - 1) * 100, 2) if years >= 0.083 else None
        result[basket_key] = {
            "name":        _WP_BASKET_LABELS.get(basket_key, basket_key),
            "net":         net,
            "cagr":        cagr,
            "base_date":   base_pt["date"],
            "latest_date": latest_pt["date"],
        }
    return result


class AllowedEmailBody(BaseModel):
    email: str

@app.get("/api/allowed-emails")
def list_allowed_emails(request: Request, db: Session = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if user != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Admin only")
    rows = db.query(database.AllowedEmail).order_by(database.AllowedEmail.added_at.desc()).all()
    return [{"email": r.email, "added_by": r.added_by, "added_at": r.added_at} for r in rows]

@app.post("/api/allowed-emails")
def add_allowed_email(body: AllowedEmailBody, request: Request, db: Session = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if user != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Admin only")
    email = body.email.lower().strip()
    if not email.endswith(f"@{ADMIN_EMAIL.split('@')[1]}"):
        raise HTTPException(status_code=400, detail="Only @niveshaay.com emails can be added")
    existing = db.query(database.AllowedEmail).filter_by(email=email).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already approved")
    db.add(database.AllowedEmail(email=email, added_by=user, added_at=datetime.utcnow().isoformat()))
    db.commit()
    _dump_allowed_emails(db)
    return {"status": "added", "email": email}

@app.delete("/api/allowed-emails/{email_addr}")
def remove_allowed_email(email_addr: str, request: Request, db: Session = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if user != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Admin only")
    email = email_addr.lower().strip()
    if email == ADMIN_EMAIL:
        raise HTTPException(status_code=400, detail="Cannot remove admin email")
    row = db.query(database.AllowedEmail).filter_by(email=email).first()
    if not row:
        raise HTTPException(status_code=404, detail="Email not found")
    db.delete(row)
    db.commit()
    _dump_allowed_emails(db)
    return {"status": "removed", "email": email}


# ── Persistent JSON storage for access requests & approved emails ─────────────
# SQLite resets on every Render deploy; JSON files persist via GitHub auto-push.

import threading as _main_threading
import base64 as _main_b64

_BACKEND_DIR        = os.path.dirname(os.path.abspath(__file__))
_ACCESS_REQ_FILE    = os.path.join(_BACKEND_DIR, "access_requests.json")
_ALLOWED_EMAIL_FILE = os.path.join(_BACKEND_DIR, "allowed_emails_data.json")
_LOGIN_HISTORY_FILE  = os.path.join(_BACKEND_DIR, "login_history.json")
_AUDIT_LOG_FILE      = os.path.join(_BACKEND_DIR, "audit_log.json")
_STOCK_EVENTS_FILE   = os.path.join(_BACKEND_DIR, "stock_events.json")

def _main_github_push(rel_path: str, content: str):
    """Push a file to GitHub repo — background thread."""
    token = os.environ.get("GITHUB_TOKEN", "")
    repo  = os.environ.get("GITHUB_REPO", "")
    if not token or not repo:
        return
    try:
        import urllib.request as _ur
        api_url = f"https://api.github.com/repos/{repo}/contents/backend/{os.path.basename(rel_path)}"
        hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"}
        try:
            req = _ur.Request(api_url, headers=hdrs)
            with _ur.urlopen(req, timeout=8) as r:
                sha = json.loads(r.read())["sha"]
        except Exception:
            sha = None
        body_data = json.dumps({"message": f"auto: update {os.path.basename(rel_path)}",
                                "content": _main_b64.b64encode(content.encode()).decode(),
                                **( {"sha": sha} if sha else {})}).encode()
        req2 = _ur.Request(api_url, data=body_data, headers=hdrs, method="PUT")
        _ur.urlopen(req2, timeout=10)
    except Exception as e:
        print(f"[github-push] {os.path.basename(rel_path)}: {e}")

def _save_json_push(filepath: str, data, sync: bool = False):
    content = json.dumps(data, indent=2, ensure_ascii=False)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    if sync:
        _main_github_push(filepath, content)   # blocking — used for critical data
    else:
        _main_threading.Thread(target=_main_github_push, args=(filepath, content), daemon=True).start()

def _load_json_file(filepath: str, default):
    try:
        if os.path.exists(filepath):
            with open(filepath, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

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
    _save_json_push(_ACCESS_REQ_FILE, data, sync=True)

def _dump_allowed_emails(db):
    rows = db.query(database.AllowedEmail).all()
    data = [{
        "email": r.email,
        "added_by": r.added_by,
        "added_at": r.added_at,
        "totp_secret": r.totp_secret,
        "totp_enabled": r.totp_enabled,
        "backup_codes": r.backup_codes
    } for r in rows]
    _save_json_push(_ALLOWED_EMAIL_FILE, data, sync=True)


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
    if user != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Admin only")
    reqs = (db.query(database.AccessRequest)
              .filter_by(status="pending")
              .order_by(database.AccessRequest.requested_at.desc())
              .all())
    return [{"id": r.id, "email": r.email, "requested_at": r.requested_at} for r in reqs]


@app.post("/api/access-requests/{req_id}/approve")
def approve_access_request(req_id: int, request: Request, db: Session = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if user != ADMIN_EMAIL:
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
    if user != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Admin only")
    req = db.query(database.AccessRequest).filter_by(id=req_id).first()
    if not req:
        raise HTTPException(status_code=404)
    req.status       = "rejected"
    req.processed_at = datetime.now().isoformat()
    db.commit()
    _dump_access_requests(db)
    return {"ok": True}


@app.get("/api/actual-portfolio-all")
def get_actual_portfolio_all():
    """All webportal baskets with holdings, shaped like /api/baskets. Cached 5 min."""
    data = _fetch_all_webportal_baskets()
    if not data:
        raise HTTPException(status_code=503, detail="Webportal unreachable or no data")
    return data


@app.get("/api/actual-portfolio-sync/{basket_key}")
def sync_from_actual_portfolio(basket_key: str):
    """
    Fetch basket data from the webportal and return it shaped for the simulator.
    Each holding includes: code, name, allocation, buy_price, cmp.
    """
    try:
        r = _http.get(f"{_WEBPORTAL}/api/basket/{basket_key}", timeout=10)
        r.raise_for_status()
        basket_data = r.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Webportal unreachable: {e}")

    live_map = {}
    try:
        lr = _http.get(f"{_WEBPORTAL}/api/live", timeout=10)
        live_map = lr.json() if lr.ok else {}
    except Exception:
        pass

    stocks = basket_data.get("stocks") or []   # webportal returns a list
    buy_details = basket_data.get("buyPriceDetails") or {}

    holdings = []
    for stock in stocks:
        nse = (stock.get("nseCode") or stock.get("nse") or "").strip().upper()
        if not nse:
            continue
        allocation = float(stock.get("allocation") or 0)
        buy_price  = float((buy_details.get(nse) or {}).get("buyPrice") or stock.get("buyPrice") or 0)
        cmp        = float((live_map.get(nse) or {}).get("cmp") or 0)
        name       = stock.get("securityName") or stock.get("name") or nse
        holdings.append({
            "code": nse,
            "name": name,
            "allocation": allocation,
            "buy_price": buy_price,
            "cmp": cmp,
        })

    return {"holdings": holdings, "basket_key": basket_key, "count": len(holdings)}


# ── /wp/ proxy → webportal backend on port 8001 ──────────────────────────────
# On cloud (Render), only one port is publicly exposed.
# All /wp/* requests are forwarded to the internal webportal on port 8001.
import httpx as _httpx
from fastapi.responses import Response as _Response

@app.api_route("/wp", methods=["GET","POST","PUT","DELETE","PATCH","OPTIONS"],
               include_in_schema=False)
async def proxy_webportal_root(request: Request):
    return await proxy_webportal("", request)

@app.api_route("/wp/{wp_path:path}", methods=["GET","POST","PUT","DELETE","PATCH","OPTIONS"],
               include_in_schema=False)
async def proxy_webportal(wp_path: str, request: Request):
    url = f"http://127.0.0.1:8001/{wp_path}"
    qs  = request.url.query
    if qs:
        url = f"{url}?{qs}"

    # Strip hop-by-hop headers that must not be forwarded
    _SKIP = {"host", "content-length", "transfer-encoding",
             "connection", "keep-alive", "upgrade", "te", "trailers"}
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _SKIP}
    body = await request.body()

    last_err = None
    # Retry up to 3 times (webportal may still be starting)
    for attempt in range(3):
        try:
            async with _httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.request(
                    method  = request.method,
                    url     = url,
                    headers = headers,
                    content = body,
                )
            # Strip hop-by-hop response headers
            resp_headers = {k: v for k, v in resp.headers.items()
                            if k.lower() not in _SKIP}
            return _Response(
                content     = resp.content,
                status_code = resp.status_code,
                headers     = resp_headers,
                media_type  = resp.headers.get("content-type"),
            )
        except (_httpx.ConnectError, _httpx.ConnectTimeout) as e:
            last_err = e
            await asyncio.sleep(2)  # wait for webportal to start
        except Exception as e:
            last_err = e
            break

    return _Response(content=f"Webportal unavailable: {last_err}",
                     status_code=503, media_type="text/plain")


# ── Serve built frontend (production / internal hosting) ─────────────────────
_DIST = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'dist')
if os.path.isdir(_DIST):
    app.mount('/assets', StaticFiles(directory=os.path.join(_DIST, 'assets')), name='assets')

    @app.get('/{full_path:path}', include_in_schema=False)
    async def _spa_fallback(full_path: str = ""):
        """Return index.html for any unknown path so React Router works."""
        return FileResponse(os.path.join(_DIST, 'index.html'))


