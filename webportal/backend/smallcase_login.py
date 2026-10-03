"""Admin-only smallcase login automation: lets an admin authenticate to
smallcase.com with just a phone number + OTP (typed into our own UI), so we
can then pull real daily index values straight from smallcase instead of
manual entry.

Not a raw API replay -- the OTP-send step requires a reCAPTCHA token that
smallcase's own page JS generates, so we drive their actual page (fill the
phone field, click their real "Get OTP" button) rather than calling their
auth endpoints directly.

IMPORTANT -- this module only actually launches a browser when the current
process's working directory is this file's own directory (webportal/backend).
run.py starts TWO processes that both load this module: the standalone
webportal on :8001 (cwd=webportal/backend, correct) and the main app on
:8000 (cwd=backend, wrong -- webportal is merely *mounted* into it at /wp).
Playwright's browser launch depends on the process's cwd in some way that
isn't under this module's control (root-caused empirically: identical code,
same profile, same async/sync API, failed 100% of the time on :8000 with
"Target page, context or browser has been closed" on the very first launch
attempt, while working every single time on :8001 or in a bare standalone
script run from this directory). Rather than mutate a shared process's cwd
(risky -- other concurrent requests in the same event loop could be mid-await
relying on it), every public function here checks which process it's in and
transparently proxies to the :8001 process over plain HTTP when it's the
wrong one. The :8001 process proxies to itself... except it doesn't, because
its own cwd check is the one that's actually correct, so it just does the
real work -- no loop.
"""
import asyncio
import os
import re
from pathlib import Path

import httpx
from _shared_http import SHARED_SSL_CONTEXT
from playwright.async_api import async_playwright

_PROFILE_DIR = Path(__file__).parent / "smallcase_session_profile"
_LOGIN_URL = "https://www.smallcase.com/smallcase/mid-and-small-cap-focused-portfolio-NIVMO_0001/constituents"
_PROXY_BASE = "http://127.0.0.1:8001/api/admin"


def _should_proxy() -> bool:
    return Path.cwd().resolve() != _PROFILE_DIR.parent.resolve()


async def _proxy(method: str, path: str, auth_header: str | None = None, **kwargs) -> dict:
    # The :8001 process has its own _require_admin(request) check, so the
    # caller's Authorization header must be forwarded -- otherwise every
    # proxied call comes back {"detail": "Admin access required."} (a dict,
    # not an exception, so callers doing result.get("logged_in", False)
    # silently got False instead of the real answer).
    headers = {"Authorization": auth_header} if auth_header else {}
    try:
        async with httpx.AsyncClient(
                verify=SHARED_SSL_CONTEXT,timeout=kwargs.pop("timeout", 60)) as client:
            resp = await client.request(method, f"{_PROXY_BASE}{path}", headers=headers, **kwargs)
            return resp.json()
    except Exception as e:
        # This whole proxy mechanism assumes a second process is really
        # listening on :8001 (true for run.py's local dev setup -- see this
        # module's docstring). If production actually runs as a single
        # merged process (app.mount('/wp', ...) in backend/main.py) with
        # nothing bound to :8001, every one of these calls fails with a
        # connection error that -- uncaught -- propagated all the way to an
        # opaque 500 on EVERY smallcase-login endpoint, confirmed live
        # 2026-10-03. Returning a normal-shaped failure here instead means
        # every caller (login_status's `.get("logged_in", False)`,
        # start/verify/close's `.get("ok")`) degrades to a safe default
        # with the real reason attached, instead of crashing the request.
        return {"ok": False, "logged_in": False, "error": f"Could not reach the automation process: {e}"}

# Module-level Playwright/browser handles, plus a lock so overlapping HTTP
# requests to these endpoints don't interleave calls against the same page.
_lock = asyncio.Lock()
_pw = None
_browser_context = None
_page = None
_login_modal = None  # Locator for the actual login/OTP modal, set in _start_login
                      # and reused in _verify_otp -- smallcase has multiple unrelated
                      # elements matching a generic "Modal-module" class (e.g. a "Download
                      # app" promo popup), so re-searching the whole page for "any modal"
                      # in the verify step picks up the wrong one. Anchoring on the phone
                      # input's own ancestor, once, is what actually identifies the right one.


