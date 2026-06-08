"""
fetch_all_historical_ohlc.py

Step 1 — Fetch all missing OHLC data for every basket:
         buyEvents/prevBuyEvents → buyOHLC
         sellEvents/prevSellEvents → sellOHLC
         Saves to buy_price_data.json.

Step 2 — Populate portfolios.json sold entries with buyPrice / sellPrice.

Step 3 — Regenerate gains_statement.json using FIFO calculation.

Run:  python fetch_all_historical_ohlc.py
Re-runnable — skips already-cached dates.
"""

import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8")

BASE     = Path(__file__).parent
BP_FILE  = BASE / "buy_price_data.json"
PF_FILE  = BASE / "portfolios.json"
GS_FILE  = BASE / "gains_statement.json"

BASKETS = [
    "Mid_Small_Cap", "Green_Energy", "IPO_Basket",
    "Trends_Triology", "Techstack", "Make_in_India", "Consumer_Trends",
]

CONCURRENCY = 12
YF_HEADERS  = {"User-Agent": "Mozilla/5.0"}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_events(text: str) -> list[tuple[str, float]]:
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


def _date_ts(date_str: str) -> int:
    try:
        return int(datetime.strptime(date_str.strip(), "%d %b %Y")
                   .replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return 0


def next_trading_day(dt: datetime) -> datetime:
    d = dt + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def fmt_date(dt: datetime) -> str:
    MONTHS = {1:'Jan',2:'Feb',3:'Mar',4:'Apr',5:'May',6:'Jun',
              7:'Jul',8:'Aug',9:'Sep',10:'Oct',11:'Nov',12:'Dec'}
    return f"{dt.day:02d} {MONTHS[dt.month]} {dt.year}"


async def _yahoo_ohlc(nse: str, ts: int, client: httpx.AsyncClient) -> float | None:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{nse}.NS"
        f"?interval=1d&period1={ts}&period2={ts + 4 * 86400}"
    )
    try:
        r = await client.get(url, timeout=12)
        res = r.json().get("chart", {}).get("result", [])
        if not res:
            return None
        q = res[0].get("indicators", {}).get("quote", [{}])[0]
        for o, h, l, c in zip(
            q.get("open", []), q.get("high", []),
            q.get("low",  []), q.get("close", [])
        ):
            if None not in (o, h, l, c):
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
                offset  = 0
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


async def fetch_ohlc(
    nse: str, date_str: str, client: httpx.AsyncClient, sem: asyncio.Semaphore
) -> float | None:
    async with sem:
        try:
            dt = datetime.strptime(date_str, "%d %b %Y")
        except ValueError:
            return None
        ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
        val = await _yahoo_ohlc(nse, ts, client)
        if val is not None:
            return val
        return await _google_ohlc(nse, dt, client)


# ─────────────────────────────────────────────────────────────────────────────
# FIFO gains computation (mirrors main.py's _compute_all_gains)
# ─────────────────────────────────────────────────────────────────────────────

