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
from pathlib import Path

import httpx
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
    async with httpx.AsyncClient(timeout=kwargs.pop("timeout", 60)) as client:
        resp = await client.request(method, f"{_PROXY_BASE}{path}", headers=headers, **kwargs)
        return resp.json()

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
        await page.goto(_LOGIN_URL, wait_until="networkidle", timeout=20000)
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

    phone_input = page.locator("input[type='number'][placeholder='Your phone number']").first
    if await phone_input.count() == 0:
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
            results[basket] = {"ok": False, "error": "No overlapping date to calibrate the benchmark rebase from."}
            continue

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
        results[basket] = {"ok": True, "added_dates": added}

    if any(r.get("added_dates") for r in results.values()):
        _save_historical_index(hi)

    return {"ok": True, "results": results}


async def fetch_daily_values(auth_header: str | None = None) -> dict:
    if _should_proxy():
        return await _proxy("POST", "/smallcase-fetch-daily", auth_header=auth_header, timeout=120)
    async with _lock:
        return await _fetch_daily_values_locked()
