"""
Fetches the OHLC-average price (open+high+low+close)/4 for every buy-event
and sell-event date recorded in buy_price_data.json, then persists the results
as two new dict fields on each stock entry:

  buyOHLC  →  { "DD MMM YYYY": <avg_price>, ... }
  sellOHLC →  { "DD MMM YYYY": <avg_price>, ... }

Already-cached dates are skipped (incremental — safe to re-run at any time).
Source chain: Yahoo Finance (.NS) → Google Finance → Screener.in (BSE-only fallback).

Run:  python calc_event_ohlc.py
"""

import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

# BSE-only stocks: use .BO suffix on Yahoo Finance instead of .NS
YF_SYMBOL_MAP: dict = {
    "544531":    "TRUECOLORS.BO",
    "ACUTAAS":   "ACUTAAS.BO",
    "HBLENGINE": "HBLENGINE.BO",
    "ARIS":      "ARIS.BO",
    "SETL":      "SETL.BO",
}

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).parent

BASKETS = [
    "Mid_Small_Cap", "Green_Energy", "IPO_Basket",
    "Trends_Triology", "Techstack", "Make_in_India", "Consumer_Trends",
]

YF_HEADERS = {"User-Agent": "Mozilla/5.0"}
CONCURRENCY = 12


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_events(text: str) -> list[tuple[str, float]]:
    """'DD MMM YYYY * weight\\n...' → [(date_str, weight), ...]"""
    events = []
    for line in (text or "").strip().splitlines():
        parts = re.split(r"[*×]", line.strip())
        if len(parts) != 2:
            continue
        try:
            events.append((parts[0].strip(), float(parts[1].strip())))
        except ValueError:
            pass
    return events


async def _yahoo_ohlc(nse: str, ts: int, client: httpx.AsyncClient) -> float | None:
    sym = YF_SYMBOL_MAP.get(nse, f"{nse}.NS")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
        f"?interval=1d&period1={ts}&period2={ts + 4 * 86400}"
    )
    try:
        r = await client.get(url, timeout=12)
        res = r.json().get("chart", {}).get("result", [])
        if not res:
            return None
        q = res[0].get("indicators", {}).get("quote", [{}])[0]
        timestamps = res[0].get("timestamp", [])
        for i, (o, h, l, c) in enumerate(zip(
            q.get("open", []), q.get("high", []),
            q.get("low",  []), q.get("close", [])
        )):
            if None not in (o, h, l, c):
                if not timestamps or timestamps[i] >= ts - 86400:
                    return round((o + h + l + c) / 4, 2)
    except Exception:
        pass
    return None


async def _google_ohlc(nse: str, dt: datetime, client: httpx.AsyncClient) -> float | None:
    target_ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
    url = (
        f"https://finance.google.com/finance/getprices"
        f"?q={nse}&x=NSE&i=86400&p=40d&f=d,o,h,l,c,v&df=cpct&auto=1"
    )
    try:
        r = await client.get(url, timeout=12)
        base_ts = None
        for line in r.text.strip().splitlines():
            if line.startswith(("TIMEZONE", "MARKET", "EXCHANGE", "DATA")):
                continue
            parts = line.split(",")
            if line.startswith("a"):
                base_ts = int(parts[0][1:])
                offset = 0
            else:
                try:
                    offset = int(parts[0])
                except ValueError:
                    continue
            if base_ts is None or len(parts) < 5:
                continue
            row_ts = base_ts + offset * 86400
            if abs(row_ts - target_ts) < 4 * 86400:
                try:
                    o, c, h, l = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
                    if 0 not in (o, h, l, c):
                        return round((o + h + l + c) / 4, 2)
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass
    return None


async def _screener_ohlc(nse: str, dt: datetime, client: httpx.AsyncClient) -> float | None:
    """Close price from Screener.in — last-resort for BSE-only/pre-listing stocks."""
    try:
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
        api_headers = {
            "User-Agent": ua,
            "Accept": "application/json, */*",
            "Referer": "https://www.screener.in/",
            "X-Requested-With": "XMLHttpRequest",
        }
        await client.get(
            f"https://www.screener.in/company/{nse}/consolidated/",
            headers={"User-Agent": ua},
        )
        search_r = await client.get(
            f"https://www.screener.in/api/company/search/?q={nse}&v=3&fts=1",
            headers=api_headers,
        )
        company_id = None
        for item in search_r.json():
            if f"/company/{nse}/" in item.get("url", ""):
                company_id = item.get("id")
                break
        if not company_id:
            return None
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
        for i in range(5):
            check = (dt + timedelta(days=i)).strftime("%Y-%m-%d")
            if check in prices:
                return round(prices[check], 2)
    except Exception:
        pass
    return None