async def _close_browser_locked():
    global _pw, _browser_context, _page, _login_modal
    try:
        if _browser_context is not None:
            await _browser_context.close()
    except Exception as e:
        print(f"[smallcase_login] close_browser: {e}")
    try:
        if _pw is not None:
            await _pw.stop()
    except Exception as e:
        print(f"[smallcase_login] close_browser (pw.stop): {e}")
    _pw = None
    _browser_context = None
    _page = None
    _login_modal = None


async def close_browser(auth_header: str | None = None) -> dict:
    """Release our hold on the profile directory so an external tool (e.g. a
    manual login window opened directly on the same profile) can use it
    without hitting a 'profile already in use' lock conflict. The next call
    that needs the browser just relaunches against whatever session state is
    on disk at that point."""
    if _should_proxy():
        return await _proxy("POST", "/smallcase-login/close-browser", auth_header=auth_header)
    async with _lock:
        await _close_browser_locked()
    return {"ok": True, "message": "Browser released."}


async def _ensure_page():
    global _pw, _browser_context, _page
    if _page is not None:
        return _page
    _pw = await async_playwright().start()
    _browser_context = await _pw.chromium.launch_persistent_context(
        str(_PROFILE_DIR), headless=True, viewport={"width": 1400, "height": 1000},
    )
    _page = _browser_context.pages[0] if _browser_context.pages else await _browser_context.new_page()
    return _page


async def _debug_dump(tag: str):
    """On any verify-step failure, save a screenshot + full page HTML to
    fixed paths next to this file, so a failure can be diagnosed from what
    actually happened instead of guessing again."""
    try:
        page = await _ensure_page()
        await page.screenshot(path=str(Path(__file__).parent / f"smallcase_debug_{tag}.png"))
        content = await page.content()
        (Path(__file__).parent / f"smallcase_debug_{tag}.html").write_text(content, encoding="utf-8")
    except Exception as e:
        print(f"[smallcase_login] debug dump failed: {e}")


async def _is_logged_in() -> bool:
    """Check session validity by reading the actual rendered page, not by
    calling smallcase's own session-check API directly. That API needs a
    CSRF header their page's own JS attaches automatically -- a bare
    page.request.get() doesn't carry it, so it always came back 401
    regardless of real login state (confirmed: it returned "Unauthenticated"
    before login and "Invalid csrf token" after, never a real success). The
    page's own rendered content is what's actually reliable here."""
    try:
        page = await _ensure_page()
        # "networkidle" here used to time out intermittently (confirmed live,
        # 2026-09-30: 3 consecutive failures) -- smallcase's page apparently
        # keeps some persistent connection open (analytics/chat widget?) that
        # never lets the network go fully idle. "load" is what every other
        # navigation in this module already uses and never showed this
        # flakiness, and the page's rendered text is what we actually read
        # below anyway -- there was never a real need to wait for network
        # silence, just for the DOM to be there.
        await page.goto(_LOGIN_URL, wait_until="load", timeout=20000)
        await page.wait_for_timeout(1500)
        body = await page.inner_text("body")
        return "Subscribed" in body  # distinct word from "Subscribers Only" (the logged-out label)
    except Exception as e:
        print(f"[smallcase_login] login check exception: {e}")
        return False


