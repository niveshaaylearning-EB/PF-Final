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


from portfolio_data import BUY_PRICE_DETAILS

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

BASKET_DISPLAY_NAMES = {
    "Green_Energy":        "Green Energy",
    "Mid_Small_Cap":       "Mid & Small Cap",
    "IPO_Basket":          "IPO Basket",
    "Trends_Triology":     "Trends Triology",
    "Techstack":           "Techstack",
    "Make_in_India":       "Make in India",
    "Consumer_Trends":     "Consumer Trends",
    "IPO_Recommendations": "IPO Recommendations",
}

LIVE_TTL = 60 * 60  # 60 minutes — longer cache reduces Stooq calls on cloud

YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Explicit Yahoo Finance symbol overrides for NSE codes that don't map to CODE.NS
# e.g. BSE-only SME stocks (numeric codes) or corrected spellings
YF_SYMBOL_MAP: dict = {
    "544531":    "TRUECOLORS.BO",  # True Colors Ltd — BSE SME
    "ACUTAAS":   "ACUTAAS.BO",    # Acutaas Digital — BSE only
    "HBLENGINE": "HBLENGINE.BO",  # HBL Engineering — BSE only
    "ARIS":      "ARIS.BO",       # Arisinfra Solutions — BSE only
    "SETL":      "SETL.BO",       # Standard Engineering Technology — BSE only
}

SCREENER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_PORTFOLIOS_FILE  = Path(__file__).parent / "portfolios.json"
_BUY_PRICE_FILE   = Path(__file__).parent / "buy_price_data.json"
_RH_FILE          = Path(__file__).parent / "rebalance_history.json"
_GAINS_FILE       = Path(__file__).parent / "gains_statement.json"
_UNDO_FILE        = Path(__file__).parent / "undo_snapshots.json"
_ROLLBACK_FILE    = Path(__file__).parent / "rollback_points.json"
_MAX_ROLLBACK_PTS = 5

# ─────────────────────────────────────────────────────────────────────────────
# Portfolio persistence
# ─────────────────────────────────────────────────────────────────────────────

