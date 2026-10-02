"""AI-generated "Rebalance Insights" for the Competitor Analysis page --
given a condensed, already-computed summary of our basket vs. one competitor
(recent rebalance changes on both sides, sector/cap-mix gaps, returns,
timing-insight buy/sell matches with real prices), asks an LLM to point out
concrete places we could have done better: later entries at worse prices,
sector concentration the competitor avoided, stocks they caught and we
didn't, etc. Grounded only in the data handed to it -- the prompt explicitly
forbids inventing numbers not present in the summary.

Uses Groq's free tier (OpenAI-compatible API, just a different base_url +
model) rather than OpenAI directly -- the OpenAI key in .env is valid but
that account has no billing/credits set up, confirmed live (2026-10-02,
insufficient_quota). Swapping back just means changing _PROVIDER below.

Cached per (basket, competitor, digest-of-summary) so the same underlying
data never pays for a second LLM call -- only a genuinely new rebalance on
either side (which changes the summary and therefore the digest) triggers a
fresh generation."""
import hashlib
import json
import os
import re
from pathlib import Path

_CACHE_FILE = Path(__file__).parent / "rebalance_insights_cache.json"

# Groq's API is OpenAI-compatible -- same `openai` client library, just a
# different base_url, API key, and model name.
_PROVIDER = "groq"
_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
_GROQ_MODEL = "openai/gpt-oss-120b"
_OPENAI_MODEL = "gpt-4o-mini"

_SYSTEM_PROMPT = """You are a portfolio analyst comparing OUR equity basket against ONE competitor's smallcase portfolio. You are given a JSON summary of real, already-computed data: recent rebalance changes on both sides (with real buy/sell dates and, where available, real OHLC-average prices and weights), sector/market-cap mix, returns, and "timing insight" matches (stocks both portfolios added/removed within 30 days of each other, with the actual price each side paid).

Produce 4-10 short, specific, one-sentence observations from OUR perspective, each classified as either something we did BETTER than the competitor ("good") or something the competitor did better than us / a risk we're exposed to that they avoided ("bad"). Be honest and balanced -- don't force a 50/50 split, but don't report only one side either if the data supports both.

Respond with ONLY a JSON object of this exact shape, no other text:
{"good": ["sentence one.", "sentence two."], "bad": ["sentence one.", "sentence two."]}

Rules:
- Use ONLY numbers present in the JSON. Never invent a price, date, or percentage.
- Every number in the JSON is already rounded to at most 2 decimal places for display -- quote it exactly as given (e.g. "48.97%"), never with extra decimal digits.
- `returns.oursPct`/`returns.theirsPct` are already percentages (NOT 0-1 fractions) -- write them with a "%" sign, e.g. "48.97%", not "0.4897" or "48.97". When citing the return, always state the period it covers using `returns.periodLabel`, `returns.fromDate` and `returns.toDate` (e.g. "our 1Y return from 2 Oct 2025 to 2 Oct 2026 was 48.97% vs the competitor's 3.77%").
- If a timing-insight match shows one side paid a lower price for the same stock, put that observation in "good" (if we paid less / entered earlier) or "bad" (if the competitor did), citing both numbers.
- Each string is one sentence, no leading bullet characters, dashes, or numbering.
- If there is genuinely nothing to say for one side, return an empty array for it rather than padding with generic filler.
- If the data is too thin to say anything concrete at all, return {"good": [], "bad": []}."""


def _load_cache() -> dict:
    if _CACHE_FILE.exists():
        try:
            return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(data: dict) -> None:
    _CACHE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _digest(summary: dict) -> str:
    blob = json.dumps(summary, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _parse_good_bad(text: str) -> dict:
    """The model is asked for raw JSON but sometimes wraps it in a markdown
    code fence or adds stray text around it -- pull out the first {...}
    block and parse that instead of failing outright."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    good = [str(s).strip() for s in (parsed.get("good") or []) if str(s).strip()]
    bad = [str(s).strip() for s in (parsed.get("bad") or []) if str(s).strip()]
    return {"good": good, "bad": bad}


async def generate_insights(basket: str, competitor_key: str, summary: dict) -> dict:
    """Returns {"good": [str], "bad": [str], "cached": bool, "generatedAt": str}.
    Never raises -- a provider error or an unparsable response comes back as
    {"good": None, "bad": None, "error": str} so the frontend can show a
    clear message instead of a blank crash."""
    if _PROVIDER == "groq":
        key = os.environ.get("GROQ_API_KEY")
        key_name, base_url, model = "GROQ_API_KEY", _GROQ_BASE_URL, _GROQ_MODEL
    else:
        key = os.environ.get("OPENAI_API_KEY")
        key_name, base_url, model = "OPENAI_API_KEY", None, _OPENAI_MODEL
    if not key:
        return {"good": None, "bad": None, "error": f"{key_name} not set"}

    digest = _digest(summary)
    cache_key = f"{basket}|{competitor_key}|{digest}"
    cache = _load_cache()
    cached = cache.get(cache_key)
    if cached and "good" in cached:
        return {"good": cached["good"], "bad": cached["bad"], "cached": True, "generatedAt": cached["generatedAt"]}

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=key, base_url=base_url) if base_url else AsyncOpenAI(api_key=key)
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(summary, default=str)},
            ],
            temperature=0.3,
            max_tokens=2000,
            response_format={"type": "json_object"},
        )
        text = (resp.choices[0].message.content or "").strip()
        parsed = _parse_good_bad(text)
    except Exception as e:
        return {"good": None, "bad": None, "error": str(e)}

    from datetime import datetime, timezone
    now_str = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    cache[cache_key] = {"good": parsed["good"], "bad": parsed["bad"], "generatedAt": now_str}
    _save_cache(cache)
    return {"good": parsed["good"], "bad": parsed["bad"], "cached": False, "generatedAt": now_str}