async def _start_login_locked(phone: str) -> dict:
    global _login_modal
    page = await _ensure_page()
    await page.goto(_LOGIN_URL, wait_until="load", timeout=30000)
    await page.wait_for_timeout(1500)

    # Already have a valid session (e.g. from a previous login, or the profile
    # was seeded from another machine) -- there's no "Login" button to click
    # in that state, so treat it as success instead of failing to find one.
    body = await page.inner_text("body")
    if "Subscribed" in body:
        return {"ok": True, "already_logged_in": True, "message": "Already logged in to smallcase."}

    login_btn = page.locator("button:has-text('Login')").first
    if await login_btn.count() == 0:
        return {"ok": False, "error": "Could not find a Login button on the smallcase page."}
    await login_btn.click(timeout=5000)
    await page.wait_for_timeout(1000)

    # smallcase changed this input's type from "number" to "tel" at some
    # point after this was first built (confirmed 2026-09-19 -- the old
    # type='number'+placeholder selector silently stopped matching anything).
    # data-testid is far less likely to shift on a future redesign than type
    # or placeholder text, so prefer it and keep the old selector as a
    # fallback rather than a hard break if it ever changes again.
    phone_input = page.locator("input[data-testid='test-login-phone-number-input']").first
    if await phone_input.count() == 0:
        phone_input = page.locator("input[placeholder='Your phone number']").first
    if await phone_input.count() == 0:
        await _debug_dump("phone_input_not_found")
        return {"ok": False, "error": "Could not find the phone number field."}
    await phone_input.fill(phone)

    # Identify the login modal itself via the phone input's own ancestor --
    # the page has other unrelated "Modal-module"-classed popups (e.g. a
    # "Download app" promo), so a bare page-wide modal search later would
    # pick up the wrong one. Anchoring on the phone field pins down the
    # right container while it's unambiguous -- but smallcase swaps the
    # modal's *inner content* for the OTP step (the phone input itself gets
    # removed from the DOM), so a locator chained through phone_input stops
    # resolving to anything once that happens. Instead, read the ancestor's
    # actual class string now, while the phone input still exists, and build
    # an independent selector from it that keeps working after the content
    # underneath it changes.
    ancestor = phone_input.locator("xpath=ancestor::*[contains(@class, 'Modal-module')]").first
    class_attr = await ancestor.get_attribute("class") or ""
    modal_token = next((c for c in class_attr.split() if "Modal-module" in c), None)
    if modal_token:
        _login_modal = page.locator(f".{modal_token}").first
    else:
        _login_modal = ancestor

    get_otp_btn = page.locator("button:has-text('Get OTP')").first
    if await get_otp_btn.count() == 0:
        return {"ok": False, "error": "Could not find the 'Get OTP' button."}
    await get_otp_btn.click(timeout=5000)
    await page.wait_for_timeout(2000)

    return {"ok": True, "message": f"OTP requested for {phone}."}


async def _verify_otp_locked(otp: str) -> dict:
    page = await _ensure_page()

    if _login_modal is None:
        return {"ok": False, "error": "No login flow in progress -- click 'Get OTP' first."}
    modal = _login_modal
    if await modal.count() == 0:
        await _debug_dump("modal_not_found")
        return {"ok": False, "error": "Could not find the login modal -- it may have already closed."}

    # The OTP is 4 separate single-digit boxes (data-testid="otp-input-0..3"),
    # not one field. Filling the first one with the full string makes
    # smallcase's own JS auto-distribute a digit into each box (confirmed via
    # debug screenshot: all 4 ended up correctly filled and disabled from a
    # single .fill() on box 0) -- and the whole thing auto-submits once
    # complete, with no separate verify/submit button to click at all.
    otp_input = modal.locator("input[data-testid='otp-input-0']").first
    if await otp_input.count() == 0:
        await _debug_dump("otp_input_not_found")
        try:
            debug_html = (await modal.inner_html())[:2000]
        except Exception:
            debug_html = "(could not read modal html)"
        print(f"[smallcase_login] OTP input not found. Modal HTML snippet:\n{debug_html}")
        return {"ok": False, "error": "Could not find the OTP input field -- selector needs updating."}
    await otp_input.fill(otp)

    # No button to click -- wait for the auto-submit + server round-trip,
    # polling a few times rather than a single fixed sleep.
    for _ in range(6):
        await page.wait_for_timeout(1000)
        if await _is_logged_in():
            return {"ok": True, "message": "Logged in to smallcase successfully."}

    await _debug_dump("login_not_confirmed")
    return {"ok": False, "error": "Login did not complete -- OTP may be wrong or the flow changed."}


async def start_login(phone: str, auth_header: str | None = None) -> dict:
    if _should_proxy():
        return await _proxy("POST", "/smallcase-login/start", auth_header=auth_header, json={"phone": phone})
    async with _lock:
        return await _start_login_locked(phone)


async def verify_otp(otp: str, auth_header: str | None = None) -> dict:
    if _should_proxy():
        return await _proxy("POST", "/smallcase-login/verify", auth_header=auth_header, json={"otp": otp})
    async with _lock:
        return await _verify_otp_locked(otp)


async def login_status(auth_header: str | None = None) -> dict:
    if _should_proxy():
        result = await _proxy("GET", "/smallcase-login/status", auth_header=auth_header)
        return result.get("logged_in", False)
    async with _lock:
        return await _is_logged_in()


