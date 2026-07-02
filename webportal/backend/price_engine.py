"""Multi-source live price fetching: Yahoo Finance chart API, NSE bhavcopy,
Screener.in HTML scrape, Google Finance scrape, market-cap/PE pipeline, and
portfolio PDF parsing. Also owns the in-memory live/mc-pe/nse-symbols caches
since they're only ever read/written by the functions in this module.

_nse_symbols_cache is read directly (via `import price_engine` + module attribute
access, not `from price_engine import _nse_symbols_cache`, since it's reassigned
here) by rebalance.py and portfolio_report.py.
"""
import asyncio
import csv
import io
import json
import re
import time
import urllib.parse
from typing import Optional

import httpx
from fastapi import APIRouter, File, UploadFile
from pypdf import PdfReader

from config import YF_HEADERS, YF_SYMBOL_MAP, LIVE_TTL
from persistence import BASKET_DISPLAY_NAMES, _all_nse_codes

router = APIRouter()

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


_live_refresh_task = None   # asyncio.Task | None — background Yahoo refresh


async def _refresh_live_cache() -> None:
    """Background task: refresh Yahoo price data and update _live_cache in-place."""
    global _live_cache, _live_cache_ts, _mc_pe_task_running, _live_refresh_task
    try:
        codes = _all_nse_codes()
        mc_pe_cold = not _mc_pe_cache or (time.time() - _mc_pe_cache_ts) >= _MC_PE_TTL
        try:
            yahoo_data = await _fetch_yahoo_charts(codes)
        except Exception:
            yahoo_data = {}
        if mc_pe_cold and not _mc_pe_task_running:
            asyncio.create_task(_mc_pe_background_refresh())
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
        async with _live_cache_lock:
            _live_cache    = data
            _live_cache_ts = time.time()
    finally:
        _live_refresh_task = None


async def fetch_live_batch() -> dict:
    """
    Stale-while-revalidate: serve the cached result immediately if it exists,
    and kick off a background refresh whenever the TTL has expired.
    - Cold start (no cache): awaits the refresh directly (~5-10s Yahoo fetch).
    - Warm cache (within TTL): returns instantly, no background work needed.
    - Stale cache (TTL expired): returns stale data NOW, refreshes in background.
    """
    global _live_refresh_task

    async with _live_cache_lock:
        age = time.time() - _live_cache_ts
        cache_warm = bool(_live_cache) and age < LIVE_TTL
        cache_stale = bool(_live_cache) and age >= LIVE_TTL

    if cache_warm:
        return _live_cache

    if cache_stale:
        # Serve stale immediately; start background refresh if not already running
        if _live_refresh_task is None or _live_refresh_task.done():
            _live_refresh_task = asyncio.create_task(_refresh_live_cache())
        return _live_cache

    # Cache is empty (cold start) — must wait for the first fetch
    if _live_refresh_task is None or _live_refresh_task.done():
        _live_refresh_task = asyncio.create_task(_refresh_live_cache())
    await _live_refresh_task
    return _live_cache


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

@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/api/baskets")
async def get_baskets():
    return BASKET_DISPLAY_NAMES


@router.post("/api/debug/pdf-text")
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


@router.get("/api/debug/mcpe")
async def debug_mcpe():
    return {
        "mc_pe_cache_size": len(_mc_pe_cache),
        "mc_pe_task_running": _mc_pe_task_running,
        "mc_pe_cache_age_s": round(time.time() - _mc_pe_cache_ts, 1) if _mc_pe_cache_ts else None,
        "live_cache_size": len(_live_cache),
        "live_cache_age_s": round(time.time() - _live_cache_ts, 1) if _live_cache_ts else None,
        "sample": {k: _mc_pe_cache[k] for k in list(_mc_pe_cache.keys())[:5]} if _mc_pe_cache else {},
    }


@router.get("/api/nse-symbols")
async def get_nse_symbols():
    """Return full NSE equity symbol list for autocomplete (cached 24 h)."""
    return await _fetch_nse_symbols()

