"""Dashboard Assistant -- a tool-calling LLM (Groq, with Gemini/OpenRouter
free-tier fallbacks -- see _PROVIDERS) that answers natural-language
questions about what's actually in this dashboard: basket holdings, buy
prices, rebalance history/summary, basket returns/profile/overlap, stock
exposure and cross-basket detail, live prices, benchmarks, watchlist,
corporate actions, result-update tracking, analyst notes/targets, the
asking admin's own simulator portfolio, and competitor data. The widget
itself is admin-only for now, but per an explicit 2026-10-07 instruction
the tool coverage itself deliberately spans EVERY piece of data visible to
an ordinary logged-in member, not just admin-only features -- see the
"Added 2026-10-07" comment block below for the audit this was built from.

Deliberately NOT classic vector-embedding RAG -- almost everything here is
structured JSON (holdings, dates, prices), not long-form prose, so
"retrieval" means calling the same read-only functions/endpoints the
dashboard's own pages already use, not chunking+embedding documents. The
LLM decides which tool(s) answer a given question via OpenAI/Groq-style
function calling -- confirmed working live against openai/gpt-oss-120b
(2026-10-05) before building the rest of this.

Every tool wraps an EXISTING data source already used elsewhere in this
codebase -- none of them duplicate fetch logic. Webportal-held data goes
through the same http://127.0.0.1:8000/wp loopback every other main-app
router already uses (actual_portfolio_bridge.py); it's always safe
regardless of process topology, since it targets the port THIS process is
listening on, not a second process that may not exist (see the
smallcase-login proxy bug fixed earlier the same day -- same lesson
applies: never assume a second process is there to talk to).
"""
import asyncio
import contextvars
import json
import os
from pathlib import Path

import requests as _http
from fastapi import APIRouter, HTTPException, Request

from auth import is_admin_email

router = APIRouter()

_WEBPORTAL = "http://127.0.0.1:8000/wp"
_GROQ_MODEL = "openai/gpt-oss-120b"
_GEMINI_MODEL = "gemini-3.8-flash"
# Confirmed live (2026-10-05) to actually invoke tools correctly -- several
# other ":free" OpenRouter slugs either 404 (deprecated/paid-only now) or
# silently fail, so don't swap this without re-testing tool_calls firsthand.
_OPENROUTER_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
_MAX_TOOL_ROUNDS = 6

# Groq's free tier has a very low per-model TPM cap (8000 for gpt-oss-120b),
# which this assistant has hit live more than once (context_length_exceeded,
# then rate_limit_exceeded). Gemini and OpenRouter's free tiers are far more
# generous and both expose an OpenAI-compatible endpoint, so each is a
# drop-in fallback using the exact same AsyncOpenAI client / tool-calling
# code -- no separate SDK needed. Try Groq first (already proven, fast);
# fall back through the list only when the current provider's own call
# fails for any reason (rate limit, context length, outage, etc).
_PROVIDERS = [
    ("groq",       "GROQ_API_KEY",       "https://api.groq.com/openai/v1",                            _GROQ_MODEL),
    ("gemini",     "GEMINI_API_KEY",     "https://generativelanguage.googleapis.com/v1beta/openai/",  _GEMINI_MODEL),
    ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1",                              _OPENROUTER_MODEL),
]


def _require_admin(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not is_admin_email(user):
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


# ── Tool implementations -- each one is plain synchronous code (requests/file
# I/O), always run via asyncio.to_thread() below so it never blocks the
# event loop, including the self-loopback HTTP calls some of them make. ──────

def _wp_get(path: str, timeout: float = 10.0):
    r = _http.get(f"{_WEBPORTAL}{path}", timeout=timeout)
    r.raise_for_status()
    return r.json()


# Unlike /wp/* (open, no auth), the main app's own /api/* routes are all
# behind JWTMiddleware (main.py) -- a self-loopback call needs a real bearer
# token or gets a 401. The admin asking the assistant a question already has
# one (that's how they got past _require_admin), so ask_assistant() stashes
# their own Authorization header here per-request and every _api_get() call
# forwards it. A contextvar (not a plain module global) because this runs
# per-request under asyncio -- it survives the asyncio.to_thread() hop tool
# impls run in (to_thread copies the current context into the thread).
_current_auth_header: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_auth_header", default=""
)