# ── Daily value fetch ─────────────────────────────────────────────────────────
# Maps our basket keys to smallcase's own scid + benchmark id. All 7 confirmed
# directly from each smallcase's own page metadata (all share the same
# Nifty Smallcap 100 benchmark).
#
# IMPORTANT -- each basket's "benchmark" column in historical_index.json is
# independently rebased to 100 at THAT basket's own inception date, not a
# single continuous "Nifty Smallcap 100 since 2019" series. A single hardcoded
# rebase divisor here (there used to be one, NIFTY_SMALLCAP_100_BASE = 5430.6,
# the raw index close on 19 Sep 2019) is therefore only correct for whichever
# basket happens to be inception-anchored on that exact date (Mid_Small_Cap) --
# every other basket got a sudden nonsensical jump in its benchmark column the
# first time this ran (confirmed: Green_Energy's benchmark jumped ~243 -> ~367
# overnight on 2026-08-31, corrupting its Alpha % on the Calculate Returns page
# for every date after that). Instead, calibrate a per-basket rebase factor
# from the most recent date this basket already has stored AND that's also
# present in the freshly fetched raw points -- factor = existing_value / raw_value
# at that shared date -- so new points stay continuous with that basket's own
# established series regardless of what its original rebase anchor even was.
BASKET_SMALLCASE_MAP = {
    "Mid_Small_Cap":   {"scid": "NIVMO_0001",  "benchmark_id": ".NIFSMCP100"},
    "Green_Energy":    {"scid": "NIVTR_0001",  "benchmark_id": ".NIFSMCP100"},
    "IPO_Basket":      {"scid": "NIVFMM_0001", "benchmark_id": ".NIFSMCP100"},
    "Trends_Triology": {"scid": "NIVMO_0004",  "benchmark_id": ".NIFSMCP100"},
    "Techstack":       {"scid": "NIVNM_0003",  "benchmark_id": ".NIFSMCP100"},
    "Make_in_India":   {"scid": "NIVNM_0001",  "benchmark_id": ".NIFSMCP100"},
    "Consumer_Trends": {"scid": "NIVNM_0002",  "benchmark_id": ".NIFSMCP100"},
}


def _merge_basket_points(hi: dict, basket: str, port_pts: list, bench_pts: list) -> dict:
    """Shared merge logic for one basket's freshly fetched raw points (however
    they were obtained -- server-side Playwright, or a browser bookmarklet
    posting smallcase's own API response straight from an already-logged-in
    tab) into historical_index.json's existing series for that basket."""
    bench_by_date = {p["date"][:10]: p["price"] for p in bench_pts}

    existing_series = hi.get(basket, {}).get("data", [])
    existing_by_date = {e["date"]: e for e in existing_series}

    # Calibrate this basket's own rebase factor from the most recent date
    # it already has stored that's also in the freshly fetched raw points.
    rebase_factor = None
    for e in sorted(existing_series, key=lambda e: e["date"], reverse=True):
        raw_at_date = bench_by_date.get(e["date"])
        if raw_at_date:
            rebase_factor = e["benchmark"] / raw_at_date
            break
    if rebase_factor is None:
        return {"ok": False, "error": "No overlapping date to calibrate the benchmark rebase from."}

    added = []
    for p in port_pts:
        date = p["date"][:10]
        if date in existing_by_date:
            continue
        bench_raw = bench_by_date.get(date)
        if bench_raw is None:
            continue
        bench_rebased = round(bench_raw * rebase_factor, 4)
        hi.setdefault(basket, {"data": []})
        hi[basket]["data"].append({
            "date": date, "value": round(p["price"], 4), "benchmark": bench_rebased,
        })
        added.append(date)
    if added:
        hi[basket]["data"].sort(key=lambda e: e["date"])
    return {"ok": True, "added_dates": added}


def merge_ingested_payload(payload: dict) -> dict:
    """Entry point for the browser-bookmarklet ingest path (see
    routers add_smallcase_ingest / static/smallcase-bookmarklet.js) -- payload
    shape: {"Green_Energy": {"port_pts": [...], "bench_pts": [...]}, ...},
    each basket's raw points being exactly what smallcase's own
    /sam/graph/performance endpoint returned for that scid/benchmark_id, no
    server-side browser involved at all."""
    from persistence import _load_historical_index, _save_historical_index

    hi = _load_historical_index()
    results = {}
    for basket in BASKET_SMALLCASE_MAP:
        entry = payload.get(basket)
        if not entry:
            results[basket] = {"ok": False, "error": "No data submitted for this basket."}
            continue
        results[basket] = _merge_basket_points(hi, basket, entry.get("port_pts") or [], entry.get("bench_pts") or [])

    if any(r.get("added_dates") for r in results.values()):
        _save_historical_index(hi)
    return {"ok": True, "results": results}