def _total_to_delta(buy_events: list[tuple[str, float]],
                    sell_events: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Convert total-weight buyEvents to delta weights (net buy per event)."""
    combined = ([(d, "buy",  q) for d, q in buy_events] +
                [(d, "sell", q) for d, q in sell_events])
    combined.sort(key=lambda e: _date_ts(e[0]))
    net, result = 0.0, []
    for date_str, etype, qty in combined:
        if etype == "buy":
            delta = qty - net
            if delta > 0.001:
                result.append((date_str, round(delta, 6)))
            net = qty
        else:
            net = max(0.0, net - qty)
    return result


def _fifo_gains(buy_ev: list[tuple[str, float]],
                sell_ev: list[tuple[str, float]],
                buy_ohlc: dict, sell_ohlc: dict) -> list[dict]:
    if not buy_ev or not sell_ev:
        return []
    buy_q = sorted(
        [{"date": d, "remaining": q, "price": buy_ohlc.get(d)} for d, q in buy_ev],
        key=lambda e: _date_ts(e["date"]),
    )
    gains = []
    for sell_date, sell_weight in sorted(sell_ev, key=lambda e: _date_ts(e[0])):
        sell_price = sell_ohlc.get(sell_date)
        remaining  = sell_weight
        lots = []
        for lot in buy_q:
            if remaining < 1e-6 or lot["remaining"] < 1e-6:
                continue
            take = min(lot["remaining"], remaining)
            lot["remaining"] = round(lot["remaining"] - take, 6)
            remaining        = round(remaining - take, 6)
            bp = lot["price"]
            gains_pct = round((sell_price - bp) / bp * 100, 2) if (bp and sell_price and bp > 0) else None
            lots.append({"buyDate": lot["date"], "weight": round(take, 4),
                         "buyPrice": bp, "gainPct": gains_pct})
        valid   = [l for l in lots if l["gainPct"] is not None]
        total_w = sum(l["weight"] for l in valid)
        wt_gain = round(sum(l["gainPct"] * l["weight"] for l in valid) / total_w, 2) if total_w > 0 else None
        gains.append({
            "sellDate":        sell_date,
            "sellWeight":      sell_weight,
            "sellPrice":       sell_price,
            "lots":            lots,
            "weightedGainPct": wt_gain,
        })
    return gains


def compute_all_gains(bp_data: dict) -> dict:
    result = {}
    for basket in BASKETS:
        basket_bp = bp_data.get(basket, {})
        basket_result = {}
        for nse, det in basket_bp.items():
            buy_ev      = parse_events(det.get("buyEvents")      or "")
            sell_ev     = parse_events(det.get("sellEvents")     or "")
            prev_buy_ev = parse_events(det.get("prevBuyEvents")  or "")
            prev_sel_ev = parse_events(det.get("prevSellEvents") or "")
            buy_ohlc    = det.get("buyOHLC")  or {}
            sell_ohlc   = det.get("sellOHLC") or {}

            # All baskets use delta-weight buyEvents after fix_all_issues.py
            prev_gains   = _fifo_gains(prev_buy_ev, prev_sel_ev, buy_ohlc, sell_ohlc)
            curr_gains   = _fifo_gains(buy_ev,      sell_ev,     buy_ohlc, sell_ohlc)

            if prev_gains or curr_gains:
                basket_result[nse] = {
                    "securityName":      det.get("securityName", ""),
                    "prevSeriesGains":   prev_gains,
                    "currentSeriesGains": curr_gains,
                }
        if basket_result:
            result[basket] = basket_result
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Step 2: populate portfolios sold entries
# ─────────────────────────────────────────────────────────────────────────────

def _weighted_buy_price(buy_delta_ev: list[tuple[str, float]],
                        buy_ohlc: dict) -> float | None:
    valid = [(d, w) for d, w in buy_delta_ev if buy_ohlc.get(d) is not None]
    if not valid or len(valid) != len(buy_delta_ev):
        return None
    total_w = sum(w for _, w in valid)
    if total_w < 1e-6:
        return None
    return round(sum(w * buy_ohlc[d] for d, w in valid) / total_w, 2)


def _find_sell_ohlc_date(sold_date_str: str,
                         sell_events: list[tuple[str, float]],
                         sell_ohlc: dict) -> float | None:
    """Match a sold entry's rebalance date to the closest sell event's OHLC price."""
    # Direct hit (sold entry date is already the next-trading-day format)
    if sold_date_str in sell_ohlc:
        return sell_ohlc[sold_date_str]

    # Try next_trading_day of sold entry date
    try:
        dt = datetime.strptime(sold_date_str, "%d %b %Y")
        ntd = fmt_date(next_trading_day(dt))
        if ntd in sell_ohlc:
            return sell_ohlc[ntd]
    except ValueError:
        pass

    # Find closest sell event date (within 7 days)
    if not sell_events:
        return None
    try:
        sold_ts = _date_ts(sold_date_str)
        closest = min(
            [(d, w) for d, w in sell_events if abs(_date_ts(d) - sold_ts) < 7 * 86400],
            key=lambda e: abs(_date_ts(e[0]) - sold_ts),
            default=None,
        )
        if closest and sell_ohlc.get(closest[0]) is not None:
            return sell_ohlc[closest[0]]
    except Exception:
        pass
    return None


def populate_sold_entries(bp_data: dict, portfolios: dict) -> int:
    updated = 0
    for basket in BASKETS:
        basket_bp   = bp_data.get(basket, {})
        sold_key    = f"{basket}_sold"
        sold_list   = portfolios.get(sold_key, [])
        if not sold_list:
            continue

        for entry in sold_list:
            ticker     = entry.get("nseCode", "")
            sold_date  = entry.get("date", "")
            det        = basket_bp.get(ticker, {})
            if not det:
                continue

            buy_ohlc  = det.get("buyOHLC")  or {}
            sell_ohlc = det.get("sellOHLC") or {}
            buy_ev    = parse_events(det.get("buyEvents")     or "")
            sell_ev   = parse_events(det.get("sellEvents")    or "")
            prev_buy  = parse_events(det.get("prevBuyEvents") or "")
            prev_sell = parse_events(det.get("prevSellEvents")or "")

            # ── Sell price ────────────────────────────────────────────────────
            if entry.get("sellPrice") is None:
                # Determine which series this sell event belongs to by proximity to sell event dates
                sp = _find_sell_ohlc_date(sold_date, prev_sell + sell_ev, sell_ohlc)
                if sp is not None:
                    entry["sellPrice"] = sp
                    updated += 1

            # ── Buy price ─────────────────────────────────────────────────────
            if entry.get("buyPrice") is None:
                # Decide series: if sold date is close to a prevSell event → prev series
                sold_ts = _date_ts(sold_date)
                in_prev = any(abs(_date_ts(d) - sold_ts) < 8 * 86400 for d, _ in prev_sell)

                if in_prev and prev_buy:
                    bp = _weighted_buy_price(prev_buy, buy_ohlc)
                elif buy_ev:
                    bp = _weighted_buy_price(buy_ev, buy_ohlc)
                else:
                    bp = None

                if bp is not None:
                    entry["buyPrice"] = bp
                    updated += 1

        portfolios[sold_key] = sold_list

    return updated


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: fetch all missing OHLC
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_all_ohlc(bp_data: dict) -> tuple[int, int, int]:
    sem              = asyncio.Semaphore(CONCURRENCY)
    total_cached     = 0
    total_fetched    = 0
    total_missing    = 0

    async with httpx.AsyncClient(
        follow_redirects=True, headers=YF_HEADERS, timeout=15
    ) as client:
        for basket in BASKETS:
            basket_bp = bp_data.get(basket, {})
            if not basket_bp:
                continue

            # Collect all (nse, date, field) triples needed
            # field = "buy" or "sell" → maps to buyOHLC / sellOHLC
            pairs_needed: dict[tuple[str, str], str] = {}  # (nse, date) → "buy" or "sell"

            for nse, det in basket_bp.items():
                cached_buy  = det.get("buyOHLC")  or {}
                cached_sell = det.get("sellOHLC") or {}

                for field_key, ohlc_cache, ohlc_type in [
                    ("buyEvents",      cached_buy,  "buy"),
                    ("prevBuyEvents",  cached_buy,  "buy"),
                    ("sellEvents",     cached_sell, "sell"),
                    ("prevSellEvents", cached_sell, "sell"),
                ]:
                    for date_str, _ in parse_events(det.get(field_key) or ""):
                        if date_str in ohlc_cache:
                            total_cached += 1
                        else:
                            key = (nse, date_str)
                            if key not in pairs_needed:
                                pairs_needed[key] = ohlc_type

            unique_pairs = sorted(pairs_needed.keys())
            if not unique_pairs:
                print(f"  {basket}: all dates cached, nothing to fetch.")
                continue

            print(f"\n── {basket}: fetching {len(unique_pairs)} new (stock, date) pairs ──")

            # Concurrent fetch
            prices = await asyncio.gather(
                *[fetch_ohlc(nse, date_str, client, sem)
                  for nse, date_str in unique_pairs]
            )

            # Write back into bp_data
            for (nse, date_str), price in zip(unique_pairs, prices):
                det       = basket_bp[nse]
                ohlc_type = pairs_needed[(nse, date_str)]

                if ohlc_type == "buy":
                    cache = det.setdefault("buyOHLC", {})
                else:
                    cache = det.setdefault("sellOHLC", {})

                if price is not None:
                    cache[date_str] = price
                    total_fetched  += 1
                    print(f"  [{ohlc_type.upper()}] {nse:15s} @ {date_str} = ₹{price:,.2f}")
                else:
                    total_missing += 1
                    print(f"  [{ohlc_type.upper()}] {nse:15s} @ {date_str} = MISSING")

    return total_cached, total_fetched, total_missing


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    print("Loading data files...")
    with open(BP_FILE, encoding="utf-8") as f:
        bp_data = json.load(f)
    with open(PF_FILE, encoding="utf-8") as f:
        portfolios = json.load(f)

    # ── Step 1: fetch OHLC ────────────────────────────────────────────────────
    print("\n[Step 1] Fetching all missing OHLC (buy + sell + prev series)...")
    cached, fetched, missing = await fetch_all_ohlc(bp_data)

    print(f"\n  Already cached : {cached}")
    print(f"  Newly fetched  : {fetched}")
    print(f"  Missing/failed : {missing}")

    print("\n  Saving buy_price_data.json...")
    with open(BP_FILE, "w", encoding="utf-8") as f:
        json.dump(bp_data, f, indent=2, ensure_ascii=False)
    print("  Saved.")

    # ── Step 2: populate sold entries ─────────────────────────────────────────
    print("\n[Step 2] Populating sold entries buyPrice / sellPrice...")
    updated = populate_sold_entries(bp_data, portfolios)
    print(f"  {updated} fields updated.")

    print("  Saving portfolios.json...")
    with open(PF_FILE, "w", encoding="utf-8") as f:
        json.dump(portfolios, f, indent=2, ensure_ascii=False)
    print("  Saved.")

    # ── Step 3: regenerate gains_statement.json ───────────────────────────────
    print("\n[Step 3] Regenerating gains_statement.json...")
    gains = compute_all_gains(bp_data)

    total_sell_events = sum(
        len(stk.get("prevSeriesGains", [])) + len(stk.get("currentSeriesGains", []))
        for basket_data in gains.values()
        for stk in basket_data.values()
    )
    computed_gains = sum(
        1
        for basket_data in gains.values()
        for stk in basket_data.values()
        for ev in (stk.get("prevSeriesGains", []) + stk.get("currentSeriesGains", []))
        if ev.get("weightedGainPct") is not None
    )

    with open(GS_FILE, "w", encoding="utf-8") as f:
        json.dump(gains, f, indent=2, ensure_ascii=False)

    print(f"  {total_sell_events} sell events across all baskets")
    print(f"  {computed_gains} have computed weightedGainPct")
    print(f"  gains_statement.json saved.")

    # ── Final summary ─────────────────────────────────────────────────────────
    msc_sold = portfolios.get("Mid_Small_Cap_sold", [])
    null_bp  = sum(1 for e in msc_sold if e.get("buyPrice")  is None)
    null_sp  = sum(1 for e in msc_sold if e.get("sellPrice") is None)
    print(f"\n{'='*60}")
    print(f"Done.")
    print(f"  MSC sold entries still missing buyPrice  : {null_bp}")
    print(f"  MSC sold entries still missing sellPrice : {null_sp}")
    print(f"  Newly fetched OHLC                       : {fetched}")
    print(f"  OHLC pairs unavailable (old stocks)      : {missing}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