async def fetch_ohlc(
    nse: str, date_str: str, client: httpx.AsyncClient, sem: asyncio.Semaphore
) -> float | None:
    """OHLC avg for date_str. Yahoo Finance (.NS/.BO) → Google Finance → Screener.in."""
    async with sem:
        try:
            dt = datetime.strptime(date_str, "%d %b %Y")
        except ValueError:
            return None
        ts = int(dt.replace(tzinfo=timezone.utc).timestamp())

        val = await _yahoo_ohlc(nse, ts, client)
        if val is not None:
            return val
        val = await _google_ohlc(nse, dt, client)
        if val is not None:
            return val
        return await _screener_ohlc(nse, dt, client)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    bp_path = BASE / "buy_price_data.json"
    with open(bp_path, encoding="utf-8") as f:
        bp_data = json.load(f)

    sem = asyncio.Semaphore(CONCURRENCY)
    total_fetched = total_missing = total_cached = 0

    async with httpx.AsyncClient(
        follow_redirects=True, headers=YF_HEADERS, timeout=15
    ) as client:
        for basket in BASKETS:
            basket_bp = bp_data.get(basket, {})
            if not basket_bp:
                continue
            print(f"\n{'─'*55}")
            print(f"  {basket}  ({len(basket_bp)} stocks)")
            print(f"{'─'*55}")

            # Collect unique (nse, date) pairs not yet cached
            pairs_needed: set[tuple[str, str]] = set()
            for nse, det in basket_bp.items():
                cached_buy  = det.get("buyOHLC")  or {}
                cached_sell = det.get("sellOHLC") or {}
                for date_str, _ in parse_events(det.get("buyEvents") or ""):
                    if date_str not in cached_buy:
                        pairs_needed.add((nse, date_str))
                    else:
                        total_cached += 1
                for date_str, _ in parse_events(det.get("sellEvents") or ""):
                    if date_str not in cached_sell:
                        pairs_needed.add((nse, date_str))
                    else:
                        total_cached += 1

            unique_pairs = sorted(pairs_needed)
            if not unique_pairs:
                print("  All dates already cached — nothing to fetch.")
                continue
            print(f"  Fetching {len(unique_pairs)} new (stock, date) pairs...")

            # Concurrent fetch
            prices = await asyncio.gather(
                *[fetch_ohlc(nse, date_str, client, sem)
                  for nse, date_str in unique_pairs]
            )
            ohlc_cache: dict[tuple[str, str], float | None] = {
                pair: price for pair, price in zip(unique_pairs, prices)
            }

            # Write results back into bp_data
            for nse, det in basket_bp.items():
                buy_ohlc  = dict(det.get("buyOHLC")  or {})
                sell_ohlc = dict(det.get("sellOHLC") or {})

                for date_str, _ in parse_events(det.get("buyEvents") or ""):
                    if date_str in buy_ohlc:
                        continue
                    price = ohlc_cache.get((nse, date_str))
                    if price is not None:
                        buy_ohlc[date_str] = price
                        total_fetched += 1
                        print(f"  [BUY ] {nse:15s} @ {date_str} = ₹{price:,.2f}")
                    else:
                        total_missing += 1
                        print(f"  [BUY ] {nse:15s} @ {date_str} = MISSING")

                for date_str, _ in parse_events(det.get("sellEvents") or ""):
                    if date_str in sell_ohlc:
                        continue
                    price = ohlc_cache.get((nse, date_str))
                    if price is not None:
                        sell_ohlc[date_str] = price
                        total_fetched += 1
                        print(f"  [SELL] {nse:15s} @ {date_str} = ₹{price:,.2f}")
                    else:
                        total_missing += 1
                        print(f"  [SELL] {nse:15s} @ {date_str} = MISSING")

                det["buyOHLC"]  = buy_ohlc
                det["sellOHLC"] = sell_ohlc

    # Persist
    with open(bp_path, "w", encoding="utf-8") as f:
        json.dump(bp_data, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*55}")
    print(f"  Done.")
    print(f"  Already cached : {total_cached}")
    print(f"  Newly fetched  : {total_fetched}")
    print(f"  Missing/failed : {total_missing}")
    print(f"  buy_price_data.json updated.")
    print(f"{'='*55}")


if __name__ == "__main__":
    asyncio.run(main())