def _api_get(path: str, timeout: float = 10.0):
    headers = {}
    auth = _current_auth_header.get()
    if auth:
        headers["Authorization"] = auth
    r = _http.get(f"http://127.0.0.1:8000{path}", headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _tool_list_baskets(_args: dict) -> dict:
    return _wp_get("/api/baskets")


def _tool_get_basket_holdings(args: dict) -> dict:
    basket = args["basket"]
    data = _wp_get(f"/api/basket/{basket}")
    stocks = data.get("stocks") or []
    return {
        "basket": basket,
        "holdings": [
            {
                "nseCode": s.get("nseCode"),
                "securityName": s.get("securityName"),
                "allocationPct": round((s.get("allocation") or 0) * 100, 2),
                "buyPrice": s.get("buyPrice"),
            }
            for s in stocks
        ],
        "soldStocks": data.get("soldStocks") or [],
    }


def _tool_get_rebalance_history(args: dict) -> dict:
    return _wp_get(f"/api/admin/basket-rebalance-history/{args['basket']}")


def _tool_get_basket_returns(args: dict) -> dict:
    # Reuses actual_portfolio_bridge.py's own period-return computation
    # directly (same process, same main app) rather than re-deriving it --
    # that's the exact function behind the Actual Portfolio page's own
    # return figures, so this answers with the identical numbers. Not
    # limited to the 5 preset buttons the UI shows -- the underlying data
    # is a daily series, so any "<n><W|D|M|Y>" period works (e.g. "9M",
    # "270D"), same as a custom date range would on the Actual Portfolio page.
    from routers.actual_portfolio_bridge import get_basket_period_returns
    period = (args.get("period") or "1M").strip().upper()
    all_returns = get_basket_period_returns(period=period)
    basket = args.get("basket")
    return {basket: all_returns.get(basket)} if basket else all_returns


def _tool_get_competitor_rebalance_history(args: dict) -> dict:
    # get_competitor_list already returns fullRebalanceHistory/rebalanceTimeline
    # per competitor -- this narrows to the one asked about. fullRebalanceHistory
    # itself is deliberately left out by default: it's a per-stock breakdown of
    # EVERY historical rebalance event and alone can be ~25K+ characters, which
    # blows Groq's 8000 TPM request-size cap on its own. rebalanceTimeline (dates
    # + add/remove counts) and latestRebalanceDetail (current full holding list)
    # cover "what/when was the recent rebalance" questions; pass includeFullHistory
    # only when the user explicitly wants the entire historical breakdown, and
    # even then only the last 3 events are returned.
    data = _wp_get("/api/admin/competitor-list")
    key = (args.get("competitorKey") or "").strip().lower()
    label = (args.get("competitorLabel") or "").strip().lower()
    for c in data.get("competitors") or []:
        if (key and c.get("key") == key) or (label and label in (c.get("label") or "").lower()):
            result = {
                "key": c.get("key"), "label": c.get("label"), "manager": c.get("manager"),
                "rebalanceTimeline": c.get("rebalanceTimeline"),
                "latestRebalanceDetail": c.get("latestRebalanceDetail"),
            }
            if args.get("includeFullHistory"):
                result["fullRebalanceHistory"] = (c.get("fullRebalanceHistory") or [])[-3:]
            return result
    return {"error": "No matching competitor -- call get_competitor_list to see the exact key/label spelling."}


def _tool_get_gains_statement(args: dict) -> dict:
    data = _wp_get("/api/gains-statement")
    basket = args.get("basket")
    return {basket: data.get(basket, {})} if basket else data


def _tool_get_corporate_actions(_args: dict) -> dict:
    return {"corporateActions": _wp_get("/api/corporate-actions")}


def _tool_get_watchlist(_args: dict) -> dict:
    return {"watchlist": _wp_get("/api/watchlist")}


def _tool_get_competitor_list(_args: dict) -> dict:
    # Deliberately slimmed down -- the full payload includes each competitor's
    # stocks/performanceSeries/rebalanceTimeline/fullRebalanceHistory, which
    # is far too large to feed back into the model as a single tool result
    # (blows Groq's context window). This tool is only for resolving a name
    # to a key/label; use get_competitor_rebalance_history for full detail.
    full = _wp_get("/api/admin/competitor-list")
    competitors = full.get("competitors", full) if isinstance(full, dict) else full
    if isinstance(competitors, dict):
        items = competitors.values()
    else:
        items = competitors
    slim = []
    for c in items:
        if not isinstance(c, dict):
            continue
        slim.append({
            "key": c.get("key"),
            "label": c.get("label"),
            "manager": c.get("manager"),
            "cagr": c.get("cagr"),
            "launchDate": c.get("launchDate"),
        })
    return {"competitors": slim}


def _tool_get_result_update_status(args: dict) -> dict:
    import routers.result_updates as ru
    data = ru._load()
    ru._sync_and_compute_reminders(data)  # refreshes currentlyHeld flags in memory only -- not saved
    rows = list(data.values())
    nse_code = (args.get("nseCode") or "").strip().upper()
    basket = args.get("basket")
    if nse_code:
        rows = [r for r in rows if r["nseCode"] == nse_code]
    if basket:
        rows = [r for r in rows if basket in (r.get("baskets") or {})]
    return {"rows": rows}


def _tool_get_results_calendar(_args: dict) -> dict:
    cache_file = Path(__file__).parent.parent / "results_calendar_cache.json"
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
        return {"events": (cache.get("calendar") or {}).get("data") or []}
    except Exception:
        return {"events": []}


# ── Added 2026-10-07 -- per an explicit instruction that the assistant must
# cover EVERYTHING visible to an ordinary logged-in member (not just the
# admin-only data it started with), an audit of every GET route in both
# backend/ and webportal/backend/ found ~45 member-visible endpoints with no
# tool coverage at all. These wrap the genuinely distinct ones (duplicates of
# already-covered data, e.g. the main-app mirror of /wp/api/baskets, are
# skipped). Admin-only/mutating routes (confirmed via _require_admin/
# is_admin_email in each file) are deliberately left uncovered.

def _tool_get_stock_exposure(args: dict) -> dict:
    data = _wp_get("/api/stock-exposure")
    code = (args.get("nseCode") or "").strip().upper()
    if code:
        data = [e for e in data if e.get("code") == code]
    return {"exposure": data}


def _tool_get_stock_cross_basket_detail(args: dict) -> dict:
    return _wp_get(f"/api/stock-detail/{args['nseCode'].strip().upper()}")


def _tool_get_basket_overlap(_args: dict) -> dict:
    return {
        "stockMap": _wp_get("/api/basket-stock-map"),
        "weightMap": _wp_get("/api/basket-weight-map"),
    }


def _tool_get_rebalance_summary(args: dict) -> dict:
    # More complete than get_rebalance_history (which only reads
    # rebalance_history.json) -- this replays every buy/sell event directly,
    # so it also catches manual buy-price-page edits that never touch the
    # history file. Prefer this one when both could answer the question.
    return _wp_get(f"/api/rebalance-summary/{args['basket']}")


def _tool_get_live_price(args: dict) -> dict:
    code = (args.get("nseCode") or "").strip().upper()
    if code:
        return _wp_get(f"/api/live/{code}")
    return {"live": _wp_get("/api/live")}


def _tool_get_stock_performance(args: dict) -> dict:
    codes = args.get("nseCodes") or []
    if not codes:
        return {"error": "nseCodes is required -- a list of one or more NSE codes."}
    return _wp_get(f"/api/performance?codes={','.join(c.strip().upper() for c in codes)}")


def _tool_get_basket_profile(args: dict) -> dict:
    return _wp_get(f"/api/admin/basket-profile/{args['basket']}")


def _tool_get_watchlist_alerts(_args: dict) -> dict:
    return {"alerts": _wp_get("/api/watchlist/alerts")}


def _tool_get_benchmark_comparison(_args: dict) -> dict:
    return {
        "benchmarks": _api_get("/api/benchmarks"),
        "basketsVsInception": _api_get("/api/baskets/comparison"),
    }


def _tool_get_rebalance_alerts(_args: dict) -> dict:
    # Per-user unacknowledged alerts (exits/partial sells/new additions/
    # weight increases since last viewed) -- scoped to whichever admin asked.
    return {"alerts": _api_get("/api/rebalance-alerts")}


def _tool_get_market_snapshot(_args: dict) -> dict:
    return {
        "indices": _api_get("/api/market"),
        "targetStoplossAlerts": _api_get("/api/alerts"),
    }


def _tool_get_analyst_contacts(_args: dict) -> dict:
    return {"contacts": _api_get("/api/results-calendar/analyst-contacts")}


def _tool_get_basket_notes_targets(args: dict) -> dict:
    basket = args["basket"]
    return {
        "notes": _api_get(f"/api/basket-notes/{basket}"),
        "targets": _api_get(f"/api/portfolio/{basket}/targets"),
    }


def _tool_search_stock(args: dict) -> dict:
    query = (args.get("query") or "").strip()
    code = (args.get("nseCode") or "").strip().upper()
    if code:
        return _api_get(f"/api/stocks/info?code={code}")
    if query:
        return {"results": _api_get(f"/api/stocks/search?q={query}")}
    return {"error": "Pass either query (name search) or nseCode (exact lookup)."}


def _tool_get_simulator_portfolio(_args: dict) -> dict:
    # The asking admin's OWN virtual/simulator portfolio (Simulator Portfolio
    # page) -- self-scoped by their own token, not a real-money basket.
    return {
        "holdings": _api_get("/api/simulator"),
        "sips": _api_get("/api/simulator/sips"),
        "settings": _api_get("/api/simulator/settings"),
    }


_TOOL_IMPLS = {
    "list_baskets": _tool_list_baskets,
    "get_basket_holdings": _tool_get_basket_holdings,
    "get_rebalance_history": _tool_get_rebalance_history,
    "get_basket_returns": _tool_get_basket_returns,
    "get_gains_statement": _tool_get_gains_statement,
    "get_corporate_actions": _tool_get_corporate_actions,
    "get_watchlist": _tool_get_watchlist,
    "get_competitor_list": _tool_get_competitor_list,
    "get_competitor_rebalance_history": _tool_get_competitor_rebalance_history,
    "get_result_update_status": _tool_get_result_update_status,
    "get_results_calendar": _tool_get_results_calendar,
    "get_stock_exposure": _tool_get_stock_exposure,
    "get_stock_cross_basket_detail": _tool_get_stock_cross_basket_detail,
    "get_basket_overlap": _tool_get_basket_overlap,
    "get_rebalance_summary": _tool_get_rebalance_summary,
    "get_live_price": _tool_get_live_price,
    "get_stock_performance": _tool_get_stock_performance,
    "get_basket_profile": _tool_get_basket_profile,
    "get_watchlist_alerts": _tool_get_watchlist_alerts,
    "get_benchmark_comparison": _tool_get_benchmark_comparison,
    "get_rebalance_alerts": _tool_get_rebalance_alerts,
    "get_market_snapshot": _tool_get_market_snapshot,
    "get_analyst_contacts": _tool_get_analyst_contacts,
    "get_basket_notes_targets": _tool_get_basket_notes_targets,
    "search_stock": _tool_search_stock,
    "get_simulator_portfolio": _tool_get_simulator_portfolio,
}

_BASKET_KEYS_HINT = "Green_Energy, Mid_Small_Cap, Consumer_Trends, IPO_Basket, Trends_Triology, Techstack, Make_in_India"

_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "list_baskets",
        "description": "Every basket's internal key and display label. Call this first if unsure of a basket's exact key.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_basket_holdings",
        "description": "Current stock holdings of one basket: NSE code, security name, allocation %, buy price. Also includes recently sold stocks.",
        "parameters": {"type": "object", "properties": {
            "basket": {"type": "string", "description": f"Exact basket key, e.g. {_BASKET_KEYS_HINT}"},
        }, "required": ["basket"]},
    }},
    {"type": "function", "function": {
        "name": "get_rebalance_history",
        "description": "Dated rebalance events for one of OUR OWN baskets: stocks added, removed, or reweighted on each date. For a COMPETITOR smallcase's rebalances instead, use get_competitor_rebalance_history.",
        "parameters": {"type": "object", "properties": {
            "basket": {"type": "string", "description": "Exact basket key"},
        }, "required": ["basket"]},
    }},
    {"type": "function", "function": {
        "name": "get_basket_returns",
        "description": "Our own basket's percentage return (and CAGR) over a trailing period, as of today. Not limited to a fixed list -- any lookback window works.",
        "parameters": {"type": "object", "properties": {
            "basket": {"type": "string", "description": "Optional exact basket key -- omit for every basket"},
            "period": {"type": "string", "description": "Trailing window, default 1M. Either a preset (1W/1M/3M/6M/1Y), a generic '<number><W|D|M|Y>' string for any other window (e.g. '9M' for 9 months, '270D' for 270 days, '2Y' for 2 years), or 'MAX' for the full since-inception return -- always use 'MAX' for 'since inception'/'all-time' questions rather than guessing a large year count, since guessing too small silently truncates the result to that guessed date instead of the true launch date."},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_gains_statement",
        "description": "Realized gains from past sells, per stock, for one basket (or every basket if none given).",
        "parameters": {"type": "object", "properties": {
            "basket": {"type": "string", "description": "Optional exact basket key -- omit for every basket"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_corporate_actions",
        "description": "Every tracked corporate action (splits, bonuses, mergers, etc.) across all baskets.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_watchlist",
        "description": "Stocks on the watchlist (not currently held in any basket) with sector/market-cap/price-alert info.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_competitor_list",
        "description": "Every one of the 8 tracked COMPETITOR smallcases (external funds we benchmark against, e.g. GEM-Q Model, Omni AI-Tech Global-AI Theme, Wright Innovation Theme, Caprize Earnings Momentum Portfolio -- NOT our own baskets): key, label, fund manager, current stocks, CAGR, and rebalance history. Call this to see the exact key/label list if a competitor's name is ambiguous.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_competitor_rebalance_history",
        "description": "One COMPETITOR smallcase's rebalance history (not one of our own baskets -- use get_rebalance_history for those). Match by key or a partial/fuzzy label (e.g. 'Omni' matches 'Omni AI-Tech Global-AI Theme').",
        "parameters": {"type": "object", "properties": {
            "competitorKey": {"type": "string"},
            "competitorLabel": {"type": "string", "description": "Partial name is fine, e.g. 'Omni' or 'GEM-Q'"},
            "includeFullHistory": {"type": "boolean", "description": "Only set true if the user explicitly wants the full historical per-stock rebalance breakdown, not just the latest/recent one. Returns just the last 3 events."},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_result_update_status",
        "description": "Quarterly result-update tracking: result/concall dates and received/checked/sent status, per company. Filter by nseCode and/or basket, or omit both for everything.",
        "parameters": {"type": "object", "properties": {
            "nseCode": {"type": "string"},
            "basket": {"type": "string", "description": "Exact basket key"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_results_calendar",
        "description": "Upcoming/recent board-meeting result dates sourced from NSE/yfinance, across every held stock.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_stock_exposure",
        "description": "Combined weight of each stock summed across ALL our baskets (concentration), how many baskets it's in, and its weight in each -- e.g. 'how concentrated is our exposure to X' or 'which stock do we hold the most of overall'. Omit nseCode to rank every held stock.",
        "parameters": {"type": "object", "properties": {
            "nseCode": {"type": "string", "description": "Optional exact NSE code to look up just one stock"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_stock_cross_basket_detail",
        "description": "For ONE specific stock: its current weight, buy price, and buy date in EVERY one of our baskets that holds it right now -- the tool for 'which baskets hold X' or 'what's our buy price for X in each basket'.",
        "parameters": {"type": "object", "properties": {
            "nseCode": {"type": "string", "description": "Exact NSE code"},
        }, "required": ["nseCode"]},
    }},
    {"type": "function", "function": {
        "name": "get_basket_overlap",
        "description": "Which stocks every basket holds (plain list) and at what weight (allocation map) -- for comparing overlap/duplication between our own baskets.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_rebalance_summary",
        "description": "Day-wise rebalance detail for one of our own baskets (added/removed/reweighted per date), derived from the actual buy/sell event log -- more complete than get_rebalance_history since it also catches manual buy-price edits, not just formal rebalance uploads. Prefer this over get_rebalance_history when available.",
        "parameters": {"type": "object", "properties": {
            "basket": {"type": "string", "description": "Exact basket key"},
        }, "required": ["basket"]},
    }},
    {"type": "function", "function": {
        "name": "get_live_price",
        "description": "Live current market price (CMP) and OHLC for one stock, or every tracked stock if nseCode is omitted.",
        "parameters": {"type": "object", "properties": {
            "nseCode": {"type": "string", "description": "Optional exact NSE code -- omit for every tracked stock's live price"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_stock_performance",
        "description": "Multi-tenure (1M/3M/6M/1Y/2Y/3Y/5Y) percentage price performance for one or more specific stocks (not a basket).",
        "parameters": {"type": "object", "properties": {
            "nseCodes": {"type": "array", "items": {"type": "string"}, "description": "One or more exact NSE codes"},
        }, "required": ["nseCodes"]},
    }},
    {"type": "function", "function": {
        "name": "get_basket_profile",
        "description": "One of our own basket's derived profile: sector mix %, market-cap mix % (Large/Mid/Small/Multicap), and most recent rebalance date -- useful for 'what sectors does X basket cover' type questions.",
        "parameters": {"type": "object", "properties": {
            "basket": {"type": "string", "description": "Exact basket key"},
        }, "required": ["basket"]},
    }},
    {"type": "function", "function": {
        "name": "get_watchlist_alerts",
        "description": "Active watchlist alerts -- a watchlist stock whose next-review date has arrived, or whose price target was hit. Different from get_watchlist, which is just the plain company list.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_benchmark_comparison",
        "description": "Nifty 50/200/MidSmall benchmark index returns (1M/6M/1Y/3Y/5Y net+CAGR), plus every one of our baskets' own returns compared against their individual inception dates -- for 'how do we compare to the index' or 'which basket has done best since it launched' type questions.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_rebalance_alerts",
        "description": "Unacknowledged rebalance-impact alerts for the asking admin: exits, partial sells, new additions, or weight increases since they last checked.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_market_snapshot",
        "description": "Current Nifty 50/Sensex/Nifty 200 index levels and day change, plus any cross-basket target-hit or stoploss-hit alerts right now.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_analyst_contacts",
        "description": "Per-basket analyst contact names/emails who receive the day-before-results reminder.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_basket_notes_targets",
        "description": "The free-text analyst note for one basket, plus per-stock target-price/stoploss values set for it.",
        "parameters": {"type": "object", "properties": {
            "basket": {"type": "string", "description": "Exact basket key"},
        }, "required": ["basket"]},
    }},
    {"type": "function", "function": {
        "name": "search_stock",
        "description": "Look up a stock by name (fuzzy search) or get sector/industry info for an exact NSE code. Use this to resolve a company name the user mentioned into its exact NSE code before calling other stock-specific tools.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Partial company name to search, if the exact NSE code isn't known"},
            "nseCode": {"type": "string", "description": "Exact NSE code, if already known, to get sector/industry detail"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_simulator_portfolio",
        "description": "The asking admin's OWN virtual/practice Simulator Portfolio (a what-if sandbox, not a real basket) -- holdings, SIP entries, and initial-investment setting.",
        "parameters": {"type": "object", "properties": {}},
    }},
]

_SYSTEM_PROMPT = f"""You are the Niveshaay dashboard's own assistant. Answer questions ONLY using data you retrieve via the tools provided -- never invent a stock, price, date, or percentage. Basket keys you may need: {_BASKET_KEYS_HINT} (call list_baskets if unsure).

Two different things can be named in a question, and they use DIFFERENT tools -- never assume a name must be one of OUR baskets just because no basket matches it:
- OUR OWN baskets (the {len(_BASKET_KEYS_HINT.split(', '))} listed above) -- holdings, rebalances, returns, gains, corporate actions, watchlist, result updates.
- The 8 tracked COMPETITOR smallcases (external funds, e.g. GEM-Q Model, Consumer Durables Stars Tracker, Omni AI-Tech Global-AI Theme, AI & Data Center Theme, Wright Innovation Theme, Nirivantes TechWave Select Theme, Caprize Earnings Momentum Portfolio, Caprize Midcap & Smallcap Portfolio) -- use get_competitor_list / get_competitor_rebalance_history for these, matching by partial name.
If a name in the question doesn't match a basket, check whether it's actually a competitor smallcase before saying no data exists.

Call as many tools as needed to answer fully, including multiple baskets if the question spans more than one. Keep answers concise and concrete (real numbers/dates/names from the tool results), in plain prose or a short list -- no preamble. If the data needed isn't available through any tool, say so plainly instead of guessing."""


async def _dispatch_tool(name: str, args: dict) -> dict:
    impl = _TOOL_IMPLS.get(name)
    if not impl:
        return {"error": f"Unknown tool {name}"}
    try:
        return await asyncio.to_thread(impl, args)
    except Exception as e:
        return {"error": str(e)}


@router.post("/api/admin/assistant/ask")
async def ask_assistant(body: dict, request: Request):
    _require_admin(request)
    _current_auth_header.set(request.headers.get("Authorization", ""))
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    history = body.get("history") or []  # [{role, content}, ...] prior turns, optional
    # The frontend sends the whole conversation back on every turn, so a long
    # back-and-forth session keeps growing the request size until it blows
    # Groq's 8000 TPM cap (confirmed live: a multi-turn session hit a 413
    # rate_limit_exceeded wanting 23781 tokens). Older turns add little value
    # for a Q&A assistant anyway -- cap to the last few exchanges.
    _MAX_HISTORY_MESSAGES = 8
    history = history[-_MAX_HISTORY_MESSAGES:]

    base_messages = [{"role": "system", "content": _SYSTEM_PROMPT}] + history + [{"role": "user", "content": question}]

    from openai import AsyncOpenAI

    errors = {}
    for provider_name, env_var, base_url, model in _PROVIDERS:
        key = os.environ.get(env_var)
        if not key:
            continue
        client = AsyncOpenAI(api_key=key, base_url=base_url)
        # Fresh copy of messages per provider -- a failed attempt on one
        # provider must not leave its partial tool-call state polluting the
        # next provider's request.
        messages = list(base_messages)
        tools_used = []
        try:
            for _ in range(_MAX_TOOL_ROUNDS):
                resp = await client.chat.completions.create(
                    model=model, messages=messages, tools=_TOOL_SCHEMAS,
                    tool_choice="auto", temperature=0.2, max_tokens=2000,
                )
                msg = resp.choices[0].message
                if msg.tool_calls:
                    messages.append({
                        "role": "assistant", "content": msg.content,
                        "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                    })
                    for tc in msg.tool_calls:
                        args = json.loads(tc.function.arguments or "{}")
                        tools_used.append({"name": tc.function.name, "args": args})
                        result = await _dispatch_tool(tc.function.name, args)
                        messages.append({
                            "role": "tool", "tool_call_id": tc.id,
                            "content": json.dumps(result, default=str),
                        })
                    continue
                return {"answer": msg.content, "toolsUsed": tools_used, "provider": provider_name}
            return {"answer": "I needed too many steps to answer that -- try asking something more specific.", "toolsUsed": tools_used, "provider": provider_name}
        except Exception as e:
            errors[provider_name] = str(e)
            continue  # try the next provider

    if not errors:
        return {"answer": None, "error": "No AI provider configured -- set GROQ_API_KEY or GEMINI_API_KEY."}
    return {"answer": None, "error": f"All AI providers failed: {errors}"}
