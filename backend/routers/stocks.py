"""Basket list + basic stock lookup/search/price-history endpoints."""
import asyncio
import re
import time as _time
from datetime import datetime, timedelta

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import database
import sheet_service
from main import get_db, _io_pool, yf
from routers.actual_portfolio_bridge import _fetch_all_webportal_baskets

router = APIRouter()

@router.get("/api/baskets")
def get_baskets():
    """All baskets with holdings — sourced from webportal (actual portfolio)."""
    data = _fetch_all_webportal_baskets()
    if not data:
        raise HTTPException(status_code=503, detail="Webportal unreachable or no basket data")
    return data

@router.get("/api/stocks/search")
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

@router.get("/api/stocks/info")
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

@router.get("/api/stocks/history")
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
