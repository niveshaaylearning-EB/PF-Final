"""Upcoming-results calendar: cross-references portfolio holdings against NSE
board-meeting/corporate-action feeds and yfinance earnings calendars.

_refresh_results_calendar_data() is also called by main.py's startup
background thread (daily refresh), so it's imported back from there.
"""
import asyncio
import json
import os
import re
import time as _time
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import database
from main import get_db, _io_pool, yf
from routers.actual_portfolio_bridge import _fetch_all_webportal_baskets

router = APIRouter()

_RESULTS_CACHE_FILE = os.path.join(os.path.dirname(__file__), '..', 'results_calendar_cache.json')
_RESULTS_TTL = 12 * 3600      # 12 hours -- normal cache lifetime for a successful fetch
_RESULTS_RETRY_TTL = 15 * 60  # 15 minutes -- short-lived cache when a source errored, so a
                              # transient NSE rate-limit/block self-heals quickly instead of
                              # leaving an empty result cached for the full 12h window

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
    any_source_failed = False  # tracks transient errors so we don't cache a bad empty result for 12h

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
        any_source_failed = True

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
            if _r.status_code != 200:
                any_source_failed = True
            else:
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
            any_source_failed = True

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
        "data": upcoming_events,
        "reliable": not any_source_failed,
    }
    _save_results_cache(_results_cache)

    return upcoming_events

@router.get("/api/portfolio/results-calendar")
async def get_results_calendar(db: Session = Depends(get_db)):
    from datetime import date
    now = _time.time()
    cached = _results_cache.get("calendar")
    today_str = date.today().isoformat()

    # If the last refresh had a source error (e.g. NSE rate-limiting), don't trust
    # the full 12h TTL -- retry much sooner so a transient failure self-heals
    # instead of leaving a possibly-empty/incomplete result cached all day.
    effective_ttl = _RESULTS_TTL if (cached and cached.get("reliable", True)) else _RESULTS_RETRY_TTL

    if cached and (now - cached['time']) < effective_ttl:
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
