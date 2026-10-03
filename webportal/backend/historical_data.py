"""Historical index values: daily basket/benchmark values used by the
webportal's own historic-return charts, populated manually or via Excel import."""
import io
import os
from datetime import datetime

import openpyxl
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from persistence import (
    _load_historical_index, _save_historical_index, _require_admin,
    _load_portfolios, _load_buy_price_data, BASKET_DISPLAY_NAMES,
)

router = APIRouter()

# Shared secret for the smallcase bookmarklet ingest endpoint below -- NOT a
# replacement for the normal admin JWT, which the bookmarklet has no way to
# attach (it runs on smallcase.com's own origin, with no access to our site's
# localStorage). Deliberately low-stakes: the worst a leaked key allows is
# someone posting fake index values into historical_index.json, which any
# admin can already do and which is trivially fixable -- not an account- or
# data-takeover risk, so a single shared static key (not per-admin) is fine.
_SMALLCASE_INGEST_KEY = os.environ.get("SMALLCASE_INGEST_KEY", "niveshaay-smallcase-ingest-2026")

@router.get("/api/index-history")
async def get_index_history():
    """Serve pre-computed historical index values for all baskets."""
    return _load_historical_index()


@router.get("/api/stock-exposure")
async def get_stock_exposure():
    """Combined weightage of each stock across all baskets -- how concentrated
    a single company's exposure is when every basket's allocation % is summed
    together, plus how many baskets it shows up in at all. IPO_Recommendations
    is a watchlist (every entry has allocation 0), so it naturally contributes
    nothing and doesn't need special-casing."""
    portfolios = _load_portfolios()
    buy_price  = _load_buy_price_data()

    exposure: dict = {}
    for basket, stocks in portfolios.items():
        label = BASKET_DISPLAY_NAMES.get(basket, basket)
        bp_map = buy_price.get(basket, {})
        for stock in stocks:
            alloc = float(stock.get("allocation") or 0) * 100
            if alloc <= 0:
                continue
            code = (stock.get("nseCode") or "").strip().upper()
            if not code:
                continue
            name = (bp_map.get(code) or {}).get("securityName") or code
            e = exposure.setdefault(code, {
                "code": code, "stock_name": name,
                "total_weight": 0.0, "basket_count": 0, "per_basket": {},
            })
            e["total_weight"] += alloc
            e["basket_count"] += 1
            e["per_basket"][label] = round(alloc, 2)

    ranked = sorted(exposure.values(), key=lambda x: -x["total_weight"])
    for e in ranked:
        e["total_weight"] = round(e["total_weight"], 2)
    return ranked


@router.post("/api/daily-values")
async def post_daily_values(body: dict, request: Request):
    """Append (or update) daily basket + benchmark index values in historical_index.json.
    Body: { "date": "YYYY-MM-DD", "entries": [ { "basket": key, "value": float, "benchmark": float }, ... ] }
    If an entry for the given date already exists, it is overwritten."""
    _require_admin(request)
    date_str = (body.get("date") or "").strip()
    entries  = body.get("entries") or []

    if not date_str:
        raise HTTPException(status_code=400, detail="date is required")
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    hi = _load_historical_index()

    saved = []
    for entry in entries:
        basket = entry.get("basket", "").strip()
        value  = entry.get("value")
        bench  = entry.get("benchmark")
        if not basket or value is None or bench is None:
            continue
        if basket not in hi:
            continue
        data = hi[basket]["data"]
        # Remove existing entry for this date (overwrite)
        hi[basket]["data"] = [e for e in data if e["date"] != date_str]
        hi[basket]["data"].append({"date": date_str, "value": round(float(value), 4), "benchmark": round(float(bench), 4)})
        hi[basket]["data"].sort(key=lambda e: e["date"])
        saved.append(basket)

    _save_historical_index(hi)

    return {"ok": True, "date": date_str, "saved": saved}