async def _fetch_daily_values_locked() -> dict:
    from persistence import _load_historical_index, _save_historical_index

    page = await _ensure_page()
    if not await _is_logged_in():
        return {"ok": False, "error": "Not logged in to smallcase -- use the login button first."}

    hi = _load_historical_index()
    results = {}
    for basket, cfg in BASKET_SMALLCASE_MAP.items():
        scid = cfg["scid"]
        bench_id = cfg["benchmark_id"]
        url = (f"https://api.smallcase.com/sam/graph/performance?currency=INR&duration=1m"
               f"&scids[]={scid}&stockBenchmarkIds[]={bench_id}")
        resp = await page.request.get(url)
        if resp.status != 200:
            results[basket] = {"ok": False, "error": f"HTTP {resp.status}"}
            continue
        body = await resp.json()
        port_pts = (body.get("data", {}).get(scid) or {}).get("points", [])
        bench_pts = (body.get("data", {}).get(bench_id) or {}).get("points", [])
        results[basket] = _merge_basket_points(hi, basket, port_pts, bench_pts)

    if any(r.get("added_dates") for r in results.values()):
        _save_historical_index(hi)

    return {"ok": True, "results": results}


async def fetch_daily_values(auth_header: str | None = None) -> dict:
    if _should_proxy():
        return await _proxy("POST", "/smallcase-fetch-daily", auth_header=auth_header, timeout=120)
    async with _lock:
        return await _fetch_daily_values_locked()


# ── Rebalance report fetch ────────────────────────────────────────────────────
# smallcase's "Get portfolio report" (Stocks & Weights tab) issues a
# password-protected PDF. price_engine._parse_portfolio_pdf (used by the
# existing, never-actually-wired-to-a-button /api/upload-portfolio-report)
# looked like a match at first glance -- same section vocabulary ("Additions"
# / "Removals" / etc) -- but it is NOT safe to reuse here: confirmed live
# (2026-09-30, Mid & Small Cap) that it corrupted real data. Its section
# state ("current = stype") is set the FIRST time it sees a header substring
# and never cleared until the NEXT header substring -- but smallcase's real
# report repeats those exact words as narrative section titles in a long
# free-text "Constituent-wise Rationale" (paragraphs of investment thesis
# prose) BEFORE the second, real occurrence. Every prose line downstream that
# happens to contain a bare "NN%" (e2695a45's test run hit "...EMS from 2% to
# 6%...") got swallowed as a fake constituent row, with a nonsense "company
# name" and a garbage NSE-code match via _resolve_nse's substring fallback.
# _parse_portfolio_pdf may well be correct for whatever simpler PDF format it
# was originally written against -- it's just wrong for THIS one, so this
# fetch path uses its own parser instead, deliberately reading only ONE
# tightly bounded, single-line-per-stock section: "Latest Rebalance Update"
# through the following "Click here to view sectorwise..." marker. That
# section lists EVERY current holding (not just changed ones) as one line
# each -- "Name Smallcap 3% +3%" -- with the trailing "+/-N%" delta present
# only for stocks that actually changed, which is what turns each line into
# an unambiguous addition/increase/removal/decrease/no_change classification
# without any cross-line state to get confused by.
_REPORT_DATE_RE = re.compile(r"issued on\s*:\s*([A-Za-z]+ \d{1,2}, \d{4})")
_REBALANCE_LINE_RE = re.compile(
    r"^(.+?)\s+(Smallcap|Midcap|Largecap|Multicap)\s+([\d.]+)%(?:\s+([+-][\d.]+)%)?$"
)


def _extract_report_date(text: str) -> str | None:
    m = _REPORT_DATE_RE.search(text)
    if not m:
        return None
    from datetime import datetime as _dt
    try:
        return _dt.strptime(m.group(1), "%B %d, %Y").strftime("%d %b %Y")
    except ValueError:
        return None


