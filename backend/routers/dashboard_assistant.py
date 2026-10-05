"""Dashboard Assistant -- a tool-calling LLM (Groq, same setup as
webportal/backend/rebalance_insights.py) that answers natural-language
questions about what's actually in this dashboard: basket holdings, buy
prices, rebalance history, result-update tracking, corporate actions,
watchlist, and competitor data. Admin-only for now.

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
import json
import os
from pathlib import Path

import requests as _http
from fastapi import APIRouter, HTTPException, Request

from auth import is_admin_email

router = APIRouter()

_WEBPORTAL = "http://127.0.0.1:8000/wp"
_GROQ_MODEL = "openai/gpt-oss-120b"
_MAX_TOOL_ROUNDS = 6


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


def _tool_get_gains_statement(args: dict) -> dict:
    data = _wp_get("/api/gains-statement")
    basket = args.get("basket")
    return {basket: data.get(basket, {})} if basket else data


def _tool_get_corporate_actions(_args: dict) -> dict:
    return {"corporateActions": _wp_get("/api/corporate-actions")}


def _tool_get_watchlist(_args: dict) -> dict:
    return {"watchlist": _wp_get("/api/watchlist")}


def _tool_get_competitor_list(_args: dict) -> dict:
    return _wp_get("/api/admin/competitor-list")


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


_TOOL_IMPLS = {
    "list_baskets": _tool_list_baskets,
    "get_basket_holdings": _tool_get_basket_holdings,
    "get_rebalance_history": _tool_get_rebalance_history,
    "get_gains_statement": _tool_get_gains_statement,
    "get_corporate_actions": _tool_get_corporate_actions,
    "get_watchlist": _tool_get_watchlist,
    "get_competitor_list": _tool_get_competitor_list,
    "get_result_update_status": _tool_get_result_update_status,
    "get_results_calendar": _tool_get_results_calendar,
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
        "description": "Dated rebalance events for one basket: stocks added, removed, or reweighted on each date.",
        "parameters": {"type": "object", "properties": {
            "basket": {"type": "string", "description": "Exact basket key"},
        }, "required": ["basket"]},
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
        "description": "The 8 tracked competitor smallcases: label, fund manager, last-fetched stocks/returns/rebalance data.",
        "parameters": {"type": "object", "properties": {}},
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
]

_SYSTEM_PROMPT = f"""You are the Niveshaay dashboard's own assistant. Answer questions ONLY using data you retrieve via the tools provided -- never invent a stock, price, date, or percentage. Basket keys you may need: {_BASKET_KEYS_HINT} (call list_baskets if unsure).

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
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    history = body.get("history") or []  # [{role, content}, ...] prior turns, optional

    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return {"answer": None, "error": "GROQ_API_KEY not set"}

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=key, base_url="https://api.groq.com/openai/v1")

    messages = [{"role": "system", "content": _SYSTEM_PROMPT}] + history + [{"role": "user", "content": question}]
    tools_used = []

    try:
        for _ in range(_MAX_TOOL_ROUNDS):
            resp = await client.chat.completions.create(
                model=_GROQ_MODEL, messages=messages, tools=_TOOL_SCHEMAS,
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
            return {"answer": msg.content, "toolsUsed": tools_used}
        return {"answer": "I needed too many steps to answer that -- try asking something more specific.", "toolsUsed": tools_used}
    except Exception as e:
        return {"answer": None, "error": str(e)}
