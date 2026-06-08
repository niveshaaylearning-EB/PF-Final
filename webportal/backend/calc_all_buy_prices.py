"""
Standalone script: calculate OHLC-weighted avg buy price for every stock with
buy events across all baskets, then persist to portfolios.json.

Run: python calc_all_buy_prices.py
"""
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).parent

BASKETS = [
    "Mid_Small_Cap", "Green_Energy", "IPO_Basket",
    "Trends_Triology", "Techstack", "Make_in_India", "Consumer_Trends",
]


def parse_events(text: str) -> list[tuple[str, float]]:
    events = []
    for line in (text or "").strip().split("\n"):
        parts = re.split(r"[*×]", line.strip())
        if len(parts) != 2:
            continue
        try:
            events.append((parts[0].strip(), float(parts[1].strip())))
        except ValueError:
            pass
    return events


def _date_ts(date_str: str) -> int:
    try:
        return int(datetime.strptime(date_str.strip(), "%d %b %Y").replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return 0


def current_series_buy_events(
    buy_events: list[tuple[str, float]],
    sell_events: list[tuple[str, float]],
) -> list[tuple[str, float]]:
    """
    Returns only buy events from the current active series.
    When net weight hits 0 (full exit) and the stock is re-entered,
    prior buy events are excluded from the buy-price calculation.
    """
    combined = (
        [(d, "buy",  q) for d, q in buy_events] +
        [(d, "sell", q) for d, q in sell_events]
    )
    combined.sort(key=lambda e: _date_ts(e[0]))

    net: float = 0.0
    series: list[tuple[str, float]] = []

    for date_str, etype, qty in combined:
        if etype == "buy":
            if net <= 0.001:
                series = []
            series.append((date_str, qty))
            net += qty
        else:
            net = max(0.0, net - qty)
            if net <= 0.001:
                series = []

    return series


YF_SYMBOL_MAP: dict = {
    "544531":    "TRUECOLORS.BO",
    "ACUTAAS":   "ACUTAAS.BO",
    "HBLENGINE": "HBLENGINE.BO",
    "ARIS":      "ARIS.BO",
    "SETL":      "SETL.BO",
}


async def _screener_price(nse: str, dt: datetime, client: httpx.AsyncClient) -> float | None:
    """Close price from Screener.in — last-resort for BSE-only/pre-listing stocks."""
    from datetime import timedelta
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
                return round(prices[check], 4)
    except Exception:
        pass
    return None


async def fetch_ohlc(nse: str, date_str: str, client: httpx.AsyncClient, sem: asyncio.Semaphore) -> float | None:
    """OHLC avg for date_str. Yahoo Finance (.NS/.BO) first, Screener.in last resort."""
    async with sem:
        dt = datetime.strptime(date_str, "%d %b %Y")
        ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
        sym = YF_SYMBOL_MAP.get(nse, f"{nse}.NS")
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
            f"?interval=1d&period1={ts}&period2={ts + 4 * 86400}"
        )
        try:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
            result = r.json()["chart"]["result"][0]
            q = result["indicators"]["quote"][0]
            timestamps = result.get("timestamp", [])
            for i, (o, h, l, c) in enumerate(zip(q["open"], q["high"], q["low"], q["close"])):
                if None not in (o, h, l, c):
                    if not timestamps or timestamps[i] >= ts - 86400:
                        return round((o + h + l + c) / 4, 4)
        except Exception:
            pass
        return await _screener_price(nse, dt, client)


async def main():
    bp_path = BASE / "buy_price_data.json"
    with open(bp_path, encoding="utf-8") as f:
        bp_data = json.load(f)
    with open(BASE / "portfolios.json", encoding="utf-8") as f:
        portfolios = json.load(f)

    sem = asyncio.Semaphore(12)
    total_ok  = 0
    total_err = 0
    bp_changed = False
    errors = []

    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        for basket in BASKETS:
            basket_bp   = bp_data.get(basket, {})
            basket_stks = portfolios.get(basket, [])
            stk_map     = {s["nseCode"]: s for s in basket_stks}
            print(f"\n── {basket} ({len(basket_bp)} stocks) ──")

            for nse, det in basket_bp.items():
                all_buy  = parse_events(det.get("buyEvents")  or "")
                all_sell = parse_events(det.get("sellEvents") or "")
                buy_events = current_series_buy_events(all_buy, all_sell)
                if not buy_events:
                    continue

                # Use cached buyOHLC prices where available; only fetch what is missing
                cached_ohlc = det.get("buyOHLC") or {}
                ohlc_avgs: list[float | None] = []
                newly_fetched: dict[str, float] = {}
                for date_str, _ in buy_events:
                    if date_str in cached_ohlc:
                        ohlc_avgs.append(cached_ohlc[date_str])
                    else:
                        val = await fetch_ohlc(nse, date_str, client, sem)
                        ohlc_avgs.append(val)
                        if val is not None:
                            newly_fetched[date_str] = val

                # Persist any newly fetched OHLC prices
                if newly_fetched:
                    det["buyOHLC"] = {**cached_ohlc, **newly_fetched}
                    bp_changed = True

                missing = [buy_events[i][0] for i, v in enumerate(ohlc_avgs) if v is None]
                if missing:
                    msg = f"  SKIP {nse}: OHLC missing for {', '.join(missing)}"
                    print(msg)
                    errors.append(f"{basket}/{nse}: {msg.strip()}")
                    total_err += 1
                    continue

                total_qty    = sum(qty for _, qty in buy_events)
                weighted_sum = sum(qty * avg for (_, qty), avg in zip(buy_events, ohlc_avgs))
                buy_price    = round(weighted_sum / total_qty, 2)

                if nse not in stk_map:
                    continue  # skip sold/non-active stocks
                stk_map[nse]["buyPrice"] = buy_price

                total_ok += 1
                ev_summary = "  +  ".join(f"{d} ×{q:g}%" for d, q in buy_events)
                cached_note = f"  ({len(cached_ohlc)} cached)" if cached_ohlc else ""
                print(f"  {nse:15s}  ₹{buy_price:>9.2f}   [{ev_summary}]{cached_note}")

            portfolios[basket] = basket_stks

    with open(BASE / "portfolios.json", "w", encoding="utf-8") as f:
        json.dump(portfolios, f, indent=2, ensure_ascii=False)

    if bp_changed:
        with open(bp_path, "w", encoding="utf-8") as f:
            json.dump(bp_data, f, indent=2, ensure_ascii=False)
        print("\nbuy_price_data.json updated with newly fetched OHLC prices.")

    print(f"\n{'='*60}")
    print(f"Done: {total_ok} calculated, {total_err} skipped")
    if errors:
        print("\nSkipped:")
        for e in errors:
            print(f"  {e}")


if __name__ == "__main__":
    asyncio.run(main())