def parse_rebalance_update(text: str) -> list[dict]:
    """Parse ONLY the bounded 'Latest Rebalance Update' section (see the
    module comment above for why nothing else in this PDF is safe to read).
    Returns entries shaped like price_engine._parse_portfolio_pdf's output
    (section/companyName/holdingType/newWeight) so portfolio_report.py's
    existing apply logic can consume either one unchanged."""
    lines = text.split("\n")
    try:
        start = next(i for i, l in enumerate(lines) if l.strip().startswith("Latest Rebalance Update"))
    except StopIteration:
        return []
    end = next((i for i in range(start, len(lines)) if "Click here to view sectorwise" in lines[i]), len(lines))

    entries = []
    for line in lines[start + 1:end]:
        m = _REBALANCE_LINE_RE.match(line.strip())
        if not m:
            continue
        name, holding, weight_s, delta_s = m.groups()
        weight = float(weight_s)
        delta = float(delta_s) if delta_s else None

        if delta is None:
            section = "no_change"
        elif weight <= 0.01 and delta < 0:
            section = "removal"
        elif delta > 0 and weight - delta <= 0.01:
            section = "addition"
        elif delta > 0:
            section = "increase"
        else:
            section = "decrease"

        entries.append({
            "section": section, "companyName": name.strip(),
            "holdingType": holding, "newWeight": weight,
            # The report's own delta is ground truth for how much to buy/sell
            # -- our own rebalance_history's "previous weight" can drift out
            # of sync with smallcase's real one (confirmed live: HFCL showed
            # 4.0% in our last recorded snapshot when the report's own delta
            # said the true previous weight was 2.5%), so the caller should
            # prefer this over recomputing from its own history where both
            # are available.
            "delta": abs(delta) if delta is not None else None,
        })
    return entries


async def _fetch_rebalance_report_locked(basket: str) -> dict:
    if basket not in BASKET_SMALLCASE_MAP:
        return {"ok": False, "error": f"Unknown basket: {basket}"}

    page = await _ensure_page()
    if not await _is_logged_in():
        return {"ok": False, "error": "Not logged in to smallcase -- use the login button first."}

    scid = BASKET_SMALLCASE_MAP[basket]["scid"]
    # The slug before the scid is cosmetic -- smallcase redirects to the
    # canonical URL from the scid alone (confirmed live), so a fixed
    # placeholder slug works for every basket without needing its real one.
    await page.goto(f"https://www.smallcase.com/smallcase/x-{scid}/constituents", wait_until="load", timeout=30000)
    await page.wait_for_timeout(2000)

    report_btn = page.locator("text=/Get portfolio report/i").first
    if await report_btn.count() == 0:
        return {"ok": False, "error": "Could not find the 'Get portfolio report' button."}
    await report_btn.click()
    await page.wait_for_timeout(1200)

    view_btn = page.locator("text=/View report/i").first
    if await view_btn.count() == 0:
        return {"ok": False, "error": "Could not find the 'View report' button."}

    download_holder: dict = {}

    async def _save_download(d):
        download_holder["download"] = d

    def _on_new_page(p):
        p.on("download", lambda d: asyncio.create_task(_save_download(d)))

    page.on("download", lambda d: asyncio.create_task(_save_download(d)))
    page.context.on("page", _on_new_page)

    await view_btn.click()
    for _ in range(15):
        await asyncio.sleep(1)
        if "download" in download_holder:
            break
    if "download" not in download_holder:
        return {"ok": False, "error": "Timed out waiting for the portfolio report download."}

    tmp_path = Path(__file__).parent / f"_tmp_rebalance_report_{scid}.pdf"
    try:
        await download_holder["download"].save_as(str(tmp_path))
        raw = tmp_path.read_bytes()
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

    # Peek at the (still-encrypted-for-content, but plaintext-metadata) date
    # line using the same password price_engine._parse_portfolio_pdf uses, so
    # the caller doesn't have to duplicate PDF decryption just to learn the
    # report's own issue date for the existing endpoint's required `date` field.
    date_str = None
    try:
        from pypdf import PdfReader
        import io as _io
        reader = PdfReader(_io.BytesIO(raw))
        if reader.is_encrypted:
            reader.decrypt(os.environ.get("PORTFOLIO_PDF_PASSWORD", ""))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        date_str = _extract_report_date(text)
    except Exception as e:
        print(f"[smallcase_login] could not pre-read report date: {e}")

    import base64
    return {"ok": True, "date": date_str, "pdf_base64": base64.b64encode(raw).decode("ascii")}


async def fetch_rebalance_report(basket: str, auth_header: str | None = None) -> dict:
    if _should_proxy():
        return await _proxy("POST", f"/smallcase-rebalance-report/{basket}", auth_header=auth_header, timeout=120)
    async with _lock:
        return await _fetch_rebalance_report_locked(basket)
