"""Retrieval layer for the Dashboard Assistant (see routers/dashboard_assistant.py).

Added 2026-10-30, per explicit instruction to "convert into RAG" for
efficiency. The assistant's live tool-calling (kept, unchanged) is accurate
but slow -- every question pays for at least one LLM round trip PLUS a live
data fetch per tool call. This module periodically rebuilds a text snapshot
of the mostly-stable, dashboard-wide data (basket holdings/returns/profile,
competitors, corporate actions, watchlist, result-update status, benchmarks)
into small chunks, then answers the common case with ONE LLM call against
locally-retrieved chunks -- no live fetch, no multi-round tool loop.

Deliberately NOT neural-embedding retrieval: the chunk set is small (one per
basket/competitor plus a handful of global ones, ~20-30 total) and the
vocabulary is distinctive (basket names, stock codes, company names), so a
hand-rolled TF-IDF cosine match routes correctly without a PyTorch/
sentence-transformers dependency -- this project already had one real
incident from an unvetted pip dependency, and the download size alone isn't
worth it for a corpus this small.

Deliberately EXCLUDES anything user-scoped or time-critical (live prices,
market snapshot, the asking admin's own simulator, rebalance alerts) --
those still only ever come from the live tool-calling fallback, never from
a periodically-stale snapshot.
"""
import asyncio
import json
import math
import re
import time
from collections import Counter
from pathlib import Path

_SNAPSHOT_FILE = Path(__file__).parent / "assistant_knowledge.json"
_REFRESH_INTERVAL_SECONDS = 15 * 60

_chunks: list[dict] = []   # [{"id": str, "text": str}, ...]
_idf: dict[str, float] = {}
_last_built: float = 0.0
_build_lock = asyncio.Lock()

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _build_idf(chunks: list[dict]) -> dict[str, float]:
    n = len(chunks)
    doc_freq: Counter = Counter()
    for c in chunks:
        for term in set(_tokenize(c["text"])):
            doc_freq[term] += 1
    return {term: math.log((n + 1) / (df + 1)) + 1 for term, df in doc_freq.items()}