@router.post("/api/admin/smallcase-login/start")
async def smallcase_login_start(body: dict, request: Request):
    """Admin-only: begin smallcase login with a phone number (triggers their OTP SMS)."""
    _require_admin(request)
    phone = (body.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone is required")
    import smallcase_login
    try:
        return await smallcase_login.start_login(phone, auth_header=request.headers.get("Authorization"))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@router.post("/api/admin/smallcase-login/verify")
async def smallcase_login_verify(body: dict, request: Request):
    """Admin-only: complete smallcase login with the OTP code."""
    _require_admin(request)
    otp = (body.get("otp") or "").strip()
    if not otp:
        raise HTTPException(status_code=400, detail="otp is required")
    import smallcase_login
    try:
        return await smallcase_login.verify_otp(otp, auth_header=request.headers.get("Authorization"))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@router.get("/api/admin/smallcase-login/status")
async def smallcase_login_check(request: Request):
    """Admin-only: is there currently a valid logged-in smallcase session?"""
    _require_admin(request)
    import smallcase_login
    try:
        return {"logged_in": await smallcase_login.login_status(auth_header=request.headers.get("Authorization"))}
    except Exception as e:
        return {"logged_in": False, "error": f"{type(e).__name__}: {e}"}


@router.post("/api/admin/smallcase-login/close-browser")
async def smallcase_login_close(request: Request):
    """Admin-only: release our hold on the browser profile so it can be
    opened directly elsewhere (e.g. a manual login window on the same
    profile) without a lock conflict."""
    _require_admin(request)
    import smallcase_login
    try:
        return await smallcase_login.close_browser(auth_header=request.headers.get("Authorization"))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@router.post("/api/admin/smallcase-fetch-daily")
async def smallcase_fetch_daily(request: Request):
    """Admin-only: using the saved smallcase session, pull the latest daily
    index + benchmark values for every mapped basket and append any new dates
    to historical_index.json."""
    _require_admin(request)
    import smallcase_login
    return await smallcase_login.fetch_daily_values(auth_header=request.headers.get("Authorization"))


@router.post("/api/admin/smallcase-rebalance-report/{basket}")
async def smallcase_rebalance_report(basket: str, request: Request):
    """Admin-only: using the saved smallcase session, fetch the password-
    protected 'portfolio report' PDF for one basket (raw bytes + its issue
    date, base64-encoded for JSON transport) -- this is the proxy target
    smallcase_login.fetch_rebalance_report() calls when it's running in the
    wrong process (see that module's own cwd-proxy docstring); the actual
    parse-and-apply step lives in portfolio_report.py's
    /api/fetch-portfolio-report/{basket}, which calls the same public
    function and gets the real result whichever process ends up doing the
    Playwright work."""
    _require_admin(request)
    import smallcase_login
    return await smallcase_login.fetch_rebalance_report(basket, auth_header=request.headers.get("Authorization"))


@router.get("/api/admin/competitor-list")
async def competitor_list(request: Request):
    """Read-only, open to any logged-in user (per the webportal's "non-admins
    view everything, only admins mutate" permission model): the 8 tracked
    competitor smallcases + their last-fetched snapshot (if any), for the
    Competitor Analysis page. Triggering a new fetch stays admin-only, see
    competitor_fetch_all below."""
    import competitor_login
    from persistence import _load_competitor_data
    cached = _load_competitor_data()
    out = []
    for key, cfg in competitor_login.COMPETITOR_SMALLCASE_MAP.items():
        entry = {"key": key, "label": cfg["label"], "manager": cfg["manager"]}
        entry.update(cached.get(key, {}))
        out.append(entry)
    return {"competitors": out}


@router.get("/api/admin/basket-profile/{basket}")
async def basket_profile(basket: str, request: Request):
    """Admin-only: derived profile for one of OUR OWN baskets -- sector mix
    and market-cap mix (both weight-aggregated from current holdings) and
    the most recent rebalance date -- so the Competitor Analysis page can
    compare these against a competitor's equivalent numbers instead of only
    showing the competitor's side. Sector comes from buy_price_data.json's
    "segment" field (real NSE industry/sector name, populated whenever a
    stock was added via a rebalance or manually edited); market-cap bucket
    comes from rebalance_history.json's "segment" field instead -- same
    field NAME, different MEANING in each file (Smallcap/Midcap/Largecap
    there vs industry name here), a pre-existing inconsistency in how this
    data was recorded, not something introduced here. Read-only, open to any
    logged-in user."""
    if basket not in BASKET_DISPLAY_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown basket: {basket}")

    from persistence import _load_portfolios, _load_buy_price_data, _load_rebalance_history
    from buy_price_gains import _date_to_ts

    stocks = _load_portfolios().get(basket, [])
    bp_data = _load_buy_price_data().get(basket, {})
    rh = _load_rebalance_history().get(basket, [])

    # rebalance_history.json's "segment" field is only a real market-cap
    # bucket (Smallcap/Midcap/Largecap/Multicap) for entries written by the
    # smallcase-report rebalance-apply path -- confirmed live (2026-10-01)
    # that an older bulk-import wrote the literal asset-class string
    # "Equity" into this SAME field for a large batch of entries (e.g. Mid
    # & Small Cap's "30 Jun 2026" rows), which isn't a cap bucket at all. A
    # naive "most recent entry" pick silently lost ~80% of this basket's
    # weight from the market-cap mix because of that junk value, with no
    # indication anything was missing. Only entries with one of the 4 real
    # labels are trusted here; anything else (or no history at all) falls
    # back to a live-market-cap-based estimate below, rather than being
    # dropped without explanation.
    _VALID_CAP_LABELS = {"Largecap", "Midcap", "Smallcap", "Multicap"}
    by_code: dict = {}
    for e in rh:
        if e.get("nseCode"):
            by_code.setdefault(e["nseCode"], []).append(e)
    latest_cap_segment = {}
    for code, entries in by_code.items():
        valid = [e for e in entries if e.get("segment") in _VALID_CAP_LABELS]
        if valid:
            latest_cap_segment[code] = max(valid, key=lambda e: _date_to_ts(e.get("date", ""))).get("segment")

    sector_mix: dict = {}
    cap_mix: dict = {}
    needs_live_cap = []
    for s in stocks:
        code = s.get("nseCode")
        alloc_pct = (s.get("allocation") or 0) * 100
        sector = bp_data.get(code, {}).get("segment")
        if sector:
            sector_mix[sector] = round(sector_mix.get(sector, 0) + alloc_pct, 2)
        cap = latest_cap_segment.get(code)
        if cap:
            cap_mix[cap] = round(cap_mix.get(cap, 0) + alloc_pct, 2)
        elif code:
            needs_live_cap.append((code, alloc_pct))

    if needs_live_cap:
        import asyncio
        import price_engine
        import time as _time
        now = _time.time()
        # 4-way concurrency here (unlike stock_metrics below, which has a
        # frontend loading indicator) made this endpoint sit for ~60s on a
        # basket's FIRST load with ~20-40 uncached stocks, with no loading
        # state anywhere on the page -- confirmed live, it looked
        # indistinguishable from stuck/broken. Bumping to 12 alone barely
        # helped (still 59.5s measured live) because the real cost per stock
        # isn't the semaphore, it's price_engine.fetch_live_single's own
        # Screener->Google->NSE cascade (12-13s timeout EACH, tried in
        # sequence on failure) -- so the fix that actually matters is
        # covering the whole basket in one batch instead of several
        # sequential ones. These are public scraping targets tolerating the
        # occasional one-off basket load fine at this concurrency.
        sem = asyncio.Semaphore(40)

        async def _one(code):
            cached = _stock_metrics_cache.get(code)
            if cached and now - cached[0] < _STOCK_METRICS_TTL:
                return cached[1].get("marketCapCr")
            async with sem:
                try:
                    data = await price_engine.fetch_live_single(code)
                except Exception:
                    data = None
                mc = (data or {}).get("marketCapCr")
                _stock_metrics_cache[code] = (now, {"marketCapCr": mc, "peRatio": (data or {}).get("peRatio")})
                return mc

        mcaps = await asyncio.gather(*(_one(code) for code, _ in needs_live_cap))
        for (code, alloc_pct), mc in zip(needs_live_cap, mcaps):
            if mc is None:
                cap_mix["Unclassified"] = round(cap_mix.get("Unclassified", 0) + alloc_pct, 2)
            else:
                # Approximate Cr-based thresholds (not SEBI's exact rank-based
                # definition, which needs a full ranked universe we don't
                # have) -- close enough to bucket correctly in the vast
                # majority of cases, and clearly labeled as an estimate.
                bucket = "Largecap" if mc >= 20000 else "Midcap" if mc >= 5000 else "Smallcap"
                cap_mix[bucket] = round(cap_mix.get(bucket, 0) + alloc_pct, 2)

    dates = [e.get("date") for e in rh if e.get("date")]
    last_rebalance = max(dates, key=_date_to_ts) if dates else None

    # Latest-rebalance new/increased/decreased/removed counts, for the same
    # "what changed last time" comparison panel the competitor side already
    # has (from its portfolio report). rebalance_history.json records one
    # entry per stock per rebalance date with its weight AT that date
    # (0 = wholly removed, per portfolio_report.py's _apply_parsed_entries);
    # classified against that same stock's immediately-preceding recorded
    # weight (first time it's seen at all = "new").
    rebalance_summary = None
    if last_rebalance:
        by_date: dict = {}
        for e in rh:
            by_date.setdefault(e.get("date", ""), []).append(e)
        prior_weight: dict = {}
        for d in sorted(by_date.keys(), key=_date_to_ts):
            if d == last_rebalance:
                break
            for e in by_date[d]:
                if e.get("nseCode"):
                    prior_weight[e["nseCode"]] = e.get("weight", 0)
        counts = {"new": 0, "increased": 0, "decreased": 0, "removed": 0}
        for e in by_date.get(last_rebalance, []):
            code = e.get("nseCode")
            if not code:
                continue
            new_w = e.get("weight", 0) or 0
            old_w = prior_weight.get(code)
            if new_w == 0:
                counts["removed"] += 1
            elif old_w is None:
                counts["new"] += 1
            elif new_w > old_w:
                counts["increased"] += 1
            elif new_w < old_w:
                counts["decreased"] += 1
        rebalance_summary = {"date": last_rebalance, **counts}

    return {
        "basket": basket, "sectorMix": sector_mix, "capMix": cap_mix,
        "lastRebalance": last_rebalance, "stockCount": len(stocks),
        "rebalanceSummary": rebalance_summary,
    }


@router.get("/api/admin/basket-rebalance-history/{basket}")
async def basket_rebalance_history(basket: str, request: Request):
    """Read-only, open to any logged-in user: full rebalance history for OUR
    OWN basket, one entry per date, each with the exact per-stock new/
    increased/decreased/removed detail (name + old/new weight) -- not just
    the latest date, unlike basket_profile's rebalanceSummary. rebalance_
    history.json already has every date's full per-stock snapshot, so
    (unlike competitor data, which only ever exposes the LATEST rebalance's
    stock-level detail via its portfolio report PDF) we can compute this for
    every historical date."""
    if basket not in BASKET_DISPLAY_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown basket: {basket}")

    from persistence import _load_rebalance_history
    from buy_price_gains import _date_to_ts

    rh = _load_rebalance_history().get(basket, [])
    by_date: dict = {}
    for e in rh:
        by_date.setdefault(e.get("date", ""), []).append(e)
    sorted_dates = sorted(by_date.keys(), key=_date_to_ts)

    history = []
    prior_weight: dict = {}
    prior_name: dict = {}
    for d in sorted_dates:
        changes = []
        counts = {"new": 0, "increased": 0, "decreased": 0, "removed": 0}
        for e in by_date[d]:
            code = e.get("nseCode")
            if not code:
                continue
            name = e.get("securityName") or code
            new_w = e.get("weight", 0) or 0
            old_w = prior_weight.get(code)
            if new_w == 0:
                status = "removed"
            elif old_w is None:
                status = "new"
            elif new_w > old_w:
                status = "increased"
            elif new_w < old_w:
                status = "decreased"
            else:
                status = "unchanged"
            if status != "unchanged":
                counts[status] += 1
                changes.append({
                    "nseCode": code, "name": name,
                    "oldWeight": old_w, "newWeight": new_w, "status": status,
                })
        if changes:
            history.append({"date": d, "counts": counts, "changes": changes})
        for e in by_date[d]:
            if e.get("nseCode"):
                prior_weight[e["nseCode"]] = e.get("weight", 0)
                prior_name[e["nseCode"]] = e.get("securityName")

    history.sort(key=lambda h: _date_to_ts(h["date"]), reverse=True)
    return {"basket": basket, "history": history}


# Small server-side cache for arbitrary (possibly non-basket) NSE codes'
# market cap + P/E -- the Competitor Analysis table needs these for stocks
# that aren't in any of our own baskets, which price_engine's own
# fetch_live_batch() cache doesn't cover (that one's scoped to basket
# holdings only). A 30-minute TTL keeps repeat page loads/competitor
# switches cheap without hitting Screener.in/Google Finance/NSE on every
# request for the same ~40 stocks.
_stock_metrics_cache: dict = {}
_STOCK_METRICS_TTL = 1800


@router.post("/api/admin/stock-metrics")
async def stock_metrics(body: dict, request: Request):
    """Read-only, open to any logged-in user: real Market Cap (Cr) + P/E for
    a batch of NSE codes, used by the Competitor Analysis stock-comparison
    table. Never fabricated -- a code that fails every source in price_
    engine's cascade is just omitted, and the frontend shows that as "N/A"."""
    import asyncio
    import time
    import price_engine

    codes = list({c.strip().upper() for c in (body.get("codes") or []) if c})
    now = time.time()
    result = {}
    to_fetch = []
    for code in codes:
        cached = _stock_metrics_cache.get(code)
        if cached and now - cached[0] < _STOCK_METRICS_TTL:
            result[code] = cached[1]
        else:
            to_fetch.append(code)

    sem = asyncio.Semaphore(40)

    async def _one(code):
        async with sem:
            try:
                data = await price_engine.fetch_live_single(code)
            except Exception:
                data = None
            metrics = {"marketCapCr": (data or {}).get("marketCapCr"), "peRatio": (data or {}).get("peRatio")}
            _stock_metrics_cache[code] = (now, metrics)
            result[code] = metrics

    if to_fetch:
        await asyncio.gather(*(_one(c) for c in to_fetch))

    return {"metrics": result}


@router.post("/api/admin/stock-ohlc-on-date")
async def stock_ohlc_on_date(body: dict, request: Request):
    """Read-only, open to any logged-in user: real OHLC for a batch of
    (nseCode, date) pairs -- used by Stock Timing Insights to show what
    price we vs. a competitor actually bought/sold a matched stock at.
    `date` is YYYY-MM-DD; the backing fetch (price_engine.fetch_ohlc_on_date)
    resolves to the nearest trading day on/after it when that exact date is
    a weekend/holiday, never fabricated -- a pair with no data just comes
    back omitted."""
    import asyncio
    import price_engine

    pairs = body.get("pairs") or []
    seen = {}
    for p in pairs:
        code, date_str = (p.get("nseCode") or "").strip().upper(), (p.get("date") or "").strip()
        if code and date_str:
            seen[(code, date_str)] = True

    sem = asyncio.Semaphore(6)
    result = {}

    async def _one(code, date_str):
        async with sem:
            try:
                data = await price_engine.fetch_ohlc_on_date(code, date_str)
            except Exception:
                data = None
            if data:
                result[f"{code}|{date_str}"] = data

    await asyncio.gather(*(_one(code, date_str) for code, date_str in seen))
    return {"ohlc": result}


@router.post("/api/admin/competitor-login/start")
async def competitor_login_start(body: dict, request: Request):
    """Admin-only: begin login to the SEPARATE competitor smallcase account
    (holds real subscriptions to the 8 tracked competitor smallcases) with a
    phone number -- mirrors smallcase-login/start but against its own
    independent session (competitor_login.py)."""
    _require_admin(request)
    phone = (body.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone is required")
    import competitor_login
    try:
        return await competitor_login.start_login(phone, auth_header=request.headers.get("Authorization"))
    except Exception as e:
        # This is this module's FIRST-EVER production invocation (new
        # feature, brand new browser profile dir) -- unlike every other step
        # here, nothing has proven the underlying Playwright/browser launch
        # actually works in this environment yet. Surface the real error
        # instead of letting it fall through to FastAPI's generic 500 (which
        # the admin UI only shows as an opaque "Request failed (500)").
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@router.post("/api/admin/competitor-login/verify")
async def competitor_login_verify(body: dict, request: Request):
    """Admin-only: complete the competitor-account smallcase login with the OTP code."""
    _require_admin(request)
    otp = (body.get("otp") or "").strip()
    if not otp:
        raise HTTPException(status_code=400, detail="otp is required")
    import competitor_login
    try:
        return await competitor_login.verify_otp(otp, auth_header=request.headers.get("Authorization"))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@router.get("/api/admin/competitor-login/status")
async def competitor_login_check(request: Request):
    """Admin-only: is there currently a valid logged-in session on the competitor account?"""
    _require_admin(request)
    import competitor_login
    try:
        return {"logged_in": await competitor_login.login_status(auth_header=request.headers.get("Authorization"))}
    except Exception as e:
        return {"logged_in": False, "error": f"{type(e).__name__}: {e}"}


@router.post("/api/admin/competitor-login/close-browser")
async def competitor_login_close(request: Request):
    """Admin-only: release our hold on the competitor-account browser profile."""
    _require_admin(request)
    import competitor_login
    try:
        return await competitor_login.close_browser(auth_header=request.headers.get("Authorization"))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@router.post("/api/admin/competitor-fetch-snapshot/{key}")
async def competitor_fetch_snapshot(key: str, request: Request):
    """Admin-only: proxy target for competitor_login.fetch_competitor_snapshot()
    when running in the wrong process (see smallcase_login.py's cwd-proxy
    docstring for why this split exists)."""
    _require_admin(request)
    import competitor_login
    return await competitor_login.fetch_competitor_snapshot(key, auth_header=request.headers.get("Authorization"))


@router.post("/api/admin/competitor-fetch-all")
async def competitor_fetch_all(request: Request):
    """Admin-only: using the saved competitor-account session, scrape every
    tracked competitor smallcase's real /constituents page in one server-
    side run and persist the results -- this is what the Competitor
    Analysis page's "Fetch Competitor Data" button calls. No bookmarklet,
    no per-page manual visiting."""
    _require_admin(request)
    import competitor_login
    import price_engine
    from persistence import _load_competitor_data, _save_competitor_data
    from datetime import datetime, timezone

    outcome = await competitor_login.fetch_all_competitors(auth_header=request.headers.get("Authorization"))
    if not outcome.get("ok"):
        raise HTTPException(status_code=502, detail=outcome.get("error", "Could not fetch competitor data."))

    nse_symbols = price_engine._nse_symbols_cache
    if not nse_symbols:
        nse_symbols = await price_engine._fetch_nse_symbols()

    cached = _load_competitor_data()
    now_str = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    summary = []
    for key, snap in outcome.get("results", {}).items():
        cfg = competitor_login.COMPETITOR_SMALLCASE_MAP[key]
        if not snap.get("ok"):
            summary.append({"key": key, "label": cfg["label"], "ok": False, "error": snap.get("error")})
            continue
        stocks = []
        for s in (snap.get("stocks") or []):
            name = s.get("name", "")
            nse = price_engine._resolve_nse(name, [], nse_symbols)
            stocks.append({"name": name, "weight": s.get("weight"), "nseCode": nse, "sector": s.get("sector"), "capSegment": s.get("capSegment")})

        # Full since-inception rebalance history (from the "Download rebalance
        # timeline" .xlsx, see competitor_login.diff_historical_constituents) --
        # resolve each change's nseCode the same way `stocks` above does, so
        # the frontend's timing-insights nseCode matching works on this too.
        full_history = competitor_login.diff_historical_constituents(snap.get("historicalConstituents") or [])
        for h in full_history:
            for c in h["changes"]:
                c["nseCode"] = price_engine._resolve_nse(c["name"], [], nse_symbols)

        cached[key] = {
            "key": key, "label": cfg["label"], "manager": cfg["manager"],
            "stocks": stocks,
            "cagr": snap.get("cagr"),
            "rebalanceTimeline": snap.get("rebalanceTimeline"),
            "fullRebalanceHistory": full_history,
            "marketCapMix": snap.get("marketCapMix"),
            "performanceSeries": snap.get("performanceSeries"),
            "launchDate": snap.get("launchDate"),
            "latestRebalanceDetail": snap.get("latestRebalanceDetail"),
            "lastFetched": now_str,
        }
        summary.append({"key": key, "label": cfg["label"], "ok": True, "stockCount": len(stocks)})
    _save_competitor_data(cached)
    return {"ok": True, "results": summary}


@router.get("/api/admin/smallcase-bookmarklet")
async def smallcase_bookmarklet(request: Request):
    """Admin-only: generates ONE combined bookmarklet (dashboard-
    bookmarklet-source.js) that does both jobs in a single click -- fetches
    our own 7 baskets' daily values, AND (if the current page is one of the
    8 tracked competitor smallcases) scrapes that page's stocks/rebalance/
    performance sections. One bookmark to drag, not two -- it works from
    whichever smallcase account is logged into the current tab, since both
    steps independently no-op if their data isn't reachable from that
    session. Endpoint name kept as /smallcase-bookmarklet (not renamed to
    /dashboard-bookmarklet) so an already-dragged bookmark's ingest URL
    keeps working after this change -- only the SOURCE file it's generated
    from changed, not this route."""
    _require_admin(request)
    from pathlib import Path
    src_path = Path(__file__).parent.parent / "frontend" / "public" / "dashboard-bookmarklet-source.js"
    src = src_path.read_text(encoding="utf-8")
    # Derive the ingest URL from THIS request's own path rather than a fixed
    # prefix -- local dev hits this directly on :8001 as /api/admin/..., prod
    # hits it through the main app's /wp mount as /wp/api/admin/... , and the
    # bookmarklet must post back to whichever one the admin actually used.
    prefix = request.url.path.rsplit("/", 1)[0]  # .../admin
    ingest_url = str(request.base_url).rstrip("/") + prefix + "/smallcase-ingest"
    src = src.replace("__INGEST_URL__", ingest_url).replace("__INGEST_KEY__", _SMALLCASE_INGEST_KEY)
    import urllib.parse
    href = "javascript:" + urllib.parse.quote(src)
    return {"href": href}


@router.post("/api/admin/smallcase-ingest")
async def smallcase_ingest(request: Request):
    """Receives the combined bookmarklet's payload: {"baskets": {...}} (our
    own 7 baskets' raw performance points, merged into historical_index.json
    exactly as before) and/or {"competitor": {...}} (one competitor
    smallcase's page -- scid/cagr/rebalanceTimeline/stocks scraped from its
    real rendered /constituents page, merged into competitor_data.json).
    Either key may be absent depending on which page the bookmarklet ran on
    (competitors each need their own click -- see dashboard-bookmarklet-
    source.js's docstring for why a one-click-fetches-all-8 design isn't
    possible here).

    Authenticated by a shared key, NOT the normal admin JWT -- the
    bookmarklet runs on smallcase.com's origin and has no access to this
    site's localStorage token. See _SMALLCASE_INGEST_KEY's docstring above
    for why a single shared key is an acceptable tradeoff here."""
    key = request.headers.get("X-Ingest-Key", "")
    if key != _SMALLCASE_INGEST_KEY:
        raise HTTPException(status_code=403, detail="Invalid ingest key.")
    payload = await request.json()
    result = {"ok": True}

    if payload.get("baskets"):
        import smallcase_login
        result["baskets"] = smallcase_login.merge_ingested_payload(payload["baskets"]).get("results", {})

    comp = payload.get("competitor")
    if comp:
        scid = (comp.get("scid") or "").strip()
        import competitor_login
        import price_engine
        from persistence import _load_competitor_data, _save_competitor_data
        from datetime import datetime, timezone

        match = next(((k, cfg) for k, cfg in competitor_login.COMPETITOR_SMALLCASE_MAP.items() if cfg["scid"] == scid), None)
        if not match:
            raise HTTPException(status_code=400, detail=f"Unrecognized competitor scid: {scid}")
        comp_key, cfg = match

        nse_symbols = price_engine._nse_symbols_cache
        if not nse_symbols:
            nse_symbols = await price_engine._fetch_nse_symbols()

        stocks = []
        for s in (comp.get("stocks") or []):
            name = s.get("name", "")
            nse = price_engine._resolve_nse(name, [], nse_symbols)
            stocks.append({"name": name, "weight": s.get("weight"), "nseCode": nse, "sector": s.get("sector"), "capSegment": s.get("capSegment")})

        cached = _load_competitor_data()
        entry = {
            "key": comp_key, "label": cfg["label"], "manager": cfg["manager"],
            "stocks": stocks,
            "cagr": comp.get("cagr"),
            "rebalanceTimeline": comp.get("rebalanceTimeline"),
            "marketCapMix": comp.get("marketCapMix"),
            "launchDate": comp.get("launchDate"),
            "latestRebalanceDetail": comp.get("latestRebalanceDetail"),
            "lastFetched": datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC"),
        }
        cached[comp_key] = entry
        _save_competitor_data(cached)
        result["competitor"] = {"label": cfg["label"], "stockCount": len(stocks)}

    return result


@router.post("/api/import-excel-history")
async def import_excel_history(request: Request, basket: str = Form(...), file: UploadFile = File(...)):
    """Import historical index values from an Excel file for a specific basket.
    Excel format: Column A = Date (YYYY-MM-DD), Column B = Basket Value, Column C = Benchmark.
    Only dates AFTER the last already-saved date are imported — existing data is never overwritten.
    """
    _require_admin(request)
    hi = _load_historical_index()

    if basket not in hi:
        raise HTTPException(status_code=400, detail=f"Unknown basket: {basket}")

    raw = await file.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read Excel file: {e}")

    # Prefer a sheet with 'index' or 'value' in its name, else use first sheet
    sheet = next(
        (wb[n] for n in wb.sheetnames if any(k in n.lower() for k in ("index", "value", "historical"))),
        wb.active,
    )

    all_rows = list(sheet.iter_rows(values_only=True))
    if len(all_rows) < 2:
        raise HTTPException(status_code=400, detail="Excel file has no data rows")

    # Parse data rows — skip header (row 0); columns: 0=Date, 1=BasketValue, 2=Benchmark
    parsed = []
    for row in all_rows[1:]:
        if not row[0] or row[1] is None:
            continue
        date_raw = str(row[0]).strip().split(" ")[0]  # strip time component if present
        try:
            # Accept YYYY-MM-DD or DD-MM-YYYY
            if len(date_raw) == 10 and date_raw[4] == "-":
                date_str = date_raw
            else:
                from datetime import datetime as _dt
                date_str = _dt.strptime(date_raw, "%d-%m-%Y").strftime("%Y-%m-%d")
            datetime.strptime(date_str, "%Y-%m-%d")  # validate
        except Exception:
            continue
        try:
            value = round(float(str(row[1]).strip()), 4)
            benchmark = round(float(str(row[2]).strip()), 4) if row[2] is not None else None
        except Exception:
            continue
        if benchmark is None:
            continue
        parsed.append({"date": date_str, "value": value, "benchmark": benchmark})

    existing_dates = {e["date"] for e in hi[basket]["data"]}
    last_date = max(existing_dates) if existing_dates else "0000-00-00"

    # Only import dates strictly after the last saved date
    new_rows = [r for r in parsed if r["date"] > last_date]

    if not new_rows:
        return {"ok": True, "imported": 0, "lastDate": last_date,
                "message": f"Already up to date. Last saved date: {last_date}"}

    for row in new_rows:
        hi[basket]["data"] = [e for e in hi[basket]["data"] if e["date"] != row["date"]]
        hi[basket]["data"].append(row)
    hi[basket]["data"].sort(key=lambda e: e["date"])

    _save_historical_index(hi)

    return {
        "ok": True,
        "imported": len(new_rows),
        "lastDate": last_date,
        "newDates": [r["date"] for r in new_rows],
        "message": f"Imported {len(new_rows)} new date(s) after {last_date}",
    }


# ── Basket auto-detection from Excel column-B header ──────────────────────────
_BASKET_KEYWORDS = {
    "green energy":   "Green_Energy",
    "green":          "Green_Energy",
    "mid & small":    "Mid_Small_Cap",
    "mid and small":  "Mid_Small_Cap",
    "mid small":      "Mid_Small_Cap",
    "mid":            "Mid_Small_Cap",
    "ipo":            "IPO_Basket",
    "consumer trend": "Consumer_Trends",
    "consumer":       "Consumer_Trends",
    "trends trilogy": "Trends_Triology",
    "trends triology":"Trends_Triology",
    "triology":       "Trends_Triology",
    "trilogy":        "Trends_Triology",
    "techstack":      "Techstack",
    "tech stack":     "Techstack",
    "make in india":  "Make_in_India",
    "make":           "Make_in_India",
    "india":          "Make_in_India",
}

def _detect_basket(col_b_header: str) -> str | None:
    h = (col_b_header or "").lower()
    for kw, key in _BASKET_KEYWORDS.items():
        if kw in h:
            return key
    return None


def _parse_excel_rows(raw: bytes) -> tuple[list[dict], str]:
    """Return (parsed_rows, detected_basket_key). Raises ValueError on bad input."""
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    sheet = next(
        (wb[n] for n in wb.sheetnames if any(k in n.lower() for k in ("index", "value", "historical"))),
        wb.active,
    )
    all_rows = list(sheet.iter_rows(values_only=True))
    if len(all_rows) < 2:
        raise ValueError("Excel file has no data rows")

    header     = all_rows[0]
    basket_key = _detect_basket(str(header[1]) if len(header) > 1 else "")

    parsed = []
    for row in all_rows[1:]:
        if not row[0] or row[1] is None:
            continue
        date_raw = str(row[0]).strip().split(" ")[0]
        try:
            if len(date_raw) == 10 and date_raw[4] == "-":
                date_str = date_raw
            else:
                from datetime import datetime as _dt
                date_str = _dt.strptime(date_raw, "%d-%m-%Y").strftime("%Y-%m-%d")
            datetime.strptime(date_str, "%Y-%m-%d")
        except Exception:
            continue
        try:
            value     = round(float(str(row[1]).strip()), 4)
            benchmark = round(float(str(row[2]).strip()), 4) if len(row) > 2 and row[2] is not None else None
        except Exception:
            continue
        if benchmark is None:
            continue
        parsed.append({"date": date_str, "value": value, "benchmark": benchmark})

    return parsed, basket_key


@router.post("/api/import-excel-multi")
async def import_excel_multi(request: Request, files: list[UploadFile] = File(...)):
    """Import multiple Excel files at once. Each file's basket is auto-detected
    from column B header. Only new dates (after the last saved entry) are added."""
    _require_admin(request)
    hi = _load_historical_index()

    results = []
    any_saved = False

    for upload in files:
        fname = upload.filename or "unknown"
        raw   = await upload.read()
        try:
            parsed, basket_key = _parse_excel_rows(raw)
        except Exception as e:
            results.append({"file": fname, "ok": False, "error": str(e)})
            continue

        if not basket_key or basket_key not in hi:
            results.append({"file": fname, "ok": False,
                            "error": f"Could not detect basket from column header. "
                                     f"Please rename column B to include the basket name (e.g. 'Green Energy Theme')."})
            continue

        existing_dates = {e["date"] for e in hi[basket_key]["data"]}
        last_date      = max(existing_dates) if existing_dates else "0000-00-00"
        new_rows       = [r for r in parsed if r["date"] > last_date]

        if not new_rows:
            results.append({"file": fname, "ok": True, "basket": basket_key,
                            "imported": 0, "lastDate": last_date,
                            "message": f"Already up to date (last saved: {last_date})"})
            continue

        for row in new_rows:
            hi[basket_key]["data"] = [e for e in hi[basket_key]["data"] if e["date"] != row["date"]]
            hi[basket_key]["data"].append(row)
        hi[basket_key]["data"].sort(key=lambda e: e["date"])
        any_saved = True

        results.append({"file": fname, "ok": True, "basket": basket_key,
                        "imported": len(new_rows), "lastDate": last_date,
                        "message": f"Imported {len(new_rows)} new date(s) after {last_date}"})

    if any_saved:
        _save_historical_index(hi)

    return {"ok": True, "results": results}


"""Historical rebalance-constituents Excel import, plus rebuilding the sold-
stocks list from buy-price data (recovery tool after data corrections)."""
import io

import openpyxl
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile

from buy_price_gains import (
    _date_to_ts, _add_event,
    _rebuild_sold_from_bp, _recalc_basket_buy_prices, _refresh_gains_file,
)
from persistence import (
    BASKET_DISPLAY_NAMES, _auto_save_rollback,
    _load_portfolios, _save_portfolios,
    _load_buy_price_data, _save_buy_price_data,
    _load_rebalance_history, _save_rebalance_history,
    _require_admin,
)
from rebalance import _parse_excel_date, _resolve_nse_code



@router.post("/api/upload-historical-excel")
async def upload_historical_excel(
    background_tasks: BackgroundTasks,
    request: Request,
    basket: str = Form(...),
    file: UploadFile = File(...),
):
    """Upload an Excel workbook whose 'Historical Constituents' sheet (col A: Date,
    col B: Stock Name, col C: Weight %) contains rebalance history.
    Only dates AFTER the last stored rebalance date are processed."""
    _require_admin(request)

    if basket not in BASKET_DISPLAY_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown basket: {basket}")

    raw = await file.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot open Excel file: {e}")

    # Locate the sheet
    sheet = None
    for name in wb.sheetnames:
        if "historical constituent" in name.lower():
            sheet = wb[name]
            break
    if sheet is None and len(wb.sheetnames) >= 2:
        sheet = wb.worksheets[1]   # fall back to sheet 2
    if sheet is None:
        raise HTTPException(
            status_code=400,
            detail="Sheet 'Historical Constituents' not found. "
                   "Expected sheet 2 or a sheet named 'Historical Constituents'.",
        )

    # Parse rows → { date_str: { stock_name: weight } }
    by_date: dict[str, dict[str, float]] = {}
    skipped = 0
    for row in sheet.iter_rows(min_row=2, values_only=True):
        if not row or len(row) < 3:
            continue
        date_val, stock_val, weight_val = row[0], row[1], row[2]
        if not date_val or not stock_val:
            continue
        date_str = _parse_excel_date(date_val)
        if not date_str:
            skipped += 1
            continue
        try:
            weight = float(weight_val or 0)
        except (TypeError, ValueError):
            weight = 0.0
        stock_name = str(stock_val).strip()
        if not stock_name:
            continue
        by_date.setdefault(date_str, {})[stock_name] = round(weight, 6)

    if not by_date:
        raise HTTPException(status_code=400, detail="No valid data rows found in the sheet.")

    # Sort dates chronologically
    sorted_dates = sorted(by_date.keys(), key=_date_to_ts)

    # Find the last stored rebalance date for this basket
    rh = _load_rebalance_history()
    existing_entries = rh.get(basket, [])
    stored_dates = {e["date"] for e in existing_entries}
    last_stored_ts = max((_date_to_ts(d) for d in stored_dates), default=0)

    # Determine the previous snapshot at last_stored_ts
    # (latest snapshot from existing history whose date == max stored date)
    if stored_dates:
        last_stored_date = max(stored_dates, key=_date_to_ts)
        prev_snap: dict[str, float] = {
            e["nseCode"]: e["weight"]
            for e in existing_entries
            if e["date"] == last_stored_date
        }
    else:
        prev_snap = {}

    # Load supporting data
    bp_data    = _load_buy_price_data()
    portfolios = _load_portfolios()
    basket_bp  = bp_data.setdefault(basket, {})
    basket_stks = portfolios.setdefault(basket, [])
    stk_map    = {s["nseCode"]: s for s in basket_stks}
    sold       = portfolios.setdefault(f"{basket}_sold", [])

    # Process only dates strictly after last_stored_ts
    new_dates = [d for d in sorted_dates if _date_to_ts(d) > last_stored_ts]

    if not new_dates:
        return {
            "ok": True,
            "message": "No new rebalance dates found after the last stored date "
                       f"({max(stored_dates, key=_date_to_ts) if stored_dates else 'none'}).",
            "newDatesProcessed": 0,
        }

    summary: list[dict] = []
    rh_new_entries: list[dict] = []
    bp_changed = False

    for date_str in new_dates:
        if date_str in stored_dates:
            continue   # already stored — skip

        curr_snap_raw: dict[str, float] = by_date[date_str]
        # Resolve stock names → NSE codes
        curr_snap: dict[str, float] = {}
        for stock_name, weight in curr_snap_raw.items():
            code = _resolve_nse_code(stock_name, basket, bp_data)
            curr_snap[code] = weight

        changes: list[dict] = []

        # Stocks in current snapshot
        for code, weight in curr_snap.items():
            prev_weight = prev_snap.get(code, 0.0)
            delta = round(weight - prev_weight, 6)

            if prev_weight == 0.0 and weight > 0:
                action = "Fresh Addition"
                # Buy event
                _add_event(basket_bp, code, "buyEvents", date_str, weight)
                bp_changed = True
                # Add to portfolio if absent
                if code not in stk_map:
                    entry = {"nseCode": code, "allocation": round(weight / 100, 6), "buyPrice": None}
                    basket_stks.append(entry)
                    stk_map[code] = entry
                else:
                    stk_map[code]["allocation"] = round(
                        stk_map[code].get("allocation", 0) + weight / 100, 6)
            elif delta > 0.001:
                action = "Addition"
                _add_event(basket_bp, code, "buyEvents", date_str, delta)
                bp_changed = True
                if code in stk_map:
                    stk_map[code]["allocation"] = round(weight / 100, 6)

            elif delta < -0.001:
                sell_qty = abs(delta)
                action = "Partial Sell" if weight > 0.001 else "Full Removal"
                _add_event(basket_bp, code, "sellEvents", date_str, sell_qty)
                bp_changed = True

                if weight <= 0.001:
                    # Remove from active portfolio → sold list
                    sold.append({
                        "nseCode": code,
                        "securityName": (basket_bp.get(code) or {}).get("securityName", ""),
                        "date": date_str,
                        "action": "Wholly Sold",
                        "weightSold": round(prev_weight, 2),
                        "buyPrice": stk_map.get(code, {}).get("buyPrice"),
                        "sellPrice": None,
                    })
                    basket_stks = [s for s in basket_stks if s["nseCode"] != code]
                    stk_map.pop(code, None)
                else:
                    if code in stk_map:
                        stk_map[code]["allocation"] = round(weight / 100, 6)
            else:
                action = "Unchanged"

            changes.append({"code": code, "action": action,
                             "prev": prev_weight, "curr": weight})

        # Stocks present in prev but absent in curr → fully removed
        for code, prev_weight in prev_snap.items():
            if code not in curr_snap and prev_weight > 0.001:
                _add_event(basket_bp, code, "sellEvents", date_str, prev_weight)
                bp_changed = True
                basket_stks = [s for s in basket_stks if s["nseCode"] != code]
                stk_map.pop(code, None)
                changes.append({"code": code, "action": "Full Removal (absent from new snapshot)",
                                 "prev": prev_weight, "curr": 0})

        # Append to rebalance history
        for code, weight in curr_snap.items():
            sn = (basket_bp.get(code) or {}).get("securityName", "")
            rh_new_entries.append({
                "date": date_str, "nseCode": code,
                "securityName": sn, "segment": "", "weight": weight,
            })

        non_unchanged = [c for c in changes if c["action"] != "Unchanged"]
        summary.append({"date": date_str, "changes": non_unchanged,
                        "total": len(curr_snap), "changed": len(non_unchanged)})
        prev_snap = curr_snap   # roll forward

    # Persist
    rh.setdefault(basket, []).extend(rh_new_entries)
    _save_rebalance_history(rh)

    if bp_changed:
        bp_data[basket] = basket_bp
        _save_buy_price_data(bp_data)

    # Always rebuild sold records from the updated event log
    portfolios[basket] = basket_stks
    portfolios[f"{basket}_sold"] = _rebuild_sold_from_bp(basket_bp, sold)
    _save_portfolios(portfolios)

    # Background: recalc buy prices + refresh gains
    background_tasks.add_task(_recalc_basket_buy_prices, basket)
    background_tasks.add_task(_refresh_gains_file)

    return {
        "ok": True,
        "basket": BASKET_DISPLAY_NAMES[basket],
        "newDatesProcessed": len(new_dates),
        "skippedRows": skipped,
        "summary": summary,
    }


@router.post("/api/rebuild-sold/{basket}")
async def rebuild_sold_endpoint(basket: str, background_tasks: BackgroundTasks, request: Request):
    """Rebuild sold-stock records from buy/sell event log. Fixes wrong weights, actions,
    sell prices, and duplicates caused by earlier code paths."""
    _require_admin(request)
    if basket not in BASKET_DISPLAY_NAMES:
        raise HTTPException(400, f"Unknown basket: {basket}")
    _auto_save_rollback()
    bp_data    = _load_buy_price_data()
    basket_bp  = bp_data.get(basket, {})
    portfolios = _load_portfolios()
    old_sold   = portfolios.get(f"{basket}_sold", [])
    new_sold   = _rebuild_sold_from_bp(basket_bp, old_sold)
    portfolios[f"{basket}_sold"] = new_sold
    _save_portfolios(portfolios)
    background_tasks.add_task(_recalc_basket_buy_prices, basket)
    background_tasks.add_task(_refresh_gains_file)
    return {"ok": True, "basket": BASKET_DISPLAY_NAMES[basket], "recordCount": len(new_sold)}


@router.post("/api/admin/rebalance-insights")
async def rebalance_insights(body: dict):
    """Read-only, open to any logged-in user (same as the rest of Competitor
    Analysis's data endpoints): AI-generated commentary on where OUR basket
    could have done better than one competitor, from a condensed summary the
    frontend already computed (recent rebalance changes, sector/cap mix,
    returns, timing-insight price matches). See rebalance_insights.py for
    the actual prompt and the response-caching that keeps this from calling
    OpenAI more than once per genuinely new rebalance."""
    import rebalance_insights
    basket = body.get("basket") or ""
    competitor_key = body.get("competitorKey") or ""
    summary = body.get("summary") or {}
    if not basket or not competitor_key:
        raise HTTPException(status_code=400, detail="basket and competitorKey are required")
    result = await rebalance_insights.generate_insights(basket, competitor_key, summary)
    return result