def _load_portfolios() -> dict:
    if _PORTFOLIOS_FILE.exists():
        with open(_PORTFOLIOS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    from portfolio_data import PORTFOLIOS_DATA
    return PORTFOLIOS_DATA


import threading as _threading
import base64 as _base64

def _github_push(file_path: Path, content: str) -> None:
    """Push a file to GitHub in a background thread — free persistent storage."""
    token = os.environ.get("GITHUB_TOKEN", "")
    repo  = os.environ.get("GITHUB_REPO", "")
    if not token or not repo:
        return
    try:
        import urllib.request, urllib.error
        # Relative path from repo root
        rel = str(file_path).replace("\\", "/")
        for prefix in ["/app/", "app/"]:
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
                break

        api_url = f"https://api.github.com/repos/{repo}/contents/{rel}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        # Get current SHA (needed for update)
        req = urllib.request.Request(api_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                sha = json.loads(r.read())["sha"]
        except Exception:
            sha = None

        body = json.dumps({
            "message": f"auto: update {file_path.name}",
            "content": _base64.b64encode(content.encode()).decode(),
            **({"sha": sha} if sha else {}),
        }).encode()
        req2 = urllib.request.Request(api_url, data=body, headers=headers, method="PUT")
        urllib.request.urlopen(req2, timeout=10)
    except Exception as e:
        print(f"[github-push] Failed to push {file_path.name}: {e}")


def _save_and_push(file_path: Path, data: dict) -> None:
    """Write JSON to disk and push to GitHub in background."""
    content = json.dumps(data, indent=2, ensure_ascii=False)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    _threading.Thread(target=_github_push, args=(file_path, content), daemon=True).start()


def _save_portfolios(data: dict) -> None:
    _save_and_push(_PORTFOLIOS_FILE, data)


def _load_buy_price_data() -> dict:
    if _BUY_PRICE_FILE.exists():
        with open(_BUY_PRICE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return dict(BUY_PRICE_DETAILS)   # copy from portfolio_data.py on first use


def _save_buy_price_data(data: dict) -> None:
    _save_and_push(_BUY_PRICE_FILE, data)


def _load_rebalance_history() -> dict:
    if _RH_FILE.exists():
        with open(_RH_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_rebalance_history(data: dict) -> None:
    _save_and_push(_RH_FILE, data)


# ─────────────────────────────────────────────────────────────────────────────
# Persistent undo snapshots (per basket, max 10)
# ─────────────────────────────────────────────────────────────────────────────

def _load_undo_snapshots() -> dict:
    try:
        if _UNDO_FILE.exists():
            with open(_UNDO_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_undo_snapshots(data: dict) -> None:
    with open(_UNDO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── Persistent Yahoo Finance cookie cache ─────────────────────────────────────
_YF_COOKIES: dict = {}

async def _refresh_yf_cookies():
    """Visit Yahoo Finance homepage to get fresh session cookies. Called at startup + on 401."""
    global _YF_COOKIES
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=YF_HEADERS, timeout=10.0) as c:
            r = await c.get("https://finance.yahoo.com/")
            _YF_COOKIES = dict(r.cookies)
            print(f"[YF] Session cookies refreshed ({len(_YF_COOKIES)} cookies)")
    except Exception as e:
        print(f"[YF] Cookie refresh failed: {e}")


def _auto_save_rollback() -> None:
    """Auto-save full system state before any write. Overwrites the single rollback point.
    Never raises — snapshot failure must never abort the actual operation."""
    try:
        point = {
            "id":               "latest",
            "label":            time.strftime("%d %b %Y %H:%M"),
            "createdAt":        time.strftime("%d %b %Y %H:%M"),
            "portfolios":       json.loads(_PORTFOLIOS_FILE.read_text(encoding="utf-8")),
            "buyPriceData":     json.loads(_BUY_PRICE_FILE.read_text(encoding="utf-8")),
            "rebalanceHistory": json.loads(_RH_FILE.read_text(encoding="utf-8")),
        }
        _ROLLBACK_FILE.write_text(json.dumps([point], ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _push_undo_snapshot(basket: str, label: str = "") -> None:
    """Snapshot current basket state before a destructive change. Keeps last 10."""
    pf = _load_portfolios()
    bp = _load_buy_price_data()
    rh = _load_rebalance_history()
    snapshot = {
        "ts":               time.time(),
        "label":            label or f"snapshot at {time.strftime('%d %b %Y %H:%M')}",
        "stocks":           pf.get(basket, []),
        "sold":             pf.get(f"{basket}_sold", []),
        "buyPriceData":     bp.get(basket, {}),
        "rebalanceHistory": rh.get(basket, []),
    }
    snaps = _load_undo_snapshots()
    basket_snaps = snaps.get(basket, [])
    basket_snaps.append(snapshot)
    snaps[basket] = basket_snaps[-10:]   # keep last 10
    _save_undo_snapshots(snaps)


def _all_nse_codes() -> list:
    seen, codes = set(), []
    for stocks in _load_portfolios().values():
        for s in stocks:
            c = s.get("nseCode", "").strip().upper()
            if c and c not in seen:
                seen.add(c)
                codes.append(c)
    return codes


# ─────────────────────────────────────────────────────────────────────────────
# In-memory live-data cache
# ─────────────────────────────────────────────────────────────────────────────

_live_cache: dict = {}
_live_cache_ts: float = 0.0
_live_cache_lock = asyncio.Lock()

_mc_pe_cache: dict = {}          # separate long-lived cache for MC + PE
_mc_pe_cache_ts: float = 0.0
_mc_pe_task_running: bool = False
_MC_PE_TTL = 6 * 3600            # refresh MC/PE every 6 hours

_nse_symbols_cache: list = []
_nse_symbols_ts: float = 0.0
_NSE_SYMBOLS_TTL = 24 * 3600  # refresh once per day

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

# ─────────────────────────────────────────────────────────────────────────────
# Source 1 — Yahoo Finance chart API: CMP + 1-month OHLC
# (No auth required; v7/quote returns 401 so we skip it)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_bhavcopy_prices(codes: list) -> dict:
    """Fetch EOD prices from NSE bhavcopy using requests with browser headers.
    NSE blocks pandas' urllib on cloud IPs — requests with proper headers works.
    """
    results: dict = {}
    code_set = set(c.upper() for c in codes)
    try:
        import pandas as pd
        import requests as _req
        import io
        from datetime import date, timedelta

        _headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Referer": "https://www.nseindia.com/",
        }

        for i in range(1, 7):
            d = (date.today() - timedelta(days=i))
            url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv"
            try:
                r = _req.get(url, headers=_headers, timeout=15)
                if r.status_code != 200:
                    continue
                df = pd.read_csv(io.StringIO(r.text))
                if df is None or df.empty:
                    continue
                df.columns = [c.strip() for c in df.columns]
                df['SYMBOL'] = df['SYMBOL'].str.strip()
                match = df[df['SYMBOL'].isin(code_set)]
                for _, row in match.iterrows():
                    sym = row['SYMBOL']
                    def _f(col):
                        try: v = float(row.get(col, 0) or 0); return v if v > 0 else None
                        except: return None
                    close = _f('CLOSE_PRICE') or _f('LAST_PRICE')
                    if close:
                        results[sym] = {
                            "cmp":     close,
                            "close1M": close,
                            "open1M":  _f('OPEN_PRICE'),
                            "high1M":  _f('HIGH_PRICE'),
                            "low1M":   _f('LOW_PRICE'),
                        }
                if results:
                    print(f"[bhavcopy] Loaded {len(results)} prices from {d.strftime('%d-%m-%Y')}")
                    break
            except Exception as e:
                print(f"[bhavcopy] {d.strftime('%d-%m-%Y')}: {e}")
                continue
    except Exception:
        pass
    return results


async def _fetch_yahoo_charts(codes: list) -> dict:
    """Fetch CMP and 1M OHLC for all codes via Yahoo Finance chart API.

    Handles three symbol variants automatically:
      1. Explicit override via YF_SYMBOL_MAP (e.g. BSE SME numeric codes)
      2. Standard NSE: CODE.NS
      3. NSE SME fallback: CODE-SM.NS (retried for any .NS that returns no data)
    """
    sem = asyncio.Semaphore(25)

    async def _one(sym: str, client: httpx.AsyncClient):
        async with sem:
            url = (
                "https://query1.finance.yahoo.com/v8/finance/chart/"
                + urllib.parse.quote(sym)
                + "?interval=1d&range=1mo"
            )
            try:
                return sym, await client.get(url, timeout=15.0)
            except Exception:
                return sym, None

    def _parse_resp(sym: str, resp, sym_to_code: dict):
        """Return (nse_code, data_dict) or (nse_code, None) on failure."""
        code = sym_to_code.get(sym, sym.split(".")[0].replace("-SM", "").upper())
        if resp is None or isinstance(resp, Exception):
            return code, None
        try:
            if resp.status_code != 200:
                return code, None
            r = (resp.json().get("chart") or {}).get("result") or []
            if not r:
                return code, None
            r     = r[0]
            meta  = r.get("meta") or {}
            q     = ((r.get("indicators") or {}).get("quote") or [{}])[0]
            opens = [v for v in (q.get("open") or []) if v is not None]
            highs = [v for v in (q.get("high") or []) if v is not None]
            lows  = [v for v in (q.get("low")  or []) if v is not None]
            cmp   = meta.get("regularMarketPrice")
            if not cmp:
                return code, None
            return code, {
                "cmp":     cmp,
                "close1M": cmp,
                "open1M":  opens[0]   if opens else None,
                "high1M":  max(highs) if highs else None,
                "low1M":   min(lows)  if lows  else None,
            }
        except Exception:
            return code, None

    # Build primary symbol list (use explicit map or default to .NS)
    sym_to_code: dict = {}
    primary_syms: list = []
    for c in codes:
        sym = YF_SYMBOL_MAP.get(c, f"{c}.NS")
        sym_to_code[sym] = c
        primary_syms.append(sym)

    # Refresh cookies if cache is empty
    if not _YF_COOKIES:
        await _refresh_yf_cookies()

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={**YF_HEADERS, "Referer": "https://finance.yahoo.com/", "Origin": "https://finance.yahoo.com"},
        cookies=_YF_COOKIES,
        timeout=30.0,
    ) as client:
        pairs = await asyncio.gather(
            *[_one(sym, client) for sym in primary_syms],
            return_exceptions=True,
        )

    # If all failed (stale cookies), refresh and retry once
    results_check = [p for p in pairs if isinstance(p, tuple) and p[1] and getattr(p[1], 'status_code', 0) == 200]
    if not results_check and primary_syms:
        await _refresh_yf_cookies()
        async with httpx.AsyncClient(
            follow_redirects=True,
            headers={**YF_HEADERS, "Referer": "https://finance.yahoo.com/", "Origin": "https://finance.yahoo.com"},
            cookies=_YF_COOKIES,
            timeout=30.0,
        ) as client:
            pairs = await asyncio.gather(
                *[_one(sym, client) for sym in primary_syms],
                return_exceptions=True,
            )

    data: dict = {}
    retry_codes: list = []
    for item in pairs:
        if isinstance(item, Exception):
            continue
        sym, resp = item
        code, d = _parse_resp(sym, resp, sym_to_code)
        if d:
            data[code] = d
        elif sym.endswith(".NS") and code not in YF_SYMBOL_MAP:
            # Standard .NS returned nothing — try SME variant next
            retry_codes.append(code)

    # Second pass: retry as NSE SME (CODE-SM.NS) for stocks that got no data
    if retry_codes:
        sm_map = {f"{c}-SM.NS": c for c in retry_codes}
        async with httpx.AsyncClient(follow_redirects=True, headers=YF_HEADERS, timeout=30.0) as client:
            sm_pairs = await asyncio.gather(
                *[_one(sym, client) for sym in sm_map],
                return_exceptions=True,
            )
        for item in sm_pairs:
            if isinstance(item, Exception):
                continue
            sym, resp = item
            code, d = _parse_resp(sym, resp, sm_map)
            if d:
                data[code] = d

    return data


# ─────────────────────────────────────────────────────────────────────────────
# Source 2 — Screener.in HTML scrape: Market Cap (Cr) + Stock P/E
# ─────────────────────────────────────────────────────────────────────────────

_MC_RE = re.compile(r'Market Cap.*?<span[^>]*class="[^"]*number[^"]*"[^>]*>([\d,.]+)</span>', re.DOTALL)
_PE_RE = re.compile(r'Stock P/E.*?<span[^>]*class="[^"]*number[^"]*"[^>]*>([\d,.]+)</span>',  re.DOTALL)


def _parse_screener_html(html: str) -> tuple:
    """Return (marketCapCr, peRatio) parsed from a Screener.in company page."""
    mc, pe = None, None
    mc_m = _MC_RE.search(html)
    pe_m = _PE_RE.search(html)
    if mc_m:
        try:
            mc = float(mc_m.group(1).replace(",", ""))
        except ValueError:
            pass
    if pe_m:
        try:
            pe = round(float(pe_m.group(1).replace(",", "")), 2)
        except ValueError:
            pass
    return mc, pe


_PROXY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
}


def _with_proxies(target: str) -> list:
    """Return proxy URL list — codetabs first (known to work), then fallbacks."""
    enc = urllib.parse.quote(target, safe="")
    return [
        f"https://api.codetabs.com/v1/proxy?quest={enc}",   # primary — works reliably
        target,                                              # direct fallback
        f"https://api.allorigins.win/raw?url={enc}",        # secondary fallback
        f"https://corsproxy.io/?{enc}",                     # tertiary fallback
    ]


async def _get_via_proxies(target: str, timeout: float = 13.0) -> Optional[str]:
    """Fetch target via codetabs proxy (primary), with fallback proxies only if codetabs
    fails with a network/timeout exception (not a valid HTTP error response)."""
    enc = urllib.parse.quote(target, safe="")
    codetabs_url = f"https://api.codetabs.com/v1/proxy?quest={enc}"

    # ── Primary: codetabs ──────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, headers=_PROXY_HEADERS,
            timeout=httpx.Timeout(timeout, connect=timeout),
        ) as client:
            resp = await client.get(codetabs_url)
        if resp.status_code == 200 and resp.text.strip():
            return resp.text
        # codetabs gave a real HTTP response (404, 500, etc.) — other proxies won't help
        return None
    except Exception:
        pass  # codetabs timed out or had a network error — try fallbacks

    # ── Fallbacks (only reached if codetabs had a network/timeout error) ──
    fallbacks = [
        (target,                                              5.0),
        (f"https://api.allorigins.win/raw?url={enc}",        5.0),
        (f"https://corsproxy.io/?{enc}",                     4.0),
    ]
    for url, t in fallbacks:
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, headers=_PROXY_HEADERS,
                timeout=httpx.Timeout(t, connect=t),
            ) as client:
                resp = await client.get(url)
            if resp.status_code == 200 and resp.text.strip():
                return resp.text
        except Exception:
            continue
    return None


async def _screener_search_url(code: str) -> Optional[str]:
    """Call Screener.in search API (via proxy waterfall) to resolve the correct company page URL."""
    target = f"https://www.screener.in/api/company/search/?q={urllib.parse.quote(code)}&v=1"
    text = await _get_via_proxies(target, timeout=13.0)
    if text:
        try:
            results = json.loads(text)
            if isinstance(results, list) and results and results[0].get("url"):
                return results[0]["url"]
        except Exception:
            pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Source 3 — Google Finance HTML scrape: Market Cap + P/E ratio
# ─────────────────────────────────────────────────────────────────────────────

_GF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _parse_google_finance_html(html: str) -> tuple:
    """Return (marketCapCr, peRatio) parsed from a Google Finance quote page.

    Google Finance (en-US) shows Indian stock market caps as ₹1.37T / ₹678B / ₹45M.
    Conversion: 1T INR = 100,000 Cr  |  1B INR = 100 Cr  |  1M INR = 0.1 Cr
    """
    mc, pe = None, None

    # Market cap — ₹ followed by number and T/B/M suffix
    for pat in (
        r'Market\s+cap[^<]{0,600}?₹\s*([\d.]+)\s*([TBM])\b',
        r'"Market cap"[^"]{0,300}?"₹([\d.]+)\s*([TBM])"',
    ):
        m = re.search(pat, html, re.DOTALL | re.IGNORECASE)
        if m:
            try:
                val    = float(m.group(1))
                suffix = m.group(2).upper()
                if   suffix == 'T': mc = round(val * 100_000)
                elif suffix == 'B': mc = round(val * 100)
                elif suffix == 'M': mc = max(1, round(val * 0.1))
                break
            except (ValueError, AttributeError):
                continue

    # P/E ratio — plain number near the "P/E ratio" label
    for pat in (
        r'P/E\s+ratio[^<]{0,600}?>([\d.]+)<',
        r'"P/E ratio"[^"]{0,200}?"([\d.]+)"',
    ):
        m = re.search(pat, html, re.DOTALL | re.IGNORECASE)
        if m:
            try:
                pe = round(float(m.group(1)), 2)
                break
            except ValueError:
                continue

    return mc, pe


# ─────────────────────────────────────────────────────────────────────────────
# Source 4 — NSE India API: Market Cap via issuedSize × price
#            (Individual-stock P/E is not exposed by this endpoint)
# ─────────────────────────────────────────────────────────────────────────────

_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com",
    "X-Requested-With": "XMLHttpRequest",
}


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio PDF parsing — password-protected report upload
# ─────────────────────────────────────────────────────────────────────────────

_R = b'\x76\x72\x69\x6e\x73\x67\x78\x70\x73\x35\x31\x31\x39\x66'

_PDF_SECTIONS = {
    "additions":                       "addition",
    "increase in weight allocation":   "increase",
    "removals":                        "removal",
    "decrease in weight allocation":   "decrease",
    "no change in weight allocation":  "no_change",
}

_HOLDING_TYPES = [
    "Large & Mid Cap", "Large and Mid Cap",
    "Largecap", "Large Cap", "Midcap", "Mid Cap",
    "Smallcap", "Small Cap", "Microcap", "Micro Cap",
    "Flexicap", "Flex Cap", "Multicap", "Multi Cap",
]

_NAME_STRIP = re.compile(
    r'\b(ltd|limited|pvt|private|inc|corp|corporation|enterprises|industries|co)\b\.?',
    re.IGNORECASE,
)


def _norm_name(name: str) -> str:
    return re.sub(r'\s+', ' ', _NAME_STRIP.sub('', name.lower())).strip()


def _parse_portfolio_pdf(raw: bytes) -> list:
    """Decrypt and parse portfolio PDF; return list of section entries."""
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        if not reader.decrypt(_R.decode()):
            raise ValueError("PDF decryption failed")

    full_text = "\n".join(page.extract_text() or "" for page in reader.pages)

    entries = []
    current = None
    for line in full_text.splitlines():
        line = line.strip()
        if not line:
            continue
        ll = line.lower()

        # Detect section header
        for hdr, stype in _PDF_SECTIONS.items():
            if hdr in ll:
                current = stype
                break
        else:
            if not current:
                continue
            if "holding type" in ll or ll.startswith("weightage"):
                continue

            # Extract first percentage (= new weight)
            wm = re.search(r'(\d+(?:\.\d+)?)\s*%', line)
            if not wm:
                continue
            new_weight = float(wm.group(1))

            # Find and remove holding type to isolate company name
            holding = "Equity"
            name_part = line
            for ht in sorted(_HOLDING_TYPES, key=len, reverse=True):
                if ht.lower() in ll:
                    holding = ht
                    idx = ll.index(ht.lower())
                    name_part = line[:idx]
                    break
            else:
                name_part = re.sub(r'\d+(?:\.\d+)?\s*%.*', '', name_part)

            company = name_part.strip().rstrip('-').strip()
            if company and len(company) > 2:
                entries.append({
                    "section":     current,
                    "companyName": company,
                    "holdingType": holding,
                    "newWeight":   new_weight,
                })

    return entries


def _resolve_nse(company: str, portfolio: list, symbols: list) -> Optional[str]:
    """Map company name → NSE code: portfolio match → symbols exact → symbols partial."""
    norm = _norm_name(company)
    # 1. Existing portfolio
    for s in portfolio:
        if _norm_name(s.get("securityName", "")) == norm:
            return s["nseCode"]
    # 2. NSE symbols exact
    for sym in symbols:
        if _norm_name(sym["name"]) == norm:
            return sym["symbol"]
    # 3. NSE symbols partial (only for names ≥6 chars to avoid false positives)
    if len(norm) >= 6:
        for sym in symbols:
            sn = _norm_name(sym["name"])
            if norm in sn or sn in norm:
                return sym["symbol"]
    return None


async def _fetch_yahoo_mc_pe(code: str) -> tuple:
    """Fetch Market Cap (Cr) and trailing PE from Yahoo Finance quoteSummary."""
    try:
        url = (
            f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{code}.NS"
            "?modules=summaryDetail"
        )
        async with httpx.AsyncClient(
            follow_redirects=True, headers=YF_HEADERS, timeout=10.0
        ) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            sd = (
                ((resp.json().get("quoteSummary") or {}).get("result") or [{}])[0]
                .get("summaryDetail") or {}
            )
            mc_raw = (sd.get("marketCap") or {}).get("raw")
            pe_raw = (sd.get("trailingPE") or {}).get("raw")
            mc = round(mc_raw / 1e7) if mc_raw else None
            pe = round(pe_raw, 1) if pe_raw else None
            return mc, pe
    except Exception:
        pass
    return None, None


async def _try_nse_mc(code: str) -> Optional[int]:
    """Compute Market Cap (Cr) from NSE India: lastPrice × issuedSize / 1e7."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=15.0,
            headers={"User-Agent": _NSE_HEADERS["User-Agent"],
                     "Accept": "text/html,application/xhtml+xml"},
        ) as client:
            await client.get("https://www.nseindia.com", timeout=10.0)
            resp = await client.get(
                f"https://www.nseindia.com/api/quote-equity?symbol={code}",
                headers=_NSE_HEADERS, timeout=12.0,
            )
        if resp.status_code != 200:
            return None
        data   = resp.json()
        price  = ((data.get("priceInfo") or {}).get("lastPrice")
                  or (data.get("priceInfo") or {}).get("close"))
        issued = (data.get("metadata") or {}).get("issuedSize")
        if price and issued:
            return round(float(price) * float(issued) / 1e7)
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Combined MC + PE fetch — Screener → Google Finance → Yahoo Finance → NSE India
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_mc_pe_one(code: str, sem: asyncio.Semaphore) -> tuple:
    """
    Fetch Market Cap (Cr) and P/E for one stock. Source order (strict):
    1. Screener.in   — HTML scrape; most accurate for Indian stocks
    2. Google Finance — fallback if Screener fails/missing
    3. Yahoo Finance  — quoteSummary; fallback if Google fails
    4. NSE India      — last resort; MC only (no PE)
    """
    async with sem:
        mc, pe = None, None

        # ── 1. Screener.in (proxy waterfall) ─────────────────────────────
        # Try direct URL first (fast for most stocks); only fall back to
        # Search API if direct URLs don't have top-ratios (mismatched slug).
        try:
            for target in [
                f"https://www.screener.in/company/{code}/consolidated/",
                f"https://www.screener.in/company/{code}/",
            ]:
                if mc is not None and pe is not None:
                    break
                html = await _get_via_proxies(target, timeout=13.0)
                if html and "top-ratios" in html:
                    sc_mc, sc_pe = _parse_screener_html(html)
                    if sc_mc is not None and mc is None:
                        mc = round(sc_mc)
                    if sc_pe is not None and pe is None:
                        pe = sc_pe

            # Fallback: slug mismatch — resolve via Search API
            if (mc is None or pe is None):
                company_url = await _screener_search_url(code)
                if company_url:
                    html = await _get_via_proxies(
                        f"https://www.screener.in{company_url}", timeout=13.0
                    )
                    if html and "top-ratios" in html:
                        sc_mc, sc_pe = _parse_screener_html(html)
                        if sc_mc is not None and mc is None:
                            mc = round(sc_mc)
                        if sc_pe is not None and pe is None:
                            pe = sc_pe
        except Exception:
            pass

        if mc is not None and pe is not None:
            return code, {"marketCapCr": mc, "peRatio": pe}

        # ── 2. Google Finance ─────────────────────────────────────────────
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, headers=_GF_HEADERS, timeout=12.0
            ) as client:
                resp = await client.get(
                    f"https://www.google.com/finance/quote/{code}:NSE",
                    timeout=12.0,
                )
            if resp.status_code == 200:
                gf_mc, gf_pe = _parse_google_finance_html(resp.text)
                if mc is None and gf_mc is not None:
                    mc = gf_mc
                if pe is None and gf_pe is not None:
                    pe = gf_pe
        except Exception:
            pass

        if mc is not None and pe is not None:
            return code, {"marketCapCr": mc, "peRatio": pe}

        # ── 3. Yahoo Finance ──────────────────────────────────────────────
        if mc is None or pe is None:
            yf_mc, yf_pe = await _fetch_yahoo_mc_pe(code)
            if mc is None and yf_mc is not None:
                mc = yf_mc
            if pe is None and yf_pe is not None:
                pe = yf_pe

        if mc is not None and pe is not None:
            return code, {"marketCapCr": mc, "peRatio": pe}

        # ── 4. NSE India (market cap only) ────────────────────────────────
        if mc is None:
            nse_mc = await _try_nse_mc(code)
            if nse_mc is not None:
                mc = nse_mc

        return code, ({"marketCapCr": mc, "peRatio": pe} if (mc is not None or pe is not None) else {})


async def _fetch_screener_batch(codes: list) -> dict:
    """Fetch Market Cap and PE for all codes using cascade: Screener → Google → NSE."""
    sem     = asyncio.Semaphore(10)
    results = await asyncio.gather(
        *[_fetch_mc_pe_one(c, sem) for c in codes],
        return_exceptions=True,
    )
    data: dict = {}
    for r in results:
        if isinstance(r, Exception):
            continue
        code, metrics = r
        if metrics:
            data[code] = metrics
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Combined batch fetch — both sources in parallel, cached 15 min
# ─────────────────────────────────────────────────────────────────────────────

async def _mc_pe_background_refresh() -> None:
    """Fetch MC+PE for all stocks in background; patches live cache when done."""
    global _mc_pe_cache, _mc_pe_cache_ts, _mc_pe_task_running
    _mc_pe_task_running = True
    try:
        codes = _all_nse_codes()
        sem   = asyncio.Semaphore(10)
        results = await asyncio.gather(
            *[_fetch_mc_pe_one(c, sem) for c in codes],
            return_exceptions=True,
        )
        fresh: dict = {}
        for r in results:
            if isinstance(r, Exception):
                continue
            code, metrics = r
            if metrics:
                fresh[code] = metrics
        _mc_pe_cache    = fresh
        _mc_pe_cache_ts = time.time()
        # Patch the live cache so current page loads see the new MC/PE immediately
        async with _live_cache_lock:
            for code, metrics in fresh.items():
                if code in _live_cache:
                    _live_cache[code].update(metrics)
    except Exception:
        pass
    finally:
        _mc_pe_task_running = False


async def fetch_live_batch() -> dict:
    """
    Fetch live data for every stock across all baskets.
    - If MC/PE cache is warm: Yahoo Finance runs alone (~5-10s), returns immediately.
    - If MC/PE cache is cold (first load / 6h refresh): Yahoo + Screener run concurrently;
      waits up to 75s so the first page load includes MC/PE data.
    - After first load the MC/PE cache is warm and all subsequent loads are fast.
    """
    global _live_cache, _live_cache_ts, _mc_pe_cache, _mc_pe_cache_ts, _mc_pe_task_running

    async with _live_cache_lock:
        if _live_cache and (time.time() - _live_cache_ts) < LIVE_TTL:
            return _live_cache

        codes = _all_nse_codes()
        mc_pe_cold = not _mc_pe_cache or (time.time() - _mc_pe_cache_ts) >= _MC_PE_TTL

        # Always fetch Yahoo Finance (fast ~5-10s) and return immediately.
        # Screener batch (MC/PE) always runs in background — 307 stocks via proxy
        # takes ~5-10 min; background task patches _live_cache when done.
        # Primary: Yahoo Finance chart API with cookie warm-up (works on cloud)
        # NSE bhavcopy is geo-blocked from non-Indian IPs (Render is US-based)
        try:
            yahoo_data = await _fetch_yahoo_charts(codes)
        except Exception:
            yahoo_data = {}

        if mc_pe_cold and not _mc_pe_task_running:
            asyncio.create_task(_mc_pe_background_refresh())

        # Build merged response
        data: dict = {}
        for code in codes:
            yf = yahoo_data.get(code, {}) if isinstance(yahoo_data, dict) else {}
            sc = _mc_pe_cache.get(code, {})
            data[code] = {
                "cmp":         yf.get("cmp"),
                "close1M":     yf.get("close1M"),
                "open1M":      yf.get("open1M"),
                "high1M":      yf.get("high1M"),
                "low1M":       yf.get("low1M"),
                "marketCapCr": sc.get("marketCapCr"),
                "peRatio":     sc.get("peRatio"),
            }

        _live_cache    = data
        _live_cache_ts = time.time()
        return data


# ─────────────────────────────────────────────────────────────────────────────
# Single-stock lookup (used when a stock isn't found in the batch cache)
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_live_single(nse_code: str) -> Optional[dict]:
    code   = nse_code.strip().upper()
    sym    = f"{code}.NS"

    async with httpx.AsyncClient(follow_redirects=True, headers=YF_HEADERS, timeout=12.0) as yf_client:
        chart_resp = await asyncio.gather(
            yf_client.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/"
                + urllib.parse.quote(sym) + "?interval=1d&range=1mo"
            ),
            return_exceptions=True,
        )
    chart_resp = chart_resp[0]

    result: dict = {}

    # CMP + OHLC from Yahoo chart
    if not isinstance(chart_resp, Exception) and chart_resp.status_code == 200:
        try:
            r = (chart_resp.json().get("chart") or {}).get("result") or [None]
            r = r[0]
            if r:
                meta  = r.get("meta") or {}
                q     = ((r.get("indicators") or {}).get("quote") or [{}])[0]
                opens = [v for v in (q.get("open") or []) if v is not None]
                highs = [v for v in (q.get("high") or []) if v is not None]
                lows  = [v for v in (q.get("low")  or []) if v is not None]
                cmp   = meta.get("regularMarketPrice")
                if cmp:
                    result.update({
                        "cmp":     cmp,
                        "close1M": cmp,
                        "open1M":  opens[0]   if opens else None,
                        "high1M":  max(highs) if highs else None,
                        "low1M":   min(lows)  if lows  else None,
                    })
        except Exception:
            pass

    # Market Cap + PE — cascade: Screener.in → Google Finance → NSE India
    sem = asyncio.Semaphore(1)
    _, metrics = await _fetch_mc_pe_one(code, sem)
    result.update(metrics)

    return result or None


# ─────────────────────────────────────────────────────────────────────────────
# NSE equity symbol list — fetched from NSE archives CSV, cached 24 h
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_nse_symbols() -> list:
    """Fetch all NSE-listed equity symbols + company names from NSE archives CSV."""
    global _nse_symbols_cache, _nse_symbols_ts

    if _nse_symbols_cache and (time.time() - _nse_symbols_ts) < _NSE_SYMBOLS_TTL:
        return _nse_symbols_cache

    url = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=30.0,
            headers={"User-Agent": YF_HEADERS["User-Agent"], "Accept": "text/csv,*/*"},
        ) as client:
            resp = await client.get(url, timeout=25.0)

        if resp.status_code == 200:
            reader = csv.reader(io.StringIO(resp.text))
            next(reader, None)          # skip header row
            symbols = []
            for row in reader:
                if len(row) >= 2:
                    sym  = row[0].strip()
                    name = row[1].strip()
                    if sym and name:
                        symbols.append({"symbol": sym, "name": name})
            if symbols:
                _nse_symbols_cache = symbols
                _nse_symbols_ts    = time.time()
                return symbols
    except Exception:
        pass

    return _nse_symbols_cache   # return stale cache on failure


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/baskets")
async def get_baskets():
    return BASKET_DISPLAY_NAMES


@app.post("/api/debug/pdf-text")
async def debug_pdf_text(file: UploadFile = File(...)):
    """Return raw text extracted from the PDF (for debugging only)."""
    raw = await file.read()
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        reader.decrypt(_R.decode())
    pages = []
    for i, page in enumerate(reader.pages):
        pages.append({"page": i + 1, "text": page.extract_text() or ""})
    return {"pages": pages}


@app.get("/api/debug/mcpe")
async def debug_mcpe():
    return {
        "mc_pe_cache_size": len(_mc_pe_cache),
        "mc_pe_task_running": _mc_pe_task_running,
        "mc_pe_cache_age_s": round(time.time() - _mc_pe_cache_ts, 1) if _mc_pe_cache_ts else None,
        "live_cache_size": len(_live_cache),
        "live_cache_age_s": round(time.time() - _live_cache_ts, 1) if _live_cache_ts else None,
        "sample": {k: _mc_pe_cache[k] for k in list(_mc_pe_cache.keys())[:5]} if _mc_pe_cache else {},
    }


@app.get("/api/nse-symbols")
async def get_nse_symbols():
    """Return full NSE equity symbol list for autocomplete (cached 24 h)."""
    return await _fetch_nse_symbols()


# ── Buy Price Calculation (OHLC weighted avg) ─────────────────────────────────

_MONTH_FULL = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
    "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,"aug":8,
    "sep":9,"oct":10,"nov":11,"dec":12,
}
_MONTH_ABBR = {
    1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
    7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec",
}

def _normalise_date(raw: str) -> str:
    """Convert 'DDth Month YYYY' or 'DD Month YYYY' to 'DD Mon YYYY'."""
    raw = raw.strip()
    # Remove ordinal suffixes: 1st 2nd 3rd 4th ... 31st
    raw = re.sub(r'(\d+)(st|nd|rd|th)\b', r'\1', raw, flags=re.IGNORECASE)
    parts = raw.split()
    if len(parts) != 3:
        return raw
    day, month, year = parts
    m = _MONTH_FULL.get(month.lower())
    if m:
        return f"{int(day):02d} {_MONTH_ABBR[m]} {year}"
    return raw


def _parse_buy_events(buy_events_str: str) -> list[tuple[str, float]]:
    """Parse 'DD Mon YYYY * qty' (or ordinal variants) → [(date_str, qty), ...]"""
    events = []
    for line in buy_events_str.strip().split('\n'):
        parts = re.split(r'[*×]', line.strip())
        if len(parts) != 2:
            continue
        try:
            date_str = _normalise_date(parts[0])
            events.append((date_str, float(parts[1].strip())))
        except ValueError:
            pass
    return events


def _total_to_delta_events(
    buy_events_total: list[tuple[str, float]],
    sell_events_delta: list[tuple[str, float]],
) -> list[tuple[str, float]]:
    """Convert total-weight buy events to delta weights for FIFO/formula use.
    Walks events chronologically; sell events (delta) reduce the running weight
    so subsequent buy deltas are computed correctly."""
    combined = (
        [(d, "buy",  q) for d, q in buy_events_total] +
        [(d, "sell", q) for d, q in sell_events_delta]
    )
    combined.sort(key=lambda e: _date_to_ts(e[0]))

    cw: float = 0.0
    result: list[tuple[str, float]] = []

    for date_str, etype, qty in combined:
        if etype == "buy":
            delta = qty - cw
            if delta > 0.001:
                result.append((date_str, round(delta, 6)))
            cw = qty
        else:
            cw = max(0.0, cw - qty)

    return result


def _compute_allocation(
    buy_events_delta: list[tuple[str, float]],
    sell_events_delta: list[tuple[str, float]],
) -> float:
    """Compute current net weight (as 0–1 fraction) from delta buy/sell events."""
    combined = (
        [(d, "buy",  q) for d, q in buy_events_delta] +
        [(d, "sell", q) for d, q in sell_events_delta]
    )
    combined.sort(key=lambda e: _date_to_ts(e[0]))

    net: float = 0.0
    for date_str, etype, qty in combined:
        if etype == "buy":
            net += qty
        else:
            net = max(0.0, net - qty)

    return round(net / 100, 6)


def _current_series_buy_events(
    buy_events: list[tuple[str, float]],
    sell_events: list[tuple[str, float]],
) -> list[tuple[str, float]]:
    """Return buy events for the current active series.

    Both buyEvents and sellEvents are stored as delta weights.
    Resets the active series when net weight reaches zero (full exit).
    Returns (date, delta_weight) pairs for the weighted-avg buy-price formula.
    """
    combined = (
        [(d, "buy",  q) for d, q in buy_events] +
        [(d, "sell", q) for d, q in sell_events]
    )
    combined.sort(key=lambda e: _date_to_ts(e[0]))

    net: float = 0.0
    series: list[tuple[str, float]] = []

    for date_str, etype, qty in combined:
        if etype == "buy":
            if net <= 0.001:            # fresh entry after full exit
                series = []
            series.append((date_str, round(qty, 6)))
            net += qty
        else:                           # sell
            net = max(0.0, net - qty)
            if net <= 0.001:            # fully exited — close series
                series = []
                net = 0.0

    return series


async def _fetch_ohlc_yahoo(nse_code: str, ts: int) -> tuple[float | None, str | None]:
    """OHLC avg from Yahoo Finance for Unix timestamp ts.
    Uses a 4-day window so weekends/holidays are handled — takes the first valid bar.
    Returns (price, actual_date_str) where actual_date_str is set only when the bar
    date differs from the requested date (i.e. a next-trading-day fallback was used)."""
    try:
        sym = YF_SYMBOL_MAP.get(nse_code, f"{nse_code}.NS")
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
            f"?interval=1d&period1={ts}&period2={ts + 4 * 86400}"
        )
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        result = r.json()["chart"]["result"][0]
        q = result["indicators"]["quote"][0]
        timestamps = result.get("timestamp", [])
        for i, (o, h, l, c) in enumerate(zip(q["open"], q["high"], q["low"], q["close"])):
            if None not in (o, h, l, c):
                # Accept the bar on or nearest after the target date
                if not timestamps or timestamps[i] >= ts - 86400:
                    price = round((o + h + l + c) / 4, 4)
                    fallback_date = None
                    if timestamps:
                        actual_date = datetime.utcfromtimestamp(timestamps[i]).date()
                        target_date = datetime.utcfromtimestamp(ts).date()
                        if actual_date != target_date:
                            fallback_date = actual_date.strftime("%d %b %Y")
                    return price, fallback_date
    except Exception:
        pass
    return None, None


async def _fetch_open_price_yahoo_sym(sym: str, ts: int) -> float | None:
    """Opening price from Yahoo Finance for an explicit symbol on a given UTC timestamp."""
    try:
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            + urllib.parse.quote(sym)
            + f"?interval=1d&period1={ts}&period2={ts + 7 * 86400}"
        )
        async with httpx.AsyncClient(follow_redirects=True, timeout=15, headers=YF_HEADERS) as client:
            r = await client.get(url)
        if r.status_code != 200:
            return None
        chart = r.json().get("chart") or {}
        results = chart.get("result") or []
        if not results:
            return None
        result = results[0]
        q = (result.get("indicators") or {}).get("quote") or [{}]
        q = q[0]
        timestamps = result.get("timestamp") or []
        for i, o in enumerate(q.get("open") or []):
            if o is not None:
                if not timestamps or timestamps[i] >= ts - 86400:
                    return round(o, 2)
    except Exception:
        pass
    return None


async def _fetch_open_price_for_listing(nse_code: str, date_str: str) -> float | None:
    """Opening price on listing date — tries YF_SYMBOL_MAP override, then .NS, then -SM.NS."""
    dt = None
    for fmt in ("%d %b %Y", "%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
    if nse_code in YF_SYMBOL_MAP:
        return await _fetch_open_price_yahoo_sym(YF_SYMBOL_MAP[nse_code], ts)
    # Try .NS first, then SME fallback
    val = await _fetch_open_price_yahoo_sym(f"{nse_code}.NS", ts)
    if val is not None:
        return val
    return await _fetch_open_price_yahoo_sym(f"{nse_code}-SM.NS", ts)


async def _backfill_ipo_listing_prices() -> None:
    """Fetch opening price for IPO stocks that have a listingDate but no buyPrice yet.
    Fetches all missing prices in parallel, then persists portfolios.json."""
    try:
        bp_data    = _load_buy_price_data()
        basket_bp  = bp_data.get("IPO_Recommendations", {})
        portfolios = _load_portfolios()
        stk_map    = {s["nseCode"]: s for s in portfolios.get("IPO_Recommendations", [])}

        to_fetch = [
            (code, det["listingDate"].strip())
            for code, det in basket_bp.items()
            if (det.get("listingDate") or "").strip()
            and not (stk_map.get(code, {}).get("buyPrice") or 0) > 0
        ]
        if not to_fetch:
            return

        prices = await asyncio.gather(
            *[_fetch_open_price_for_listing(code, date) for code, date in to_fetch],
            return_exceptions=True,
        )

        pf_changed = False
        for (code, _), price in zip(to_fetch, prices):
            if isinstance(price, Exception) or price is None:
                continue
            if code in stk_map:
                stk_map[code]["buyPrice"] = price
            else:
                entry = {"nseCode": code, "allocation": 0, "buyPrice": price}
                portfolios["IPO_Recommendations"].append(entry)
                stk_map[code] = entry
            pf_changed = True

        if pf_changed:
            _save_portfolios(portfolios)
    except Exception:
        pass


async def _fetch_ohlc_google(nse_code: str, dt: datetime) -> tuple[float | None, str | None]:
    """OHLC avg from Google Finance historical data (fallback).
    Returns (price, actual_date_str) — actual_date_str set when date differs from requested."""
    try:
        # Google Finance getprices endpoint — returns ~30 days of daily OHLC
        url = (
            f"https://finance.google.com/finance/getprices"
            f"?q={nse_code}&x=NSE&i=86400&p=40d&f=d,o,h,l,c,v&df=cpct&auto=1"
        )
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        lines = r.text.strip().splitlines()
        # Format: first data row starts with "a<unix_ts>", subsequent rows are offset in days
        base_ts = None
        target_ts   = int(dt.replace(tzinfo=timezone.utc).timestamp())
        target_date = dt.date()
        for line in lines:
            if line.startswith("TIMEZONE_OFFSET") or line.startswith("MARKET") or line.startswith("EXCHANGE"):
                continue
            if line.startswith("a"):
                parts = line.split(",")
                base_ts = int(parts[0][1:])
                offset  = 0
            else:
                parts = line.split(",")
                try:
                    offset = int(parts[0])
                except ValueError:
                    continue
            if base_ts is None or len(parts) < 5:
                continue
            row_ts = base_ts + offset * 86400
            if abs(row_ts - target_ts) < 4 * 86400:       # within 4 days (handles weekends/holidays)
                o, c, h, l = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
                if 0 not in (o, h, l, c):
                    price       = round((o + h + l + c) / 4, 4)
                    actual_date = datetime.utcfromtimestamp(row_ts).date()
                    fallback    = actual_date.strftime("%d %b %Y") if actual_date != target_date else None
                    return price, fallback
    except Exception:
        pass
    return None, None


async def _fetch_ohlc_screener(nse_code: str, dt: datetime) -> tuple[float | None, str | None]:
    """Close price from Screener.in — last-resort fallback for stocks not on NSE
    (e.g. BSE-only IPO allotments). Returns (price, fallback_date_str_or_None)."""
    try:
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
        api_headers = {
            "User-Agent": ua,
            "Accept": "application/json, */*",
            "Referer": "https://www.screener.in/",
            "X-Requested-With": "XMLHttpRequest",
        }
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            # Establish session cookies
            await client.get(
                f"https://www.screener.in/company/{nse_code}/consolidated/",
                headers={"User-Agent": ua},
            )
            # Resolve company ID
            search_r = await client.get(
                f"https://www.screener.in/api/company/search/?q={nse_code}&v=3&fts=1",
                headers=api_headers,
            )
            company_id = None
            for item in search_r.json():
                if f"/company/{nse_code}/" in item.get("url", ""):
                    company_id = item.get("id")
                    break
            if not company_id:
                return None, None
            # Fetch 400-day price chart
            chart_r = await client.get(
                f"https://www.screener.in/api/company/{company_id}/chart/"
                f"?q=Price-DMA50-DMA200-Volume&days=400&consolidated=true",
                headers=api_headers,
            )
            prices: dict[str, float] = {}
            for ds in chart_r.json().get("datasets", []):
                if ds.get("metric") == "Price":
                    for entry in ds.get("values", []):
                        prices[entry[0]] = float(entry[1])
                    break
        # Find price on target date or within +4 day forward window
        target_date = dt.date()
        for i in range(5):
            check = (dt + timedelta(days=i)).strftime("%Y-%m-%d")
            if check in prices:
                actual = (dt + timedelta(days=i)).date()
                fallback = actual.strftime("%d %b %Y") if actual != target_date else None
                return round(prices[check], 2), fallback
    except Exception:
        pass
    return None, None


async def _fetch_ohlc_avg(nse_code: str, date_str: str) -> tuple[float | None, str | None]:
    """Fetch OHLC avg (O+H+L+C)/4 — Yahoo Finance first, Google Finance second,
    Screener.in last resort (for BSE-only / pre-listing stocks not on NSE).
    Returns (price, fallback_date) where fallback_date is the actual date used when
    it differs from the requested date (i.e. next-trading-day fallback)."""
    dt = datetime.strptime(date_str, "%d %b %Y")
    ts = int(dt.replace(tzinfo=timezone.utc).timestamp())

    val, fallback = await _fetch_ohlc_yahoo(nse_code, ts)
    if val is not None:
        return val, fallback

    val, fallback = await _fetch_ohlc_google(nse_code, dt)
    if val is not None:
        return val, fallback

    return await _fetch_ohlc_screener(nse_code, dt)


@app.post("/api/set-ohlc-price")
async def set_ohlc_price(body: dict):
    """Manually override a buyOHLC or sellOHLC price for a specific stock and date.
    Body: {basket, code, date, price, type} where type is 'buy' or 'sell'.
    Regenerates gains_statement.json after saving."""
    basket = body.get("basket", "")
    code   = body.get("code", "")
    date   = body.get("date", "")
    price  = body.get("price")
    kind   = body.get("type", "buy")  # 'buy' or 'sell'

    if not basket or not code or not date or price is None:
        raise HTTPException(status_code=422, detail="basket, code, date, price are required")

    price = float(price)
    bp_data   = _load_buy_price_data()
    basket_bp = bp_data.get(basket, {})
    det       = basket_bp.get(code)
    if det is None:
        raise HTTPException(status_code=404, detail=f"{code} not found in {basket}")

    ohlc_field = "buyOHLC" if kind == "buy" else "sellOHLC"
    det.setdefault(ohlc_field, {})[date] = round(price, 4)

    _save_buy_price_data(bp_data)
    gains = _compute_all_gains()
    with open(_GAINS_FILE, "w", encoding="utf-8") as f:
        json.dump(gains, f, indent=2, ensure_ascii=False)

    return {"ok": True, "code": code, "date": date, "type": kind, "price": round(price, 4)}


@app.post("/api/refetch-buy-ohlc/{basket}/{code}")
async def refetch_buy_ohlc(basket: str, code: str):
    """Force re-fetch OHLC for ALL buy event dates of a stock, overwriting stored values.
    Also re-fetches sell OHLC for its sell event dates.
    Regenerates gains_statement.json after update."""
    bp_data   = _load_buy_price_data()
    basket_bp = bp_data.get(basket, {})
    det       = basket_bp.get(code)
    if det is None:
        raise HTTPException(status_code=404, detail=f"{code} not found in {basket}")

    results: dict = {"buy": {}, "sell": {}}

    # Re-fetch all buy event dates
    for field in ("prevBuyEvents", "buyEvents"):
        for line in (det.get(field) or "").strip().split("\n"):
            line = line.strip()
            if " * " not in line:
                continue
            date_str = line.split(" * ")[0].strip()
            if not date_str:
                continue
            price, fallback = await _fetch_ohlc_avg(code, date_str)
            if price is not None:
                det.setdefault("buyOHLC", {})[date_str] = price
                if fallback:
                    det.setdefault("buyOHLC_fallback", {})[date_str] = fallback
                elif date_str in det.get("buyOHLC_fallback", {}):
                    det["buyOHLC_fallback"].pop(date_str, None)
                results["buy"][date_str] = price

    # Re-fetch all sell event dates
    for field in ("prevSellEvents", "sellEvents"):
        for line in (det.get(field) or "").strip().split("\n"):
            line = line.strip()
            if " * " not in line:
                continue
            date_str = line.split(" * ")[0].strip()
            if not date_str:
                continue
            price, fallback = await _fetch_ohlc_avg(code, date_str)
            if price is not None:
                det.setdefault("sellOHLC", {})[date_str] = price
                if fallback:
                    det.setdefault("sellOHLC_fallback", {})[date_str] = fallback
                elif date_str in det.get("sellOHLC_fallback", {}):
                    det["sellOHLC_fallback"].pop(date_str, None)
                results["sell"][date_str] = price

    _save_buy_price_data(bp_data)

    # Regenerate gains statement
    gains = _compute_all_gains()
    with open(_GAINS_FILE, "w", encoding="utf-8") as f:
        json.dump(gains, f, indent=2, ensure_ascii=False)

    return {"ok": True, "code": code, "basket": basket, "prices": results}


@app.get("/api/calc-buy-price/{key}/{nse}")
async def calc_buy_price(key: str, nse: str):
    """Calculate weighted avg buy price for one stock from its buy events."""
    bp_data = _load_buy_price_data()
    det = bp_data.get(key, {}).get(nse, {})
    buy_events_str = det.get("buyEvents") or ""
    if not buy_events_str:
        raise HTTPException(status_code=404, detail="No buy events for this stock")

    all_buy  = _parse_buy_events(buy_events_str)
    all_sell = _parse_buy_events(det.get("sellEvents") or "")
    events   = _current_series_buy_events(all_buy, all_sell)
    if not events:
        raise HTTPException(status_code=422, detail="Could not parse buy events")

    # Use cached buyOHLC prices where available; only fetch what is missing
    cached_ohlc = det.get("buyOHLC") or {}
    ohlc_avgs: list[float | None] = []
    newly_fetched: dict[str, float] = {}
    newly_fetched_fallbacks: dict[str, str] = {}
    for date_str, _ in events:
        if date_str in cached_ohlc:
            ohlc_avgs.append(cached_ohlc[date_str])
        else:
            val, fallback_date = await _fetch_ohlc_avg(nse, date_str)
            ohlc_avgs.append(val)
            if val is not None:
                newly_fetched[date_str] = val
            if fallback_date:
                newly_fetched_fallbacks[date_str] = fallback_date

    # Persist any newly fetched OHLC prices and fallback metadata
    if newly_fetched or newly_fetched_fallbacks:
        bp_data[key][nse]["buyOHLC"] = {**cached_ohlc, **newly_fetched}
        if newly_fetched_fallbacks:
            bp_data[key][nse].setdefault("buyOHLC_fallback", {}).update(newly_fetched_fallbacks)
        _save_buy_price_data(bp_data)

    failed = [events[i][0] for i, v in enumerate(ohlc_avgs) if v is None]
    if failed:
        raise HTTPException(status_code=502, detail=f"OHLC unavailable for: {', '.join(failed)}")

    total_qty     = sum(qty for _, qty in events)
    weighted_sum  = sum(qty * avg for (_, qty), avg in zip(events, ohlc_avgs))
    buy_price     = round(weighted_sum / total_qty, 2)

    # Persist into the stocks array so dashboard reflects it immediately
    portfolios = _load_portfolios()
    for s in portfolios.get(key, []):
        if s["nseCode"] == nse:
            s["buyPrice"] = buy_price
            break
    _save_portfolios(portfolios)

    all_fallbacks = {**det.get("buyOHLC_fallback", {}), **newly_fetched_fallbacks}
    return {
        "buyPrice": buy_price,
        "events":   [
            {"date": d, "qty": q, "ohlcAvg": a, "fallbackDate": all_fallbacks.get(d)}
            for (d, q), a in zip(events, ohlc_avgs)
        ],
        "fallbacks": {d: v for d, v in all_fallbacks.items() if d in {ev[0] for ev in events}},
    }


@app.post("/api/calc-all-baskets")
async def calc_all_baskets():
    """Calculate and persist weighted avg buy prices for every stock with buy events across all baskets."""
    bp_data    = _load_buy_price_data()
    portfolios = _load_portfolios()
    results    = {}  # key → { nse: buyPrice | "error" }
    total_ok   = 0
    total_err  = 0
    bp_changed = False

    for key in BASKET_DISPLAY_NAMES:
        basket_bp   = bp_data.get(key, {})
        basket_stks = portfolios.get(key, [])
        stk_map     = {s["nseCode"]: s for s in basket_stks}
        results[key] = {}

        for nse, det in basket_bp.items():
            # Sync allocation from buy/sell events
            buy_ev_str  = det.get("buyEvents")  or ""
            sell_ev_str = det.get("sellEvents") or ""
            buy_ev_all  = _parse_buy_events(buy_ev_str)
            sell_ev_all = _parse_buy_events(sell_ev_str)
            if buy_ev_all:
                allocation = _compute_allocation(buy_ev_all, sell_ev_all)
                if nse in stk_map:
                    stk_map[nse]["allocation"] = allocation
                else:
                    new_stk = {"nseCode": nse, "allocation": allocation}
                    basket_stks.append(new_stk)
                    stk_map[nse] = new_stk

            events = _current_series_buy_events(buy_ev_all, sell_ev_all)
            if not events:
                continue

            try:
                # Use cached buyOHLC prices where available
                cached_ohlc = det.get("buyOHLC") or {}
                ohlc_avgs: list[float | None] = []
                newly_fetched: dict[str, float] = {}
                for date_str, _ in events:
                    if date_str in cached_ohlc:
                        ohlc_avgs.append(cached_ohlc[date_str])
                    else:
                        val, fallback_date = await _fetch_ohlc_avg(nse, date_str)
                        ohlc_avgs.append(val)
                        if val is not None:
                            newly_fetched[date_str] = val
                        if fallback_date:
                            det.setdefault("buyOHLC_fallback", {})[date_str] = fallback_date
                            bp_changed = True

                # Persist newly fetched OHLC prices
                if newly_fetched:
                    det["buyOHLC"] = {**cached_ohlc, **newly_fetched}
                    bp_changed = True

                if any(v is None for v in ohlc_avgs):
                    failed = [events[i][0] for i, v in enumerate(ohlc_avgs) if v is None]
                    results[key][nse] = f"OHLC missing: {', '.join(failed)}"
                    total_err += 1
                    continue

                total_qty    = sum(qty for _, qty in events)
                weighted_sum = sum(qty * avg for (_, qty), avg in zip(events, ohlc_avgs))
                buy_price    = round(weighted_sum / total_qty, 2)

                # Save into portfolios
                if nse in stk_map:
                    stk_map[nse]["buyPrice"] = buy_price
                else:
                    basket_stks.append({"nseCode": nse, "allocation": 0, "buyPrice": buy_price})
                    stk_map[nse] = basket_stks[-1]

                results[key][nse] = buy_price
                total_ok += 1
            except Exception as exc:
                results[key][nse] = str(exc)
                total_err += 1

        portfolios[key] = basket_stks

    if bp_changed:
        _save_buy_price_data(bp_data)

    _save_portfolios(portfolios)
    return {"ok": True, "calculated": total_ok, "errors": total_err, "detail": results}


def _date_to_ts(date_str: str) -> int:
    """Convert 'DD MMM YYYY' to Unix timestamp for chronological sorting."""
    try:
        return int(datetime.strptime(date_str.strip(), "%d %b %Y").replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return 0


def _compute_fifo_gains_for_series(
    buy_events: list[tuple[str, float]],
    sell_events: list[tuple[str, float]],
    buy_ohlc: dict,
    sell_ohlc: dict,
) -> list[dict]:
    """FIFO gain calculation for one buy/sell series.
    Each sell event is matched against the oldest unconsumed buy lots first.
    Returns a list of per-sell-event records with lot-level breakdown."""
    if not buy_events or not sell_events:
        return []

    buy_queue = sorted(
        [{"date": d, "remaining": q, "price": buy_ohlc.get(d)} for d, q in buy_events],
        key=lambda e: _date_to_ts(e["date"]),
    )
    sell_events_sorted = sorted(sell_events, key=lambda e: _date_to_ts(e[0]))

    gains = []
    for sell_date, sell_weight in sell_events_sorted:
        sell_price = sell_ohlc.get(sell_date)
        remaining = sell_weight
        lots = []

        for lot in buy_queue:
            if remaining < 1e-6:
                break
            if lot["remaining"] < 1e-6:
                continue
            take = min(lot["remaining"], remaining)
            lot["remaining"] = round(lot["remaining"] - take, 6)
            remaining = round(remaining - take, 6)

            buy_price = lot["price"]
            gain_pct = None
            if buy_price and sell_price and buy_price > 0:
                gain_pct = round((sell_price - buy_price) / buy_price * 100, 2)

            lots.append({
                "buyDate":  lot["date"],
                "weight":   round(take, 4),
                "buyPrice": buy_price,
                "gainPct":  gain_pct,
            })

        valid = [l for l in lots if l["gainPct"] is not None]
        total_w = sum(l["weight"] for l in valid)
        wt_gain = (
            round(sum(l["gainPct"] * l["weight"] for l in valid) / total_w, 2)
            if total_w > 0 else None
        )

        remaining_qty = sum(l["remaining"] for l in buy_queue)
        sell_type = "Full Exit" if remaining_qty < 0.05 else "Partial Sell"

        # Buy price method:
        #   Full Exit  → weighted avg of ALL series buy events (matches _wavg_cost_basis used in sold stocks tab)
        #   Partial Sell → FIFO-weighted avg of lots consumed (already correct in `lots`)
        if sell_type == "Full Exit":
            wt_buy_price = _wavg_cost_basis(buy_events, buy_ohlc)
        else:
            lots_with_price = [l for l in lots if l["buyPrice"] is not None]
            total_w_bp = sum(l["weight"] for l in lots_with_price)
            wt_buy_price = (
                round(sum(l["buyPrice"] * l["weight"] for l in lots_with_price) / total_w_bp, 4)
                if total_w_bp > 0 else None
            )

        gains.append({
            "sellDate":            sell_date,
            "sellWeight":          sell_weight,
            "sellPrice":           sell_price,
            "sellType":            sell_type,
            "lots":                lots,
            "weightedGainPct":     wt_gain,
            "weightedAvgBuyPrice": wt_buy_price,
        })

    return gains


def _rebuild_sold_from_bp(basket_bp: dict, existing_sold: list) -> list:
    """Derive sold-stock records from buy/sell event strings in basket_bp.
    This is the authoritative rebuild — weights, actions, and sell prices come
    from the event log. Preserves already-computed buyPrices from existing records."""
    # Primary lookup: (code, date) — exact match when records have dates
    bp_by_key: dict = {}
    # Fallback lookup: code only — used when records were previously stripped of dates
    bp_by_code: dict = {}
    for rec in existing_sold:
        bp = rec.get("buyPrice")
        if bp is None:
            continue
        code = rec.get("nseCode", "")
        date = rec.get("date", "")
        if date:
            bp_by_key.setdefault((code, date), []).append(bp)
        else:
            bp_by_code.setdefault(code, []).append(bp)

    sold: list = []
    for code, det in basket_bp.items():
        sec_name  = det.get("securityName", "")
        sell_ohlc = det.get("sellOHLC") or {}

        for buy_str, sell_str in [
            (det.get("prevBuyEvents"), det.get("prevSellEvents")),
            (det.get("buyEvents"),     det.get("sellEvents")),
        ]:
            buys  = _parse_buy_events(buy_str  or "")
            sells = _parse_buy_events(sell_str or "")
            if not sells:
                continue
            for sell_date, sell_qty in sells:
                ts           = _date_to_ts(sell_date)
                total_bought = sum(q for d, q in buys  if _date_to_ts(d) <= ts)
                total_sold   = sum(q for d, q in sells if _date_to_ts(d) <= ts)
                remaining    = max(0.0, round(total_bought - total_sold, 6))
                is_full      = remaining < 0.05
                # Try exact (code, date) match first; fall back to code-only queue
                keyed = bp_by_key.get((code, sell_date), [])
                if keyed:
                    buy_p = keyed.pop(0)
                else:
                    fallback = bp_by_code.get(code, [])
                    buy_p = fallback.pop(0) if fallback else None
                sold.append({
                    "nseCode":      code,
                    "securityName": sec_name,
                    "date":         sell_date,
                    "action":       "Wholly Sold" if is_full else "Partially Sold",
                    "weightSold":   round(sell_qty, 4),
                    "buyPrice":     buy_p,
                    "sellPrice":    sell_ohlc.get(sell_date),
                })

    return sold


def _compute_all_gains() -> dict:
    """Compute FIFO gains for every stock across all baskets using stored OHLC data.
    No network calls — purely derived from buy_price_data.json."""
    bp_data = _load_buy_price_data()
    result: dict = {}

    for basket_key in BASKET_DISPLAY_NAMES:
        basket_bp = bp_data.get(basket_key, {})
        basket_result: dict = {}

        for nse, det in basket_bp.items():
            buy_ev      = _parse_buy_events(det.get("buyEvents")      or "")
            sell_ev     = _parse_buy_events(det.get("sellEvents")     or "")
            prev_buy_ev = _parse_buy_events(det.get("prevBuyEvents")  or "")
            prev_sell_ev= _parse_buy_events(det.get("prevSellEvents") or "")
            buy_ohlc    = det.get("buyOHLC")  or {}
            sell_ohlc   = det.get("sellOHLC") or {}

            # All baskets now store delta weights in buyEvents
            prev_gains = _compute_fifo_gains_for_series(
                prev_buy_ev, prev_sell_ev, buy_ohlc, sell_ohlc
            )
            curr_gains = _compute_fifo_gains_for_series(
                buy_ev, sell_ev, buy_ohlc, sell_ohlc
            )

            if prev_gains or curr_gains:
                basket_result[nse] = {
                    "securityName":      det.get("securityName", ""),
                    "prevSeriesGains":   prev_gains,
                    "currentSeriesGains": curr_gains,
                }

        if basket_result:
            result[basket_key] = basket_result

    return result


def _build_history_from_events(bp_basket: dict) -> dict:
    """
    Derive per-stock rebalance history from buyEvents / sellEvents stored in
    buy_price_data.json.  Returns the same shape the React StockInfoTooltip expects:
      { nse: { added: "DD MMM YYYY", rebalances: [{ date, note }] } }
    Previous-series events (prevBuyEvents / prevSellEvents) are included in the
    full timeline but labelled as past series entries.
    """
    history: dict = {}
    for nse, det in bp_basket.items():
        buy_events       = _parse_buy_events(det.get("buyEvents")      or "")
        sell_events      = _parse_buy_events(det.get("sellEvents")     or "")
        prev_buy_events  = _parse_buy_events(det.get("prevBuyEvents")  or "")
        prev_sell_events = _parse_buy_events(det.get("prevSellEvents") or "")

        all_events = buy_events + sell_events + prev_buy_events + prev_sell_events
        if not all_events:
            continue

        # Merge all events into one chronological list
        combined = []
        for date_str, qty in buy_events:
            combined.append({"date": date_str, "note": f"Buy {qty:g}%", "_ts": _date_to_ts(date_str)})
        for date_str, qty in sell_events:
            combined.append({"date": date_str, "note": f"Sell {qty:g}%", "_ts": _date_to_ts(date_str)})
        for date_str, qty in prev_buy_events:
            combined.append({"date": date_str, "note": f"Buy {qty:g}% (prev)", "_ts": _date_to_ts(date_str)})
        for date_str, qty in prev_sell_events:
            combined.append({"date": date_str, "note": f"Sell {qty:g}% (prev)", "_ts": _date_to_ts(date_str)})

        combined.sort(key=lambda e: e["_ts"])

        rebalances = [{"date": e["date"], "note": e["note"]} for e in combined]

        # Earliest buy event = when the stock was first added
        added = min((e["date"] for e in combined if e["note"].startswith("Buy")),
                    key=_date_to_ts, default=None)

        history[nse] = {"added": added, "rebalances": rebalances}

    return history


async def _fetch_listing_date_nse(symbol: str) -> str | None:
    """Fetch listing date for a stock from NSE India API. Returns e.g. '30-Jul-2025'."""
    headers = {
        "User-Agent":      YF_HEADERS["User-Agent"],
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://www.nseindia.com/",
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=15.0, follow_redirects=True) as c:
            await c.get("https://www.nseindia.com/", timeout=10.0)
            r = await c.get(
                "https://www.nseindia.com/api/quote-equity?symbol="
                + urllib.parse.quote(symbol),
                timeout=10.0,
            )
            if r.status_code != 200:
                return None
            d = r.json()
            ld = (
                (d.get("metadata") or {}).get("listingDate")
                or (d.get("info")     or {}).get("listingDate")
            )
            return str(ld).strip() if ld else None
    except Exception:
        return None


async def _backfill_ipo_listing_dates() -> None:
    """Background: fetch & persist missing listing dates for IPO_Recommendations stocks."""
    try:
        portfolios = _load_portfolios()
        ipo_stocks = portfolios.get("IPO_Recommendations", [])
        if not ipo_stocks:
            return
        bp_data   = _load_buy_price_data()
        basket_bp = bp_data.get("IPO_Recommendations", {})
        changed   = False
        for s in ipo_stocks:
            code = s.get("nseCode", "").strip()
            if not code:
                continue
            if (basket_bp.get(code) or {}).get("listingDate"):
                continue  # already cached
            ld = await _fetch_listing_date_nse(code)
            if ld:
                if code not in basket_bp:
                    basket_bp[code] = {}
                basket_bp[code]["listingDate"] = ld
                changed = True
        if changed:
            bp_data["IPO_Recommendations"] = basket_bp
            _save_buy_price_data(bp_data)
    except Exception:
        pass


@app.get("/api/basket/{key}")
async def get_basket(key: str, background_tasks: BackgroundTasks):
    portfolios = _load_portfolios()
    bp_data    = _load_buy_price_data().get(key, {})
    if key == "IPO_Recommendations":
        background_tasks.add_task(_backfill_ipo_listing_dates)
        # Synchronously fill any missing listing prices so they appear on first load
        await _backfill_ipo_listing_prices()
        portfolios = _load_portfolios()   # reload in case prices were just written
        bp_data    = _load_buy_price_data().get(key, {})
    return {
        "stocks":          portfolios.get(key, []),
        "soldStocks":      portfolios.get(f"{key}_sold", []),
        "history":         _build_history_from_events(bp_data),
        "buyPriceDetails": bp_data,
    }


async def _recalc_basket_buy_prices(key: str) -> None:
    """Background task: recalculate OHLC-weighted avg buy prices for all stocks
    in a basket that have buy events, then persist results to portfolios.json."""
    try:
        bp_data     = _load_buy_price_data()
        portfolios  = _load_portfolios()
        basket_bp   = bp_data.get(key, {})
        basket_stks = portfolios.get(key, [])
        stk_map     = {s["nseCode"]: s for s in basket_stks}
        pf_changed  = False
        bp_changed  = False

        for nse, det in basket_bp.items():
            all_buy   = _parse_buy_events(det.get("buyEvents")  or "")
            all_sell  = _parse_buy_events(det.get("sellEvents") or "")
            buy_events = _current_series_buy_events(all_buy, all_sell)
            if not buy_events:
                continue
            try:
                # Use cached buyOHLC prices where available
                cached_ohlc = det.get("buyOHLC") or {}
                ohlc_avgs: list[float | None] = []
                newly_fetched: dict[str, float] = {}
                for date_str, _ in buy_events:
                    if date_str in cached_ohlc:
                        ohlc_avgs.append(cached_ohlc[date_str])
                    else:
                        val, fallback_date = await _fetch_ohlc_avg(nse, date_str)
                        ohlc_avgs.append(val)
                        if val is not None:
                            newly_fetched[date_str] = val
                        if fallback_date:
                            det.setdefault("buyOHLC_fallback", {})[date_str] = fallback_date
                            bp_changed = True

                if newly_fetched:
                    det["buyOHLC"] = {**cached_ohlc, **newly_fetched}
                    bp_changed = True

                if any(v is None for v in ohlc_avgs):
                    continue  # skip if any event date is unavailable
                total_qty    = sum(qty for _, qty in buy_events)
                weighted_sum = sum(qty * avg for (_, qty), avg in zip(buy_events, ohlc_avgs))
                buy_price    = round(weighted_sum / total_qty, 2)
                if nse not in stk_map:
                    continue  # skip sold/non-active stocks
                stk_map[nse]["buyPrice"] = buy_price
                pf_changed = True
            except Exception:
                continue

        if pf_changed:
            portfolios[key] = basket_stks
            _save_portfolios(portfolios)
        if bp_changed:
            _save_buy_price_data(bp_data)
    except Exception:
        pass  # background task — never crash the server


async def _refresh_gains_file() -> None:
    """Background task: recompute FIFO gains from current buy_price_data and persist."""
    try:
        gains = _compute_all_gains()
        with open(_GAINS_FILE, "w", encoding="utf-8") as f:
            json.dump(gains, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


async def _backfill_all_sell_ohlc_bg() -> None:
    """Background task: fetch any missing sell OHLC prices across ALL baskets,
    then regenerate gains_statement.json. Runs as the final step after a rebalance
    to ensure every sell event has a price regardless of which basket was updated."""
    try:
        bp_data = _load_buy_price_data()
        filled = 0
        for basket_bp in bp_data.values():
            for code, det in basket_bp.items():
                sell_ohlc = det.setdefault("sellOHLC", {})
                for field in ("prevSellEvents", "sellEvents"):
                    for line in (det.get(field) or "").strip().split("\n"):
                        line = line.strip()
                        if " * " not in line:
                            continue
                        date_str = line.split(" * ")[0].strip()
                        if not date_str or date_str in sell_ohlc:
                            continue
                        price, fallback = await _fetch_ohlc_avg(code, date_str)
                        if price:
                            sell_ohlc[date_str] = price
                            if fallback:
                                det.setdefault("sellOHLC_fallback", {})[date_str] = fallback
                            filled += 1
        if filled:
            _save_buy_price_data(bp_data)
        gains = _compute_all_gains()
        with open(_GAINS_FILE, "w", encoding="utf-8") as f:
            json.dump(gains, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


@app.get("/api/undo-count/{basket}")
async def get_undo_count(basket: str):
    snaps = _load_undo_snapshots()
    return {"count": len(snaps.get(basket, []))}


@app.post("/api/undo/{basket}")
async def undo_basket(basket: str, background_tasks: BackgroundTasks):
    snaps = _load_undo_snapshots()
    basket_snaps = snaps.get(basket, [])
    if not basket_snaps:
        raise HTTPException(400, "No undo history available for this basket.")
    snapshot = basket_snaps.pop()
    snaps[basket] = basket_snaps
    _save_undo_snapshots(snaps)

    pf = _load_portfolios()
    pf[basket]                  = snapshot["stocks"]
    pf[f"{basket}_sold"]        = snapshot["sold"]
    _save_portfolios(pf)

    bp = _load_buy_price_data()
    bp[basket] = snapshot["buyPriceData"]
    _save_buy_price_data(bp)

    rh = _load_rebalance_history()
    rh[basket] = snapshot["rebalanceHistory"]
    _save_rebalance_history(rh)

    background_tasks.add_task(_recalc_basket_buy_prices, basket)
    background_tasks.add_task(_refresh_gains_file)
    return {"ok": True, "remainingUndos": len(basket_snaps)}


@app.put("/api/basket/{key}")
async def save_basket(key: str, body: dict, background_tasks: BackgroundTasks):
    if key not in BASKET_DISPLAY_NAMES:
        raise HTTPException(status_code=404, detail=f"Unknown basket: {key}")
    _auto_save_rollback()
    _push_undo_snapshot(key, f"before save {time.strftime('%d %b %Y %H:%M')}")
    portfolios = _load_portfolios()
    portfolios[key] = body.get("stocks", [])
    if "soldStocks" in body:
        portfolios[f"{key}_sold"] = body["soldStocks"]

    if "buyPriceDetails" in body:
        bp_data = _load_buy_price_data()
        bp_data[key] = body["buyPriceDetails"]
        _save_buy_price_data(bp_data)

        # Re-derive allocations from buy/sell events — backend is authoritative source
        stk_map = {s["nseCode"]: s for s in portfolios[key]}
        for nse, det in bp_data[key].items():
            buy_events  = _parse_buy_events(det.get("buyEvents")  or "")
            sell_events = _parse_buy_events(det.get("sellEvents") or "")
            if not buy_events:
                continue
            allocation = _compute_allocation(buy_events, sell_events)
            if nse in stk_map:
                stk_map[nse]["allocation"] = allocation
            else:
                new_stk = {"nseCode": nse, "allocation": allocation}
                portfolios[key].append(new_stk)
                stk_map[nse] = new_stk

        # Auto-recalculate buy prices in background after every save
        background_tasks.add_task(_recalc_basket_buy_prices, key)
        # Refresh gains statement so P&L page reflects latest sell events
        background_tasks.add_task(_refresh_gains_file)

    # For IPO basket, auto-fetch opening prices for stocks with a listing date
    if key == "IPO_Recommendations":
        background_tasks.add_task(_backfill_ipo_listing_prices)

    _save_portfolios(portfolios)
    return {"ok": True, "saved": len(portfolios[key])}


@app.get("/api/gains-statement")
async def get_gains_statement():
    """Return FIFO gains statement for all stocks with sell events.
    Serves from gains_statement.json if it exists; otherwise computes fresh."""
    if _GAINS_FILE.exists():
        with open(_GAINS_FILE, encoding="utf-8") as f:
            return json.load(f)
    gains = _compute_all_gains()
    with open(_GAINS_FILE, "w", encoding="utf-8") as f:
        json.dump(gains, f, indent=2, ensure_ascii=False)
    return gains


@app.post("/api/gains-statement/refresh")
async def refresh_gains_statement():
    """Recompute FIFO gains from current buy_price_data.json and persist."""
    gains = _compute_all_gains()
    with open(_GAINS_FILE, "w", encoding="utf-8") as f:
        json.dump(gains, f, indent=2, ensure_ascii=False)
    total = sum(len(v) for v in gains.values())
    return {"ok": True, "stocksWithGains": total}


@app.post("/api/gains-statement/backfill-sell-ohlc")
async def backfill_sell_ohlc():
    """Fetch missing sellOHLC prices for all sell event dates across all baskets,
    then regenerate gains_statement.json."""
    bp_data = _load_buy_price_data()
    filled = 0

    for basket_key, basket_bp in bp_data.items():
        for code, det in basket_bp.items():
            sell_ohlc = det.setdefault("sellOHLC", {})
            for field in ("prevSellEvents", "sellEvents"):
                for line in (det.get(field) or "").strip().split("\n"):
                    line = line.strip()
                    if " * " not in line:
                        continue
                    date_str = line.split(" * ")[0].strip()
                    if not date_str or date_str in sell_ohlc:
                        continue
                    price, fallback = await _fetch_ohlc_avg(code, date_str)
                    if price:
                        sell_ohlc[date_str] = price
                        if fallback:
                            det.setdefault("sellOHLC_fallback", {})[date_str] = fallback
                        filled += 1

    _save_buy_price_data(bp_data)
    gains = _compute_all_gains()
    with open(_GAINS_FILE, "w", encoding="utf-8") as f:
        json.dump(gains, f, indent=2, ensure_ascii=False)

    return {"ok": True, "pricesFilled": filled}


@app.get("/api/ohlc-fallbacks/{basket}")
async def get_ohlc_fallbacks(basket: str):
    """Return all stocks in a basket that used next-trading-day OHLC fallbacks.
    Each entry has buyFallbacks and sellFallbacks dicts mapping
    {requested_date: actual_date_used}."""
    bp_data   = _load_buy_price_data()
    basket_bp = bp_data.get(basket, {})
    result    = {}
    for nse, det in basket_bp.items():
        buy_fb  = det.get("buyOHLC_fallback",  {})
        sell_fb = det.get("sellOHLC_fallback", {})
        if buy_fb or sell_fb:
            result[nse] = {
                "securityName":  det.get("securityName", ""),
                "buyFallbacks":  buy_fb,
                "sellFallbacks": sell_fb,
            }
    return result


@app.get("/api/index-history")
async def get_index_history():
    """Serve pre-computed historical index values for all baskets."""
    p = Path(__file__).parent / "historical_index.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="historical_index.json not found")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


@app.post("/api/daily-values")
async def post_daily_values(body: dict):
    """Append (or update) daily basket + benchmark index values in historical_index.json.
    Body: { "date": "YYYY-MM-DD", "entries": [ { "basket": key, "value": float, "benchmark": float }, ... ] }
    If an entry for the given date already exists, it is overwritten."""
    date_str = (body.get("date") or "").strip()
    entries  = body.get("entries") or []

    if not date_str:
        raise HTTPException(status_code=400, detail="date is required")
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    p = Path(__file__).parent / "historical_index.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="historical_index.json not found")

    with open(p, encoding="utf-8") as f:
        hi = json.load(f)

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

    with open(p, "w", encoding="utf-8") as f:
        json.dump(hi, f, indent=2, ensure_ascii=False)

    return {"ok": True, "date": date_str, "saved": saved}


@app.post("/api/import-excel-history")
async def import_excel_history(basket: str = Form(...), file: UploadFile = File(...)):
    """Import historical index values from an Excel file for a specific basket.
    Excel format: Column A = Date (YYYY-MM-DD), Column B = Basket Value, Column C = Benchmark.
    Only dates AFTER the last already-saved date are imported — existing data is never overwritten.
    """
    p = Path(__file__).parent / "historical_index.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="historical_index.json not found")

    with open(p, encoding="utf-8") as f:
        hi = json.load(f)

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

    with open(p, "w", encoding="utf-8") as f:
        json.dump(hi, f, indent=2, ensure_ascii=False)

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


@app.post("/api/import-excel-multi")
async def import_excel_multi(files: list[UploadFile] = File(...)):
    """Import multiple Excel files at once. Each file's basket is auto-detected
    from column B header. Only new dates (after the last saved entry) are added."""
    p = Path(__file__).parent / "historical_index.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="historical_index.json not found")

    with open(p, encoding="utf-8") as f:
        hi = json.load(f)

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
        with open(p, "w", encoding="utf-8") as f:
            json.dump(hi, f, indent=2, ensure_ascii=False)

    return {"ok": True, "results": results}


@app.get("/api/listing-price/{nse_code}")
async def get_listing_price(nse_code: str, date: str):
    """Return the opening price of a stock on its listing date."""
    code  = nse_code.strip().upper()
    price = await _fetch_open_price_for_listing(code, date)
    return {"price": price}


@app.get("/api/live")
async def get_live_all():
    return await fetch_live_batch()


@app.get("/api/live/{nse_code}")
async def get_live_single(nse_code: str):
    code  = nse_code.strip().upper()
    cache = await fetch_live_batch()
    if code in cache and cache[code].get("cmp") is not None:
        return cache[code]
    result = await fetch_live_single(code)
    if result:
        return result
    return {
        "cmp": None, "close1M": None, "open1M": None,
        "high1M": None, "low1M": None, "marketCapCr": None, "peRatio": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Sector / Segment fetching  (NSE India → Yahoo Finance → Screener.in)
# ─────────────────────────────────────────────────────────────────────────────

_SECTOR_RE = re.compile(
    r'(?:class="[^"]*tag[^"]*"|href="/screens/[^"]*")\s*[^>]*>([^<]{2,60})</a>',
    re.IGNORECASE,
)

async def _fetch_sector(code: str) -> str | None:
    """Try NSE India → Yahoo Finance → Screener.in to get the sector/industry name."""

    # 1. NSE India ──────────────────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=15.0,
            headers={"User-Agent": _NSE_HEADERS["User-Agent"],
                     "Accept": "text/html,application/xhtml+xml"},
        ) as client:
            await client.get("https://www.nseindia.com", timeout=10.0)
            resp = await client.get(
                f"https://www.nseindia.com/api/quote-equity?symbol={code}",
                headers=_NSE_HEADERS, timeout=12.0,
            )
        if resp.status_code == 200:
            data = resp.json()
            industry = (
                (data.get("metadata") or {}).get("industry") or
                (data.get("info")     or {}).get("industry")
            )
            if industry and industry.strip() not in ("", "-"):
                return industry.strip()
    except Exception:
        pass

    # 2. Yahoo Finance ──────────────────────────────────────────────────────────
    try:
        url = (f"https://query1.finance.yahoo.com/v11/finance/quoteSummary/{code}.NS"
               "?modules=assetProfile")
        async with httpx.AsyncClient(timeout=10.0,
                                     headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            qs = resp.json().get("quoteSummary") or {}
            results = (qs.get("result") or [{}])
            sector = (results[0].get("assetProfile") or {}).get("sector") if results else None
            if sector and sector.strip():
                return sector.strip()
    except Exception:
        pass

    # 3. Screener.in ────────────────────────────────────────────────────────────
    try:
        for path in [f"/company/{code}/consolidated/", f"/company/{code}/"]:
            html = await _get_via_proxies(f"https://www.screener.in{path}", timeout=13.0)
            if html:
                m = _SECTOR_RE.search(html)
                if m:
                    val = m.group(1).strip()
                    if val and val.lower() not in ("", "screener", "home"):
                        return val
                break
    except Exception:
        pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
# CSV Rebalance Upload
# ─────────────────────────────────────────────────────────────────────────────

def _wavg_cost_basis(
    series_buys: list[tuple[str, float]],
    buy_ohlc: dict,
) -> float | None:
    """Weighted average buy price across all lots in the current series."""
    total_cost = total_qty = 0.0
    for date_str, qty in series_buys:
        price = buy_ohlc.get(date_str)
        if price:
            total_cost += qty * price
            total_qty  += qty
    return round(total_cost / total_qty, 4) if total_qty > 1e-6 else None


def _fifo_cost_basis(
    series_buys: list[tuple[str, float]],
    prior_sells: list[tuple[str, float]],
    sell_qty: float,
    buy_ohlc: dict,
) -> float | None:
    """FIFO-weighted average buy price for selling sell_qty units."""
    buy_queue = sorted(
        [{"date": d, "remaining": q, "price": buy_ohlc.get(d)} for d, q in series_buys],
        key=lambda e: _date_to_ts(e["date"]),
    )
    # drain all prior sells through the FIFO queue
    for _, prev_qty in sorted(prior_sells, key=lambda e: _date_to_ts(e[0])):
        rem = prev_qty
        for lot in buy_queue:
            if rem < 1e-6:
                break
            if lot["remaining"] < 1e-6:
                continue
            take = min(lot["remaining"], rem)
            lot["remaining"] = round(lot["remaining"] - take, 6)
            rem = round(rem - take, 6)
    # consume sell_qty and compute cost
    total_cost = total_qty = 0.0
    rem = sell_qty
    for lot in buy_queue:
        if rem < 1e-6:
            break
        if lot["remaining"] < 1e-6 or lot["price"] is None:
            continue
        take = min(lot["remaining"], rem)
        lot["remaining"] = round(lot["remaining"] - take, 6)
        rem = round(rem - take, 6)
        total_cost += take * lot["price"]
        total_qty  += take
    return round(total_cost / total_qty, 4) if total_qty > 1e-6 else None


async def _fetch_rebalance_prices(basket: str, date_str: str,
                                  new_codes: list, sell_codes: list) -> None:
    """Background: fetch OHLC on rebalance date; store in buy/sellOHLC;
    compute FIFO (partial sell) or weighted-avg (wholly sold) cost basis."""
    try:
        bp_data      = _load_buy_price_data()
        basket_bp    = bp_data.get(basket, {})
        portfolios   = _load_portfolios()
        basket_stocks = portfolios.get(basket, [])
        sold         = portfolios.get(f"{basket}_sold", [])
        stk_map      = {s["nseCode"]: s for s in basket_stocks}
        pf_changed   = False
        bp_changed   = False

        rebalance_ts = _date_to_ts(date_str)

        # Buy price + sector for newly added stocks
        for code in new_codes:
            price, buy_fallback = await _fetch_ohlc_avg(code, date_str)
            if price:
                det = basket_bp.get(code)
                if det is not None:
                    ohlc = det.setdefault("buyOHLC", {})
                    if date_str not in ohlc:
                        ohlc[date_str] = price
                        bp_changed = True
                    if buy_fallback:
                        det.setdefault("buyOHLC_fallback", {})[date_str] = buy_fallback
                        bp_changed = True
                if code in stk_map and not stk_map[code].get("buyPrice"):
                    stk_map[code]["buyPrice"] = price
                    pf_changed = True

            # Fetch sector if missing or still at default "Equity"
            det = basket_bp.get(code)
            if det is not None:
                current_seg = (det.get("segment") or "").strip()
                if current_seg.lower() in ("", "equity"):
                    sector = await _fetch_sector(code)
                    if sector:
                        det["segment"] = sector
                        bp_changed = True

        # Also fill missing segment for ALL stocks in the basket, not just new ones
        for code, det in basket_bp.items():
            if code in new_codes:
                continue  # already handled above
            current_seg = (det.get("segment") or "").strip()
            if current_seg.lower() in ("", "equity"):
                sector = await _fetch_sector(code)
                if sector:
                    det["segment"] = sector
                    bp_changed = True

        # Sell events: fetch sell OHLC; compute cost basis
        for code in sell_codes:
            sell_price, sell_fallback = await _fetch_ohlc_avg(code, date_str)
            buy_price  = None

            det = basket_bp.get(code)
            if det is not None:
                if sell_price:
                    sohlc = det.setdefault("sellOHLC", {})
                    if date_str not in sohlc:
                        sohlc[date_str] = sell_price
                        bp_changed = True
                    if sell_fallback:
                        det.setdefault("sellOHLC_fallback", {})[date_str] = sell_fallback
                        bp_changed = True

                all_buy  = _parse_buy_events(det.get("buyEvents")  or "")
                all_sell = _parse_buy_events(det.get("sellEvents") or "")
                buy_ohlc = det.get("buyOHLC") or {}

                # Only consider events at or before the rebalance date for buy;
                # strictly before for prior sells (exclude this event)
                prior_buys  = [(d, q) for d, q in all_buy
                               if _date_to_ts(d) <= rebalance_ts]
                prior_sells = [(d, q) for d, q in all_sell
                               if _date_to_ts(d) < rebalance_ts]
                series_buys = _current_series_buy_events(prior_buys, prior_sells)

                is_wholly_sold = code not in stk_map
                if is_wholly_sold:
                    buy_price = _wavg_cost_basis(series_buys, buy_ohlc)
                else:
                    sell_qty = next((q for d, q in all_sell if d == date_str), None)
                    if sell_qty is not None:
                        buy_price = _fifo_cost_basis(series_buys, prior_sells, sell_qty, buy_ohlc)

            for ev in sold:
                if ev["nseCode"] == code and ev["date"] == date_str:
                    if sell_price and ev.get("sellPrice") is None:
                        ev["sellPrice"] = sell_price
                        pf_changed = True
                    if buy_price and ev.get("buyPrice") is None:
                        ev["buyPrice"] = buy_price
                        pf_changed = True

        if pf_changed:
            portfolios[basket] = basket_stocks
            portfolios[f"{basket}_sold"] = sold
            _save_portfolios(portfolios)
        if bp_changed:
            _save_buy_price_data(bp_data)
    except Exception:
        pass


@app.post("/api/trigger-rebalance")
async def trigger_rebalance(background_tasks: BackgroundTasks, basket: str = Form(...)):
    if basket not in BASKET_DISPLAY_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown basket: {basket}")
    background_tasks.add_task(_recalc_basket_buy_prices, basket)
    background_tasks.add_task(_refresh_gains_file)
    return {"ok": True, "message": f"Rebalance triggered for {BASKET_DISPLAY_NAMES[basket]}"}


_DATE_RE = re.compile(
    r'\b(\d{1,2})[\/\-\s](\w{3,9})[\/\-\s](\d{4})\b'
    r'|\b(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})\b'
    r'|\b(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})\b',
    re.IGNORECASE,
)
_MON = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
        "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
        "january":1,"february":2,"march":3,"april":4,"june":6,
        "july":7,"august":8,"september":9,"october":10,"november":11,"december":12}

def _parse_date_value(val) -> str | None:
    """Convert any cell value / string to 'DD Mon YYYY', or None."""
    if val is None:
        return None
    if isinstance(val, (datetime, _date)):
        try:
            return val.strftime("%d %b %Y")
        except Exception:
            return None
    s = str(val).strip()
    # try standard strptime formats first
    for fmt in ("%d %b %Y", "%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y",
                "%Y-%m-%d", "%d %B %Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%d %b %Y")
        except ValueError:
            pass
    # regex scan — picks up dates embedded in strings like "Rebalance as on 15 May 2026"
    for m in _DATE_RE.finditer(s):
        g = m.groups()
        try:
            if g[0]:   # DD Mon YYYY
                mon = _MON.get(g[1].lower()[:3])
                if mon:
                    return datetime(int(g[2]), mon, int(g[0])).strftime("%d %b %Y")
            elif g[3]: # YYYY-MM-DD
                return datetime(int(g[3]), int(g[4]), int(g[5])).strftime("%d %b %Y")
            elif g[6]: # DD-MM-YYYY
                return datetime(int(g[8]), int(g[7]), int(g[6])).strftime("%d %b %Y")
        except ValueError:
            continue
    return None


def _extract_rebalance_date(wb, filename: str = "") -> str | None:
    """Find the rebalance date from an openpyxl workbook: sheet name → first 6 rows → filename."""
    # 1. Sheet name
    for name in wb.sheetnames:
        d = _parse_date_value(name)
        if d:
            return d
    # 2. First 6 rows of first sheet
    sheet = wb.worksheets[0]
    for row in sheet.iter_rows(min_row=1, max_row=6, values_only=True):
        for cell in row:
            d = _parse_date_value(cell)
            if d:
                return d
    # 3. Filename
    return _parse_date_value(filename)


_REBALANCE_ALLOWED = {"jay.chaudhari@niveshaay.com", "nukul.madaan@niveshaay.com"}

@app.post("/api/upload-rebalance")
async def upload_rebalance(
    request: Request,
    background_tasks: BackgroundTasks,
    basket: str = Form(...),
    file: UploadFile = File(...),
):
    user_email = request.headers.get("X-User-Email", "")
    if user_email and user_email not in _REBALANCE_ALLOWED:
        raise HTTPException(status_code=403, detail="You do not have permission to upload rebalance files.")
    if basket not in BASKET_DISPLAY_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown basket: {basket}")

    raw = await file.read()
    fname = (file.filename or "").lower()

    date_str: str | None = None
    new_stocks: list = []

    if fname.endswith((".xlsx", ".xls")):
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)

        # Target "Historical Constituents" sheet; fall back to sheet 2, then sheet 1
        const_sheet = next(
            (ws for ws in wb.worksheets if "constituent" in (ws.title or "").lower()),
            wb.worksheets[1] if len(wb.worksheets) > 1 else wb.worksheets[0]
        )

        all_rows = list(const_sheet.iter_rows(values_only=True))
        wb.close()

        if not all_rows:
            raise HTTPException(400, "Historical Constituents sheet is empty.")

        # Find header row (contains 'date', 'constituent', or 'weight')
        hdr_idx = 0
        for i, row in enumerate(all_rows[:6]):
            if any("date" in str(c).lower() or "constituent" in str(c).lower() or "weight" in str(c).lower()
                   for c in row if c is not None):
                hdr_idx = i
                break

        headers = [str(c).strip().lower() if c is not None else "" for c in all_rows[hdr_idx]]

        # Column indices: A=date, B=constituent, C=weight (by header keyword, fallback to position)
        date_col   = next((i for i, h in enumerate(headers) if "date" in h), 0)
        const_col  = next((i for i, h in enumerate(headers)
                           if any(k in h for k in ("constituent", "nse", "symbol", "ticker"))), 1)
        weight_col = next((i for i, h in enumerate(headers) if "weight" in h), 2)

        # Parse rows; carry forward last seen date to handle merged cells
        date_buckets: dict[str, list] = {}
        cur_date: str | None = None
        for row in all_rows[hdr_idx + 1:]:
            if all(c is None for c in row):
                continue

            date_val = row[date_col] if len(row) > date_col else None
            if date_val is not None:
                # Date Range format: 'YYYY-MM-DD to YYYY-MM-DD' — split on ' to ', take start date
                raw_s = str(date_val).strip()
                parts = re.split(r'\s+to\s+', raw_s, flags=re.IGNORECASE)
                d = _parse_excel_date(parts[0].strip()) or _parse_date_value(parts[0].strip())
                if d:
                    cur_date = d

            if not cur_date:
                continue

            const_val  = row[const_col]  if len(row) > const_col  else None
            weight_val = row[weight_col] if len(row) > weight_col else None

            # Column B is the company name (full name like "Zen Technologies Ltd")
            name = str(const_val).strip() if const_val is not None else ""
            if not name or name.lower() in ("constituents", "nse code", "symbol", "ticker", "name", "none"):
                continue

            try:
                weight = float(str(weight_val).strip().rstrip("%")) if weight_val is not None else 0.0
            except (ValueError, AttributeError):
                continue
            if weight <= 0:
                continue

            date_buckets.setdefault(cur_date, []).append((name, weight))

        if not date_buckets:
            raise HTTPException(400, "No valid stock data found in Historical Constituents sheet. "
                                     "Expected columns: Date Range, Constituents, Weightage")

        # --- Multi-date processing: all dates chronologically with their original dates ---
        all_dates_sorted = sorted(date_buckets.keys(), key=lambda d: _date_to_ts(d))

        rh_pre         = _load_rebalance_history()
        existing_dates = {e.get("date", "").strip() for e in rh_pre.get(basket, [])}
        new_dates      = [d for d in all_dates_sorted if d not in existing_dates]

        if not new_dates:
            return {"duplicate": True, "message": "File has been already uploaded"}

        pf_data       = _load_portfolios()
        curr_stocks_l = pf_data.get(basket, [])
        nse_sym_list  = _nse_symbols_cache or await _fetch_nse_symbols()

        # Build name → NSE code reverse map.
        # Sources (in priority order): buy_price_data securityName, then rebalance history.
        # Shorter codes always win to avoid full-name fallbacks (e.g. "SJS" beats "SJS ENTERPRISES LTD").
        def _build_name_map(pairs: list[tuple[str, str]]) -> dict[str, str]:
            m: dict[str, str] = {}
            for sn, cd in pairs:
                if sn and cd:
                    k = _norm_name(sn)
                    if not m.get(k) or len(cd) < len(m[k]):
                        m[k] = cd
            return m

        bp_lookup   = _load_buy_price_data().get(basket, {})
        rh_lookup   = _load_rebalance_history().get(basket, [])
        history_name_map: dict[str, str] = _build_name_map(
            [(det.get("securityName", ""), code) for code, det in bp_lookup.items()]
            + [(e.get("securityName", ""), e.get("nseCode", "")) for e in rh_lookup]
        )

        # Build resolved stock list per new date
        date_stock_map: dict[str, list] = {}
        for d in new_dates:
            raw_entries = date_buckets[d]
            weight_sum  = sum(w for _, w in raw_entries)
            scale       = 100.0 if weight_sum <= 2.0 else 1.0
            stocks_d    = []
            for name, weight in raw_entries:
                nse = (history_name_map.get(_norm_name(name))
                       or _resolve_nse(name, curr_stocks_l, nse_sym_list)
                       or name.upper())
                stocks_d.append({
                    "nseCode": nse, "securityName": name, "segment": "Equity",
                    "weight": round(weight * scale, 4), "date": d,
                })
            if stocks_d:
                date_stock_map[d] = stocks_d

        if not date_stock_map:
            raise HTTPException(400, "No valid stocks found after NSE resolution.")

        _auto_save_rollback()
        _push_undo_snapshot(basket, f"before rebalance {new_dates[-1]}")

        # Only track stocks present in the LATEST date range (current portfolio).
        # Stocks that existed in older ranges but not the latest are completely ignored.
        latest_new = new_dates[-1]
        current_codes = {s["nseCode"] for s in date_stock_map.get(latest_new, [])}

        # Per-date lookup: code → stock entry
        date_snaps = {d: {s["nseCode"]: s for s in date_stock_map[d]}
                      for d in new_dates if d in date_stock_map}

        # Load existing history to know prior weights for each current-portfolio stock
        rh          = _load_rebalance_history()
        bh          = rh.get(basket, [])
        by_date_h: dict = {}
        for e in bh:
            by_date_h.setdefault(e.get("date", ""), []).append(e)
        latest_existing = max(by_date_h, key=lambda d: _date_to_ts(d), default=None)
        existing_weights = (
            {e["nseCode"]: float(e.get("weight", 0))
             for e in by_date_h.get(latest_existing, [])}
            if latest_existing else {}
        )

        portfolios  = _load_portfolios()
        basket_stks = portfolios.get(basket, [])
        stk_map     = {s["nseCode"]: s for s in basket_stks}
        bp_data     = _load_buy_price_data()
        basket_bp   = bp_data.setdefault(basket, {})

        all_summary_rows: list[dict]   = []
        bg_added_per_date: dict        = {d: [] for d in new_dates}
        bg_sold_per_date:  dict        = {d: [] for d in new_dates}  # sell codes per date

        # ── Stock-centric loop: trace each current-portfolio stock through history ──
        for code in current_codes:
            prev_w = existing_weights.get(code, 0.0)

            for cur_date in new_dates:
                day_snap = date_snaps.get(cur_date, {})
                if code not in day_snap:
                    # Stock absent from this date range; reset so next appearance = fresh buy
                    prev_w = 0.0
                    continue

                s      = day_snap[code]
                new_w  = s["weight"]
                det_sn = s.get("securityName", "")
                det_sg = s.get("segment", "Equity")

                # If prev_w was reset to 0 because the stock was absent from an
                # intermediate Excel date range (not a true first entry), recover
                # the actual prior weight from rebalance history so we store the
                # delta instead of the full cumulative weight.
                if prev_w == 0 and code in basket_bp and basket_bp[code].get("buyEvents"):
                    det_bp = basket_bp[code]
                    # Respect series boundary: only look at entries AFTER the last
                    # prevSellEvents date (so true re-entries after full exits still
                    # get treated as first buys in the new series).
                    prev_sell_lines = [
                        ln.strip().split(" * ")[0].strip()
                        for ln in (det_bp.get("prevSellEvents") or "").strip().split("\n")
                        if " * " in ln.strip()
                    ]
                    boundary_ts = max((_date_to_ts(d) for d in prev_sell_lines), default=0)
                    cur_ts      = _date_to_ts(cur_date)
                    recovered_w = max(
                        (float(e.get("weight", 0))
                         for e in bh
                         if e.get("nseCode") == code
                         and boundary_ts < _date_to_ts(e.get("date", "")) < cur_ts),
                        default=0.0,
                    )
                    if recovered_w > 0:
                        prev_w = recovered_w

                if prev_w == 0:
                    # True first appearance (new stock, no prior history in this series)
                    _add_event(basket_bp, code, "buyEvents", cur_date, new_w)
                    det = basket_bp[code]
                    if not det.get("securityName"):
                        det["securityName"] = det_sn
                    if not det.get("segment"):
                        det["segment"] = det_sg
                    all_summary_rows.append({
                        "nseCode": code, "securityName": det_sn,
                        "date": cur_date, "prevWeight": 0,
                        "newWeight": round(new_w, 2), "action": "Added",
                    })
                    bg_added_per_date[cur_date].append(code)

                elif new_w > prev_w + 0.01:
                    delta = round(new_w - prev_w, 4)
                    _add_event(basket_bp, code, "buyEvents", cur_date, delta)
                    all_summary_rows.append({
                        "nseCode": code, "securityName": det_sn,
                        "date": cur_date, "prevWeight": round(prev_w, 2),
                        "newWeight": round(new_w, 2), "action": "Increased",
                    })
                    bg_added_per_date[cur_date].append(code)

                elif new_w < prev_w - 0.01:
                    delta = round(prev_w - new_w, 4)
                    _add_event(basket_bp, code, "sellEvents", cur_date, delta)
                    all_summary_rows.append({
                        "nseCode": code, "securityName": det_sn,
                        "date": cur_date, "prevWeight": round(prev_w, 2),
                        "newWeight": round(new_w, 2), "action": "Decreased",
                    })
                    bg_sold_per_date[cur_date].append(code)

                prev_w = new_w

            # Ensure portfolio entry reflects current allocation
            latest_s = date_snaps.get(latest_new, {}).get(code)
            if latest_s:
                alloc = round(latest_s["weight"] / 100, 6)
                if code in stk_map:
                    stk_map[code]["allocation"] = alloc
                else:
                    entry = {"nseCode": code, "allocation": alloc, "buyPrice": None}
                    basket_stks.append(entry)
                    stk_map[code] = entry

        # Add to rebalance history — save ALL stocks at every new date so the date
        # is always tracked in existing_dates on the next upload.
        for d in new_dates:
            for s in date_stock_map.get(d, []):
                rh.setdefault(basket, []).append(s)

        # Record full exits: stocks that had weight before this upload but are no longer
        # in the portfolio (not in current_codes). Write a sell event for their remaining weight.
        for code, prev_weight in existing_weights.items():
            if prev_weight <= 0 or code in current_codes:
                continue  # still active or was already at 0
            det = basket_bp.get(code)
            if det is None:
                continue
            all_buys  = _parse_buy_events(det.get("buyEvents")  or "")
            all_sells = _parse_buy_events(det.get("sellEvents") or "")
            total_bought = sum(q for _, q in all_buys)
            total_sold   = sum(q for _, q in all_sells)
            remaining    = round(total_bought - total_sold, 4)
            if remaining > 0.01:
                _add_event(basket_bp, code, "sellEvents", latest_new, remaining)
                bg_sold_per_date[latest_new].append(code)
                all_summary_rows.append({
                    "nseCode": code,
                    "securityName": det.get("securityName", ""),
                    "date": latest_new, "prevWeight": round(prev_weight, 2),
                    "newWeight": 0, "action": "Removed",
                })

        # Persist
        _save_rebalance_history(rh)
        portfolios[basket] = basket_stks
        # Rebuild sold list from the updated buy/sell events so new sell events
        # appear in the P&L / Sold Stocks tab immediately after upload.
        old_sold = portfolios.get(f"{basket}_sold", [])
        portfolios[f"{basket}_sold"] = _rebuild_sold_from_bp(basket_bp, old_sold)
        _save_portfolios(portfolios)
        _save_buy_price_data(bp_data)

        for d in new_dates:
            has_buys  = bool(bg_added_per_date.get(d))
            has_sells = bool(bg_sold_per_date.get(d))
            if has_buys or has_sells:
                background_tasks.add_task(_fetch_rebalance_prices, basket, d,
                                          bg_added_per_date.get(d, []),
                                          bg_sold_per_date.get(d, []))
        background_tasks.add_task(_recalc_basket_buy_prices, basket)
        background_tasks.add_task(_refresh_gains_file)

        return {
            "ok": True,
            "basket": BASKET_DISPLAY_NAMES[basket],
            "date": latest_new,
            "datesProcessed": new_dates,
            "stockCount": len(current_codes),
            "summary": all_summary_rows,
        }

    else:
        # CSV fallback: date from filename
        date_str = _parse_date_value(file.filename or "")
        if not date_str:
            raise HTTPException(400, "Could not find rebalance date. Include the date in the CSV filename.")

        def _parse_rebalance_row(row: dict) -> dict | None:
            nse = (row.get("NSE Code") or row.get("nseCode") or row.get("NSE") or
                   row.get("Symbol") or row.get("Ticker") or "")
            nse = nse.strip().upper() if isinstance(nse, str) else str(nse).strip().upper()
            if not nse or nse in ("NONE", "NSE CODE", "TICKER", "SYMBOL"):
                return None
            name = str(row.get("Security Name") or row.get("Name") or "").strip()
            w_raw = (row.get("Weightage (%)") or row.get("Weightage") or row.get("Weight (%)") or
                     row.get("Weight") or row.get("weight") or row.get("Allocation (%)") or 0)
            try:
                weight = float(str(w_raw).strip().rstrip("%"))
            except (ValueError, AttributeError):
                return None
            return {"nseCode": nse, "securityName": name, "segment": "Equity", "weight": weight}

        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            parsed = _parse_rebalance_row(dict(row))
            if parsed:
                parsed["date"] = date_str
                new_stocks.append(parsed)

    # Duplicate check (after date is known)
    rh = _load_rebalance_history()
    existing_dates = {e.get("date", "").strip() for e in rh.get(basket, [])}
    if date_str in existing_dates:
        return {"duplicate": True, "message": "File has been already uploaded"}

    # Snapshot before applying rebalance changes
    _auto_save_rollback()
    _push_undo_snapshot(basket, f"before rebalance {date_str}")

    if not new_stocks:
        raise HTTPException(status_code=400, detail="No valid stocks found. "
                            "Expected columns: NSE Code, Security Name, Weightage (%), Segment")

    # Latest snapshot for diff
    basket_history = rh.get(basket, [])
    by_date: dict = {}
    for e in basket_history:
        by_date.setdefault(e.get("date", ""), []).append(e)

    latest_date = max(by_date, key=lambda d: _date_to_ts(d), default=None)
    prev_snap   = {e["nseCode"]: e for e in by_date.get(latest_date, [])} if latest_date else {}
    new_snap    = {s["nseCode"]: s for s in new_stocks}

    # Compute diff
    added     = [c for c in new_snap if c not in prev_snap]
    removed   = [c for c in prev_snap if c not in new_snap]
    increased = []
    decreased = []
    for code in new_snap:
        if code in prev_snap:
            old_w = float(prev_snap[code].get("weight", 0))
            new_w = new_snap[code]["weight"]
            if new_w > old_w + 0.01:
                increased.append({"nseCode": code, "from": old_w, "to": new_w})
            elif new_w < old_w - 0.01:
                decreased.append({"nseCode": code, "from": old_w, "to": new_w})

    # 1. Persist to rebalance_history.json
    rh.setdefault(basket, []).extend(new_stocks)
    _save_rebalance_history(rh)

    # 2. Update portfolios.json
    portfolios = _load_portfolios()
    basket_stocks = portfolios.get(basket, [])
    stk_map = {s["nseCode"]: s for s in basket_stocks}
    sold = portfolios.get(f"{basket}_sold", [])

    # New stocks
    for code in added:
        s = new_snap[code]
        if code in stk_map:
            stk_map[code]["allocation"] = round(s["weight"] / 100, 6)
        else:
            entry = {"nseCode": code, "allocation": round(s["weight"] / 100, 6), "buyPrice": None}
            basket_stocks.append(entry)
            stk_map[code] = entry

    # Weight changes for existing stocks
    for item in increased + decreased:
        code = item["nseCode"]
        if code in stk_map:
            stk_map[code]["allocation"] = round(new_snap[code]["weight"] / 100, 6)

    # Wholly removed → sell event (buyPrice computed in background via FIFO/wavg)
    for code in removed:
        old = prev_snap[code]
        sold.append({
            "nseCode": code,
            "securityName": old.get("securityName", ""),
            "date": date_str,
            "action": "Wholly Sold",
            "weightSold": round(float(old.get("weight", 0)), 2),
            "buyPrice": None,
            "sellPrice": None,
        })
        basket_stocks = [s for s in basket_stocks if s["nseCode"] != code]
        stk_map.pop(code, None)

    # Partial reductions → sell event
    for item in decreased:
        code = item["nseCode"]
        old = prev_snap[code]
        sold.append({
            "nseCode": code,
            "securityName": old.get("securityName", ""),
            "date": date_str,
            "action": "Partial Sell",
            "weightSold": round(item["from"] - item["to"], 2),
            "buyPrice": None,
            "sellPrice": None,
        })

    portfolios[basket] = basket_stocks
    portfolios[f"{basket}_sold"] = sold
    _save_portfolios(portfolios)

    # 3b. Update buy_price_data.json with buy/sell event text entries
    bp_data   = _load_buy_price_data()
    basket_bp = bp_data.setdefault(basket, {})

    for code in added:
        s = new_snap[code]
        _add_event(basket_bp, code, "buyEvents", date_str, s["weight"])
        det = basket_bp[code]
        if not det.get("securityName"):
            det["securityName"] = s.get("securityName", "")
        if not det.get("segment"):
            det["segment"] = s.get("segment", "Equity")

    for item in increased:
        delta = round(item["to"] - item["from"], 4)
        _add_event(basket_bp, item["nseCode"], "buyEvents", date_str, delta)

    for code in removed:
        old_w = float(prev_snap[code].get("weight", 0))
        _add_event(basket_bp, code, "sellEvents", date_str, old_w)

    for item in decreased:
        delta = round(item["from"] - item["to"], 4)
        _add_event(basket_bp, item["nseCode"], "sellEvents", date_str, delta)

    _save_buy_price_data(bp_data)

    # 4. Background: fetch OHLC prices, recalc buy prices, refresh gains
    sell_codes = removed + [i["nseCode"] for i in decreased]
    background_tasks.add_task(_fetch_rebalance_prices, basket, date_str, added, sell_codes)
    background_tasks.add_task(_recalc_basket_buy_prices, basket)
    background_tasks.add_task(_refresh_gains_file)

    # Build structured summary (only changed stocks, weights in % form)
    summary_rows = []
    for code in added:
        s = new_snap[code]
        summary_rows.append({"nseCode": code, "securityName": s.get("securityName", ""),
                              "prevWeight": 0, "newWeight": round(float(s["weight"]), 2), "action": "Added"})
    for code in removed:
        old = prev_snap[code]
        prev_w = float(old.get("weight", 0))
        summary_rows.append({"nseCode": code, "securityName": old.get("securityName", ""),
                              "prevWeight": round(prev_w, 2), "newWeight": 0, "action": "Removed"})
    for item in increased:
        code = item["nseCode"]
        summary_rows.append({"nseCode": code, "securityName": new_snap[code].get("securityName", ""),
                              "prevWeight": round(item["from"], 2), "newWeight": round(item["to"], 2), "action": "Increased"})
    for item in decreased:
        code = item["nseCode"]
        summary_rows.append({"nseCode": code, "securityName": new_snap[code].get("securityName", ""),
                              "prevWeight": round(item["from"], 2), "newWeight": round(item["to"], 2), "action": "Decreased"})

    return {
        "ok": True,
        "basket": BASKET_DISPLAY_NAMES[basket],
        "date": date_str,
        "stockCount": len(new_stocks),
        "summary": summary_rows,
    }


@app.post("/api/preview-rebalance")
async def preview_rebalance(
    basket: str = Form(...),
    file: UploadFile = File(...),
):
    """Parse Excel Sheet 2 and return slide1 + slide2 preview WITHOUT writing anything."""
    if basket not in BASKET_DISPLAY_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown basket: {basket}")

    raw = await file.read()
    fname = (file.filename or "").lower()
    if not fname.endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Only .xlsx/.xls files are supported for rebalance preview.")

    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    const_sheet = next(
        (ws for ws in wb.worksheets if "constituent" in (ws.title or "").lower()),
        wb.worksheets[1] if len(wb.worksheets) > 1 else wb.worksheets[0],
    )
    all_rows = list(const_sheet.iter_rows(values_only=True))
    wb.close()

    if not all_rows:
        raise HTTPException(400, "Historical Constituents sheet is empty.")

    hdr_idx = 0
    for i, row in enumerate(all_rows[:6]):
        if any("date" in str(c).lower() or "constituent" in str(c).lower() or "weight" in str(c).lower()
               for c in row if c is not None):
            hdr_idx = i
            break

    headers    = [str(c).strip().lower() if c is not None else "" for c in all_rows[hdr_idx]]
    date_col   = next((i for i, h in enumerate(headers) if "date" in h), 0)
    const_col  = next((i for i, h in enumerate(headers)
                       if any(k in h for k in ("constituent", "nse", "symbol", "ticker"))), 1)
    weight_col = next((i for i, h in enumerate(headers) if "weight" in h), 2)

    date_buckets: dict[str, list] = {}
    cur_date: str | None = None
    for row in all_rows[hdr_idx + 1:]:
        if all(c is None for c in row):
            continue
        date_val = row[date_col] if len(row) > date_col else None
        if date_val is not None:
            raw_s = str(date_val).strip()
            parts = re.split(r'\s+to\s+', raw_s, flags=re.IGNORECASE)
            d = _parse_excel_date(parts[0].strip()) or _parse_date_value(parts[0].strip())
            if d:
                cur_date = d
        if not cur_date:
            continue
        const_val  = row[const_col]  if len(row) > const_col  else None
        weight_val = row[weight_col] if len(row) > weight_col else None
        name = str(const_val).strip() if const_val is not None else ""
        if not name or name.lower() in ("constituents", "nse code", "symbol", "ticker", "name", "none"):
            continue
        try:
            weight = float(str(weight_val).strip().rstrip("%")) if weight_val is not None else 0.0
        except (ValueError, AttributeError):
            continue
        if weight <= 0:
            continue
        date_buckets.setdefault(cur_date, []).append((name, weight))

    if not date_buckets:
        raise HTTPException(400, "No valid stock data found in Historical Constituents sheet.")

    all_dates_sorted = sorted(date_buckets.keys(), key=lambda d: _date_to_ts(d))
    rh_pre         = _load_rebalance_history()
    existing_dates = {e.get("date", "").strip() for e in rh_pre.get(basket, [])}
    new_dates      = [d for d in all_dates_sorted if d not in existing_dates]

    if not new_dates:
        latest_in_file = all_dates_sorted[-1] if all_dates_sorted else "unknown"
        latest_existing = max(existing_dates, key=lambda d: _date_to_ts(d)) if existing_dates else "none"
        return {
            "duplicate": True,
            "message": (
                f"No new dates found in this file. "
                f"Latest date detected in file: {latest_in_file}. "
                f"Latest date already in system: {latest_existing}. "
                f"Please ensure the new rebalance date has been added to the Excel file before uploading."
            ),
        }

    # Build name → NSE code reverse map
    bp_lookup = _load_buy_price_data().get(basket, {})
    rh_lookup = rh_pre.get(basket, [])
    history_name_map: dict[str, str] = {}
    for code, det in bp_lookup.items():
        sn = det.get("securityName", "")
        if sn and code:
            k = _norm_name(sn)
            if not history_name_map.get(k) or len(code) < len(history_name_map[k]):
                history_name_map[k] = code
    for e in rh_lookup:
        sn, code = e.get("securityName", ""), e.get("nseCode", "")
        if sn and code:
            k = _norm_name(sn)
            if not history_name_map.get(k) or len(code) < len(history_name_map[k]):
                history_name_map[k] = code

    pf_data       = _load_portfolios()
    curr_stocks_l = pf_data.get(basket, [])
    nse_sym_list  = _nse_symbols_cache or await _fetch_nse_symbols()

    date_stock_map: dict[str, list] = {}
    for d in new_dates:
        raw_entries = date_buckets[d]
        weight_sum  = sum(w for _, w in raw_entries)
        scale       = 100.0 if weight_sum <= 2.0 else 1.0
        stocks_d: list = []
        for name, weight in raw_entries:
            nse = (history_name_map.get(_norm_name(name))
                   or _resolve_nse(name, curr_stocks_l, nse_sym_list)
                   or name.upper())
            stocks_d.append({
                "nseCode": nse, "securityName": name, "segment": "Equity",
                "weight": round(weight * scale, 4), "date": d,
            })
        if stocks_d:
            date_stock_map[d] = stocks_d

    if not date_stock_map:
        raise HTTPException(400, "No valid stocks found after name resolution.")

    latest_new    = new_dates[-1]
    current_codes = {s["nseCode"] for s in date_stock_map.get(latest_new, [])}
    date_snaps    = {d: {s["nseCode"]: s for s in date_stock_map[d]}
                     for d in new_dates if d in date_stock_map}

    bh = rh_pre.get(basket, [])
    by_date_h: dict = {}
    for e in bh:
        by_date_h.setdefault(e.get("date", ""), []).append(e)
    latest_existing = max(by_date_h, key=lambda d: _date_to_ts(d), default=None)
    existing_weights = (
        {e["nseCode"]: float(e.get("weight", 0)) for e in by_date_h.get(latest_existing, [])}
        if latest_existing else {}
    )

    # Supplement existing_weights with current portfolio holdings.
    # Old rebalance_history entries may be incomplete (written with a prior code bug that
    # filtered by current_codes). Any stock currently in the portfolio but absent from
    # rebalance_history would be misclassified as "New Addition" on the next upload,
    # generating a duplicate buy event. The live portfolio is the authoritative baseline.
    for s in curr_stocks_l:
        code  = s["nseCode"]
        alloc = s.get("allocation") or 0
        if code not in existing_weights and alloc > 0:
            existing_weights[code] = round(float(alloc) * 100, 4)

    basket_bp_curr = _load_buy_price_data().get(basket, {})

    # Only generate buy/sell events for dates that come AFTER the latest existing baseline.
    # New dates that are chronologically earlier than latest_existing are "historical gap"
    # dates (present in the Excel but missing from the DB). Processing them from the
    # latest_existing weights would incorrectly flag long-absent stocks as exits/re-entries.
    # They are still saved to rebalance_history (so they won't be reprocessed next time)
    # but no new events are emitted for them.
    if latest_existing:
        event_dates = [d for d in new_dates if _date_to_ts(d) > _date_to_ts(latest_existing)]
    else:
        event_dates = list(new_dates)

    codes_to_process: set[str] = set(existing_weights.keys())
    for d in event_dates:
        for s in date_stock_map.get(d, []):
            codes_to_process.add(s["nseCode"])

    historical_events: list[dict] = []
    latest_events:     list[dict] = []

    for code in codes_to_process:
        prev_w   = existing_weights.get(code, 0.0)
        last_sn  = basket_bp_curr.get(code, {}).get("securityName", code)
        last_seg = basket_bp_curr.get(code, {}).get("segment", "Equity")

        for cur_date in event_dates:
            day_snap = date_snaps.get(cur_date, {})
            is_lat   = (cur_date == latest_new)
            tgt      = latest_events if is_lat else historical_events

            if code not in day_snap:
                # Stock absent from this block → it exited here
                if prev_w > 0.01:
                    tgt.append({"nseCode": code, "securityName": last_sn, "segment": last_seg,
                                "eventType": "sell", "date": cur_date, "delta": round(prev_w, 4),
                                "newWeight": 0.0, "isSeriesReset": True})
                prev_w = 0.0
                continue

            s       = day_snap[code]
            new_w   = s["weight"]
            last_sn  = s["securityName"]
            last_seg = s["segment"]

            if prev_w < 0.01:
                tgt.append({"nseCode": code, "securityName": last_sn, "segment": last_seg,
                            "eventType": "buy", "date": cur_date, "delta": round(new_w, 4),
                            "newWeight": round(new_w, 4), "isSeriesReset": False})
            elif new_w > prev_w + 0.01:
                tgt.append({"nseCode": code, "securityName": last_sn, "segment": last_seg,
                            "eventType": "buy", "date": cur_date, "delta": round(new_w - prev_w, 4),
                            "newWeight": round(new_w, 4), "isSeriesReset": False})
            elif new_w < prev_w - 0.01:
                tgt.append({"nseCode": code, "securityName": last_sn, "segment": last_seg,
                            "eventType": "sell", "date": cur_date, "delta": round(prev_w - new_w, 4),
                            "newWeight": round(new_w, 4), "isSeriesReset": False})

            prev_w = new_w

    # ── Slide 2: compare latest block vs the IMMEDIATELY PRECEDING block ──
    # Use event_dates (not new_dates) so that zombie dates — old dates missing from
    # rebalance_history due to the previous current_codes filter — don't corrupt the
    # baseline. If event_dates has >1 entry we compare the latest against the
    # second-to-last event date (correct for a multi-date first upload). If only one
    # event date exists, compare against existing_weights (the confirmed prior state).
    if len(event_dates) > 1:
        prev_snap_s2 = date_snaps.get(event_dates[-2], {})
        prev_w_s2    = {c: s["weight"] for c, s in prev_snap_s2.items()}
    else:
        prev_snap_s2 = {}
        prev_w_s2    = existing_weights

    wholly_sold_s2 = set(prev_w_s2.keys()) - current_codes

    slide2: list[dict] = []
    for s in date_stock_map.get(latest_new, []):
        code   = s["nseCode"]
        prev_w = prev_w_s2.get(code, 0.0)
        new_w  = s["weight"]
        if prev_w < 0.01:
            ut, delta = "New Addition", round(new_w, 4)
        elif new_w > prev_w + 0.01:
            ut, delta = "Partial Add", round(new_w - prev_w, 4)
        elif new_w < prev_w - 0.01:
            ut, delta = "Partial Sell", round(prev_w - new_w, 4)
        else:
            ut, delta = "No Change", 0.0
        chg = (f"+{round(new_w - prev_w, 2)}%" if new_w > prev_w + 0.01
               else f"-{round(prev_w - new_w, 2)}%" if prev_w > new_w + 0.01
               else "No change")
        slide2.append({"nseCode": code, "stockName": s["securityName"], "segment": s["segment"],
                        "prevWeight": round(prev_w, 2), "newWeight": round(new_w, 2),
                        "delta": delta, "change": chg, "updateType": ut, "eventDate": latest_new})

    for code in wholly_sold_s2:
        prev_w     = prev_w_s2.get(code, 0.0)
        snap_entry = prev_snap_s2.get(code, {})
        sn  = snap_entry.get("securityName") or basket_bp_curr.get(code, {}).get("securityName", code)
        seg = snap_entry.get("segment")      or basket_bp_curr.get(code, {}).get("segment", "Equity")
        slide2.append({"nseCode": code, "stockName": sn, "segment": seg,
                        "prevWeight": round(prev_w, 2), "newWeight": 0.0,
                        "delta": round(prev_w, 4), "change": "Removed", "updateType": "Wholly Sell",
                        "eventDate": latest_new})

    # Slide 1: date discrepancies — compare Excel date vs existing last event date (±7 days)
    slide1: list[dict] = []
    def _last_event_date(det: dict, evt_type: str) -> str | None:
        field = "buyEvents" if evt_type == "buy" else "sellEvents"
        lines = [l.strip() for l in (det.get(field) or "").strip().split("\n") if " * " in l]
        if not lines:
            return None
        try:
            return lines[-1].split(" * ")[0].strip()
        except Exception:
            return None

    for evt in latest_events:
        code      = evt["nseCode"]
        excel_dt  = evt["date"]
        exist_dt  = _last_event_date(basket_bp_curr.get(code, {}), evt["eventType"])
        if not exist_dt or exist_dt == excel_dt:
            continue
        try:
            diff = abs((datetime.strptime(exist_dt, "%d %b %Y") -
                        datetime.strptime(excel_dt,  "%d %b %Y")).days)
        except Exception:
            continue
        if 0 < diff <= 7:
            slide1.append({"nseCode": code, "stockName": evt["securityName"],
                           "eventType": "Buy" if evt["eventType"] == "buy" else "Sell",
                           "existingDate": exist_dt, "newDate": excel_dt, "diffDays": diff})

    # History entries (for rebalance_history.json on confirm)
    # Save ALL stocks at every new date so that every date is tracked in existing_dates
    # on the next upload — prevents zombie dates from reappearing as "new".
    history_entries: list[dict] = []
    for d in new_dates:
        for s in date_stock_map.get(d, []):
            history_entries.append({"nseCode": s["nseCode"], "securityName": s["securityName"],
                                    "segment": s["segment"], "weight": round(s["weight"], 4), "date": d})

    return {
        "duplicate": False,
        "basketKey": basket,
        "basket": BASKET_DISPLAY_NAMES[basket],
        "newDates": new_dates,
        "latestDate": latest_new,
        "slide1": slide1,
        "slide2": slide2,
        "historicalEvents": historical_events,
        "historyEntries": history_entries,
    }


@app.post("/api/confirm-rebalance")
async def confirm_rebalance(
    background_tasks: BackgroundTasks,
    request: Request,
):
    """Apply confirmed rebalance changes from the 2-slide preview modal."""
    body          = await request.json()
    basket        = body.get("basket", "")
    latest_date   = body.get("latestDate", "")
    slide2        = body.get("slide2", [])        # confirmed (possibly edited) latest-block rows
    hist_events   = body.get("historicalEvents", [])   # pre-computed older-block events
    hist_entries  = body.get("historyEntries", [])     # rows for rebalance_history.json

    if basket not in BASKET_DISPLAY_NAMES:
        raise HTTPException(400, f"Unknown basket: {basket}")
    if not slide2:
        raise HTTPException(400, "No confirmed changes provided.")

    _auto_save_rollback()
    _push_undo_snapshot(basket, f"before rebalance {latest_date}")

    bp_data   = _load_buy_price_data()
    basket_bp = bp_data.setdefault(basket, {})

    portfolios  = _load_portfolios()
    basket_stks = portfolios.get(basket, [])
    stk_map     = {s["nseCode"]: s for s in basket_stks}

    # Maps event_date → lists of buy/sell codes (for OHLC fetch)
    new_buys_by_date:  dict[str, list[str]] = {}
    new_sells_by_date: dict[str, list[str]] = {}

    def _apply_event(code: str, evt_type: str, date: str, delta: float,
                     sec_name: str = "", segment: str = "Equity",
                     series_reset: bool = False):
        _add_event(basket_bp, code, f"{evt_type}Events", date, delta)
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

    # 1. Apply all historical events to buy_price_data only (no portfolio changes here)
    for evt in hist_events:
        _apply_event(evt["nseCode"], evt["eventType"], evt["date"], evt["delta"],
                     evt.get("securityName", ""), evt.get("segment", "Equity"),
                     evt.get("isSeriesReset", False))
        if evt["eventType"] == "buy" and evt.get("newWeight", 0) > 0:
            new_buys_by_date.setdefault(evt["date"], []).append(evt["nseCode"])
        elif evt["eventType"] == "sell":
            new_sells_by_date.setdefault(evt["date"], []).append(evt["nseCode"])

    # 2. Apply latest-block buy/sell events to buy_price_data
    for row in slide2:
        code       = row["nseCode"]
        ut         = row["updateType"]
        event_date = row.get("eventDate", latest_date)
        new_weight = float(row.get("newWeight", 0))
        prev_weight = float(row.get("prevWeight", 0))
        delta      = float(row.get("delta", abs(new_weight - prev_weight)))
        sec_name   = row.get("stockName", "")
        segment    = row.get("segment", "Equity")

        if ut == "No Change":
            pass  # no event, handled in portfolio rebuild below
        elif ut in ("New Addition", "Partial Add"):
            _apply_event(code, "buy", event_date, delta, sec_name, segment)
            if ut == "New Addition":
                new_buys_by_date.setdefault(event_date, []).append(code)
        elif ut == "Partial Sell":
            _apply_event(code, "sell", event_date, delta, sec_name, segment)
            new_sells_by_date.setdefault(event_date, []).append(code)
        elif ut == "Wholly Sell":
            _apply_event(code, "sell", event_date, delta, sec_name, segment, series_reset=True)
            new_sells_by_date.setdefault(event_date, []).append(code)

    # 3. Rebuild portfolio from slide2 — the latest block is the authoritative composition.
    # Preserve all existing entry fields (buyPrice, live data, etc.) for stocks already in portfolio.
    # Stocks absent from slide2 (exited in historical blocks) are naturally excluded.
    new_basket_stks: list[dict] = []
    for row in slide2:
        code  = row["nseCode"]
        ut    = row.get("updateType", "")
        new_w = float(row.get("newWeight", 0))
        if ut == "Wholly Sell" or new_w <= 0:
            continue
        alloc          = round(new_w / 100, 6)
        existing_entry = stk_map.get(code)
        if existing_entry:
            new_basket_stks.append({**existing_entry, "allocation": alloc})
        else:
            new_basket_stks.append({"nseCode": code, "allocation": alloc, "buyPrice": None})

    # 4. Persist rebalance history
    rh = _load_rebalance_history()
    existing_dates = {e.get("date", "").strip() for e in rh.get(basket, [])}
    for entry in hist_entries:
        if entry.get("date", "") not in existing_dates:
            rh.setdefault(basket, []).append(entry)
    _save_rebalance_history(rh)

    # 5. Rebuild sold records from the now-updated event log (authoritative)
    old_sold    = portfolios.get(f"{basket}_sold", [])
    sold_stocks = _rebuild_sold_from_bp(basket_bp, old_sold)

    # 6. Persist portfolios (active + sold) and buy_price_data
    portfolios[basket] = new_basket_stks
    portfolios[f"{basket}_sold"] = sold_stocks
    _save_portfolios(portfolios)
    _save_buy_price_data(bp_data)

    # 7. Background tasks — fetch OHLC for both buy and sell events
    all_evt_dates = sorted(set(list(new_buys_by_date.keys()) + list(new_sells_by_date.keys())))
    for evt_date in all_evt_dates:
        b_codes = new_buys_by_date.get(evt_date, [])
        s_codes = new_sells_by_date.get(evt_date, [])
        if b_codes or s_codes:
            background_tasks.add_task(_fetch_rebalance_prices, basket, evt_date, b_codes, s_codes)
    background_tasks.add_task(_recalc_basket_buy_prices, basket)
    # Backfill fills any missing sell OHLC across all baskets and regenerates gains as final step
    background_tasks.add_task(_backfill_all_sell_ohlc_bg)

    return {
        "ok": True,
        "basket": BASKET_DISPLAY_NAMES[basket],
        "date": latest_date,
        "stocksProcessed": len(slide2),
    }


def _parse_excel_date(val) -> str | None:
    """Convert openpyxl cell value (datetime, date, or string) → 'DD Mon YYYY'."""
    if val is None:
        return None
    if isinstance(val, (datetime,)):
        return val.strftime("%d %b %Y")
    try:
        from datetime import date as _date
        if isinstance(val, _date):
            return val.strftime("%d %b %Y")
    except Exception:
        pass
    s = str(val).strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%d-%b-%Y",
                "%d %B %Y", "%d-%B-%Y", "%d/%b/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%d %b %Y")
        except ValueError:
            pass
    return None


def _resolve_nse_code(stock_name: str, basket_key: str, bp_data: dict) -> str:
    """Best-effort mapping of a stock name → NSE code.
    Priority: exact code match in basket → securityName match in basket → use as-is."""
    name = stock_name.strip()
    basket_bp = bp_data.get(basket_key, {})

    # 1. Exact NSE code match (case-insensitive)
    upper = name.upper()
    if upper in basket_bp:
        return upper
    for code in basket_bp:
        if code.upper() == upper:
            return code

    # 2. securityName match (case-insensitive)
    lower = name.lower()
    for code, det in basket_bp.items():
        sn = (det.get("securityName") or "").lower()
        if sn and (sn == lower or sn.startswith(lower) or lower.startswith(sn)):
            return code

    # 3. Fall back: treat value as NSE code
    return upper


@app.post("/api/upload-historical-excel")
async def upload_historical_excel(
    background_tasks: BackgroundTasks,
    basket: str = Form(...),
    file: UploadFile = File(...),
):
    """Upload an Excel workbook whose 'Historical Constituents' sheet (col A: Date,
    col B: Stock Name, col C: Weight %) contains rebalance history.
    Only dates AFTER the last stored rebalance date are processed."""

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


@app.post("/api/rebuild-sold/{basket}")
async def rebuild_sold_endpoint(basket: str, background_tasks: BackgroundTasks):
    """Rebuild sold-stock records from buy/sell event log. Fixes wrong weights, actions,
    sell prices, and duplicates caused by earlier code paths."""
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


def _add_event(basket_bp: dict, code: str, field: str, date_str: str, qty: float) -> None:
    """Append 'DD Mon YYYY * qty' to buyEvents or sellEvents for a stock.
    Deduplicates by date across BOTH the current field and its prev* counterpart,
    so a series reset (which moves events to prevBuyEvents/prevSellEvents and clears
    the current field) does not allow the same date to be re-added."""
    if code not in basket_bp:
        basket_bp[code] = {"securityName": "", "segment": "",
                            "buyEvents": "", "sellEvents": "",
                            "buyOHLC": {}, "sellOHLC": {},
                            "prevBuyEvents": "", "prevSellEvents": ""}
    det = basket_bp[code]
    existing  = det.get(field) or ""
    prev_field = "prevBuyEvents" if field == "buyEvents" else "prevSellEvents"
    prev_existing = det.get(prev_field) or ""

    def _dates_in(s: str) -> set:
        return {
            line.strip().split(" * ")[0].strip()
            for line in s.strip().split("\n")
            if " * " in line.strip()
        }

    # Skip if this date already exists in either current or previous series
    if date_str.strip() in (_dates_in(existing) | _dates_in(prev_existing)):
        return
    new_line = f"{date_str} * {round(qty, 4):g}"
    det[field] = (existing.strip() + "\n" + new_line).strip()


@app.post("/api/upload-portfolio-report")
async def upload_portfolio_report(
    background_tasks: BackgroundTasks,
    basket: str = Form(...),
    date: str = Form(...),
    file: UploadFile = File(...),
):
    if basket not in BASKET_DISPLAY_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown basket: {basket}")

    try:
        rebalance_dt = datetime.strptime(date.strip(), "%d %b %Y")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date. Use format: 15 Jan 2025")
    date_str = rebalance_dt.strftime("%d %b %Y")

    # Duplicate check
    rh = _load_rebalance_history()
    existing_dates = {e.get("date", "").strip() for e in rh.get(basket, [])}
    if date_str in existing_dates:
        return {"duplicate": True, "message": "Report for this date has already been uploaded"}

    # Parse PDF
    raw = await file.read()
    try:
        pdf_entries = _parse_portfolio_pdf(raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read PDF: {e}")

    if not pdf_entries:
        raise HTTPException(status_code=400, detail="No rebalance data found in PDF")

    # Resolve NSE codes — load portfolio + NSE symbols for matching
    portfolios    = _load_portfolios()
    curr_stocks   = portfolios.get(basket, [])
    nse_symbols   = _nse_symbols_cache  # use cached list (populated by /api/nse-symbols)
    if not nse_symbols:
        nse_symbols = await _fetch_nse_symbols()

    stk_map  = {s["nseCode"]: s for s in curr_stocks}
    sold     = portfolios.get(f"{basket}_sold", [])

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
            rh.setdefault(basket, []).append({
                "date": date_str, "nseCode": nse,
                "securityName": entry["companyName"],
                "segment": entry["holdingType"], "weight": w,
            })

        elif section == "removal":
            old = prev_snap.get(nse) or stk_map.get(nse, {})
            sold.append({
                "nseCode": nse,
                "securityName": entry["companyName"],
                "date": date_str, "action": "Wholly Sold",
                "weightSold": round(float(old.get("weight", old.get("allocation", 0)) or 0) *
                                    (1 if float(old.get("weight", 1) or 1) <= 1 else 0.01), 2),
                "buyPrice": stk_map[nse].get("buyPrice") if nse in stk_map else None,
                "sellPrice": None,
            })
            curr_stocks = [s for s in curr_stocks if s["nseCode"] != nse]
            stk_map.pop(nse, None)
            removed_codes.append(nse)
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
            rh.setdefault(basket, []).append({
                "date": date_str, "nseCode": nse,
                "securityName": entry["companyName"],
                "segment": entry["holdingType"], "weight": w,
            })

    portfolios[basket]             = curr_stocks
    portfolios[f"{basket}_sold"]   = sold
    _save_portfolios(portfolios)
    _save_rebalance_history(rh)

    sell_codes = removed_codes + [i["nseCode"] for i in decreased_items]
    background_tasks.add_task(_fetch_rebalance_prices, basket, date_str, added_codes, sell_codes)

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


# ─────────────────────────────────────────────────────────────────────────────
# Rollback points — full system snapshots the user can manually create/restore
# ─────────────────────────────────────────────────────────────────────────────

def _load_rollback_points() -> list:
    try:
        if _ROLLBACK_FILE.exists():
            return json.loads(_ROLLBACK_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []

def _save_rollback_points(points: list) -> None:
    _ROLLBACK_FILE.write_text(json.dumps(points, indent=2, ensure_ascii=False), encoding="utf-8")


@app.get("/api/rollback-points")
async def list_rollback_points():
    points = _load_rollback_points()
    return [{"id": p["id"], "label": p["label"], "createdAt": p["createdAt"]} for p in points]


@app.post("/api/rollback-points")
async def create_rollback_point(body: dict = Body(...)):
    label = (body.get("label") or "").strip() or time.strftime("%d %b %Y %H:%M")
    point_id = str(int(time.time() * 1000))
    point = {
        "id":               point_id,
        "label":            label,
        "createdAt":        time.strftime("%d %b %Y %H:%M"),
        "portfolios":       json.loads(_PORTFOLIOS_FILE.read_text(encoding="utf-8")),
        "buyPriceData":     json.loads(_BUY_PRICE_FILE.read_text(encoding="utf-8")),
        "rebalanceHistory": json.loads(_RH_FILE.read_text(encoding="utf-8")),
    }
    points = _load_rollback_points()
    points.append(point)
    _save_rollback_points(points[-_MAX_ROLLBACK_PTS:])
    return {"ok": True, "id": point_id, "label": label, "createdAt": point["createdAt"]}


@app.post("/api/rollback-points/{point_id}/restore")
async def restore_rollback_point(point_id: str, background_tasks: BackgroundTasks):
    points = _load_rollback_points()
    point = next((p for p in points if p["id"] == point_id), None)
    if not point:
        raise HTTPException(404, "Rollback point not found.")
    _PORTFOLIOS_FILE.write_text(
        json.dumps(point["portfolios"], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _BUY_PRICE_FILE.write_text(
        json.dumps(point["buyPriceData"], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _RH_FILE.write_text(
        json.dumps(point["rebalanceHistory"], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    background_tasks.add_task(_refresh_gains_file)
    return {"ok": True, "label": point["label"], "createdAt": point["createdAt"]}


@app.delete("/api/rollback-points/{point_id}")
async def delete_rollback_point(point_id: str):
    points = _load_rollback_points()
    new_points = [p for p in points if p["id"] != point_id]
    if len(new_points) == len(points):
        raise HTTPException(404, "Rollback point not found.")
    _save_rollback_points(new_points)
    return {"ok": True}


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