def _tfidf_vector(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    tf = Counter(tokens)
    return {term: count * idf.get(term, 1.0) for term, count in tf.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[t] * b[t] for t in common)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def _fmt_pct(v) -> str:
    return f"{v:.2f}%" if isinstance(v, (int, float)) else "n/a"


def _build_chunks() -> list[dict]:
    """Synchronous (run via asyncio.to_thread) -- reuses the SAME tool
    functions the live tool-calling path already uses, so this never
    duplicates fetch logic and always reflects the identical data source."""
    from routers import dashboard_assistant as da

    chunks = []

    baskets = da._tool_list_baskets({})
    basket_keys = list(baskets.keys()) if isinstance(baskets, dict) else []

    for key in basket_keys:
        label = baskets.get(key, key) if isinstance(baskets, dict) else key
        parts = [f"Basket: {label} (key: {key})"]
        try:
            holdings = da._tool_get_basket_holdings({"basket": key})
            lines = [
                f"- {h.get('securityName') or h.get('nseCode')} ({h.get('nseCode')}): "
                f"{_fmt_pct(h.get('allocationPct'))} allocation, buy price {h.get('buyPrice')}"
                for h in (holdings.get("holdings") or [])
            ]
            parts.append("Current holdings:\n" + "\n".join(lines))
        except Exception as e:
            parts.append(f"Holdings unavailable: {e}")
        try:
            returns = da._tool_get_basket_returns({"basket": key, "period": "1Y"})
            returns_max = da._tool_get_basket_returns({"basket": key, "period": "MAX"})
            parts.append(f"1-year return: {json.dumps(returns.get(key))}")
            parts.append(f"Since-inception return: {json.dumps(returns_max.get(key))}")
        except Exception as e:
            parts.append(f"Returns unavailable: {e}")
        try:
            profile = da._tool_get_basket_profile({"basket": key})
            parts.append(f"Profile (sector/cap mix, last rebalance): {json.dumps(profile, default=str)}")
        except Exception as e:
            parts.append(f"Profile unavailable: {e}")
        try:
            # Full dated rebalance history, not just a count -- so a
            # question like "how many rebalances between Jan and June" can
            # be answered by counting the dated entries actually in this
            # text, not just "was there a rebalance at all" (confirmed live
            # 2026-10-30 this was missing entirely before -- only
            # COMPETITOR rebalance history was in the snapshot).
            rebalances = da._tool_get_rebalance_summary({"basket": key})
            parts.append(f"Full dated rebalance history (added/removed/reweighted per date): {json.dumps(rebalances, default=str)}")
        except Exception as e:
            parts.append(f"Rebalance history unavailable: {e}")
        chunks.append({"id": f"basket:{key}", "text": "\n".join(parts)})

    try:
        competitors = da._tool_get_competitor_list({}).get("competitors") or []
    except Exception:
        competitors = []
    for c in competitors:
        key = c.get("key")
        if not key:
            continue
        parts = [f"Competitor smallcase: {c.get('label')} (manager: {c.get('manager')}, key: {key})",
                 f"CAGR: {c.get('cagr')}, launch date: {c.get('launchDate')}"]
        try:
            detail = da._tool_get_competitor_rebalance_history({"competitorKey": key})
            parts.append(f"Recent rebalance timeline: {json.dumps(detail.get('rebalanceTimeline'), default=str)}")
            parts.append(f"Latest full holding list: {json.dumps(detail.get('latestRebalanceDetail'), default=str)}")
        except Exception as e:
            parts.append(f"Rebalance detail unavailable: {e}")
        chunks.append({"id": f"competitor:{key}", "text": "\n".join(parts)})

    try:
        ca = da._tool_get_corporate_actions({}).get("corporateActions")
        chunks.append({"id": "global:corporate_actions", "text": "Corporate actions across all baskets:\n" + json.dumps(ca, default=str)})
    except Exception as e:
        chunks.append({"id": "global:corporate_actions", "text": f"Corporate actions unavailable: {e}"})

    try:
        wl = da._tool_get_watchlist({}).get("watchlist")
        chunks.append({"id": "global:watchlist", "text": "Watchlist (stocks not currently held in any basket):\n" + json.dumps(wl, default=str)})
    except Exception as e:
        chunks.append({"id": "global:watchlist", "text": f"Watchlist unavailable: {e}"})

    try:
        ru = da._tool_get_result_update_status({}).get("rows")
        chunks.append({"id": "global:result_updates", "text": "Quarterly result-update tracking status, per company:\n" + json.dumps(ru, default=str)})
    except Exception as e:
        chunks.append({"id": "global:result_updates", "text": f"Result update status unavailable: {e}"})

    try:
        bench = da._tool_get_benchmark_comparison({})
        chunks.append({"id": "global:benchmarks", "text": "Benchmark index returns (Nifty 50/200/MidSmall) and our baskets vs their own inception:\n" + json.dumps(bench, default=str)})
    except Exception as e:
        chunks.append({"id": "global:benchmarks", "text": f"Benchmark data unavailable: {e}"})

    return chunks


async def ensure_fresh(force: bool = False) -> None:
    """Rebuilds the snapshot if stale (or forced). Safe to call on every
    request -- a no-op when already fresh, and the lock means concurrent
    requests during a rebuild just wait for the one in-flight build rather
    than duplicating it."""
    global _chunks, _idf, _last_built
    if not force and _chunks and (time.time() - _last_built) < _REFRESH_INTERVAL_SECONDS:
        return
    async with _build_lock:
        if not force and _chunks and (time.time() - _last_built) < _REFRESH_INTERVAL_SECONDS:
            return  # someone else just rebuilt it while we waited for the lock
        chunks = await asyncio.to_thread(_build_chunks)
        idf = _build_idf(chunks)
        _chunks = chunks
        _idf = idf
        _last_built = time.time()
        try:
            _SNAPSHOT_FILE.write_text(json.dumps({"builtAt": _last_built, "chunks": chunks}, default=str), encoding="utf-8")
        except Exception as e:
            print(f"[assistant_rag] could not persist snapshot: {e}")
        print(f"[assistant_rag] rebuilt knowledge snapshot: {len(chunks)} chunks")


async def start_background_refresh() -> None:
    """Keeps the snapshot warm on a fixed interval regardless of whether
    anyone's actually asked the assistant a question recently -- confirmed
    live 2026-10-30 that building it inline on a cold first request took
    long enough (dozens of sequential tool calls) to make that first user's
    question itself time out. Call once from the app's startup hook (fire-
    and-forget via loop.create_task), never awaited directly."""
    while True:
        try:
            await ensure_fresh(force=True)
        except Exception as e:
            print(f"[assistant_rag] background refresh failed: {e}")
        await asyncio.sleep(_REFRESH_INTERVAL_SECONDS)


def retrieve(question: str, top_k: int = 6) -> str:
    """Returns the top-K most relevant chunks' text, concatenated. Call
    ensure_fresh() first (await it) -- this part is sync/instant (just
    local TF-IDF cosine similarity over whatever's already in memory)."""
    if not _chunks:
        return ""
    q_vec = _tfidf_vector(_tokenize(question), _idf)
    scored = []
    for c in _chunks:
        c_vec = _tfidf_vector(_tokenize(c["text"]), _idf)
        scored.append((_cosine(q_vec, c_vec), c))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [c["text"] for score, c in scored[:top_k] if score > 0]
    return "\n\n---\n\n".join(top)
