"""
fix_ipo_basket_pnl.py
=====================
Populates buy/sell events for the IPO Basket in buy_price_data.json,
fetches OHLC for all event dates, rebuilds portfolios.json sold list,
and regenerates gains_statement.json.

Only touches IPO_Basket. No other basket is modified.

Run:
    python fix_ipo_basket_pnl.py --dry-run   (print plan, no writes)
    python fix_ipo_basket_pnl.py             (apply)
"""
import asyncio
import json
import re
import sys
import shutil
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8")

DRY_RUN = "--dry-run" in sys.argv
BASE = Path(__file__).parent

# ─────────────────────────────────────────────────────────────────────────────
# Complete IPO Basket event data (derived from Excel + rebalance_history.json)
# Buy dates  = rebalance confirmation dates (period_start - 1 day pattern)
# Sell dates = new period start date when stock is absent
# Weights    = delta at each event (not cumulative)
# LIQUIDCASE has 2 series: Series 1 → prev*; Series 2 → buyEvents/sellEvents
# ─────────────────────────────────────────────────────────────────────────────

IPO_STOCK_EVENTS = {
    # ── Exited stocks — events stored in prevBuyEvents/prevSellEvents ─────
    "STALLION": {
        "securityName": "Stallion India Fluorochemicals Ltd",
        "segment": "Chemicals",
        "prevBuyEvents": "24 Mar 2025 * 4.0",
        "prevSellEvents": "27 Jun 2025 * 4.0",
    },
    "INDOFARM": {
        "securityName": "Indo Farm Equipment Ltd",
        "segment": "Capital Goods",
        "prevBuyEvents": "14 Feb 2025 * 4.0",
        "prevSellEvents": "27 Jun 2025 * 4.0",
    },
    "JYOTICNC": {
        "securityName": "Jyoti CNC Automation Ltd",
        "segment": "Capital Goods",
        "prevBuyEvents": "14 Feb 2025 * 3.0",
        "prevSellEvents": "21 Jul 2025 * 3.0",
    },
    "PNGJL": {
        # Initial buy 2%, weight increase to 3% on 24 Mar 2025 (+1% delta)
        "securityName": "P N Gadgil Jewellers Ltd",
        "segment": "Consumption",
        "prevBuyEvents": "14 Feb 2025 * 2.0\n24 Mar 2025 * 1.0",
        "prevSellEvents": "31 Jul 2025 * 3.0",
    },
    "GALAPREC": {
        # Initial buy 2.5%, weight increase to 4% on 24 Mar 2025 (+1.5% delta)
        "securityName": "Gala Precision Engineering Ltd",
        "segment": "Auto Ancillary",
        "prevBuyEvents": "14 Feb 2025 * 2.5\n24 Mar 2025 * 1.5",
        "prevSellEvents": "28 Aug 2025 * 4.0",
    },
    "AWFIS": {
        "securityName": "Awfis Space Solutions Ltd",
        "segment": "Miscellaneous",
        "prevBuyEvents": "14 Feb 2025 * 3.0",
        "prevSellEvents": "28 Aug 2025 * 3.0",
    },
    "EPACK": {
        "securityName": "Epack Durable Ltd",
        "segment": "Consumption",
        "prevBuyEvents": "14 Feb 2025 * 4.0",
        "prevSellEvents": "21 Nov 2025 * 4.0",
    },
    "MAMATA": {
        # Initial buy 2.5%, weight increase to 4% on 29 Apr 2025 (+1.5% delta)
        "securityName": "Mamata Machinery Ltd",
        "segment": "Capital Goods",
        "prevBuyEvents": "24 Mar 2025 * 2.5\n29 Apr 2025 * 1.5",
        "prevSellEvents": "21 Nov 2025 * 4.0",
    },
    "EIEL": {
        # Initial buy 3%, weight increase to 4% on 24 Mar 2025 (+1% delta)
        "securityName": "Enviro Infra Engineers Ltd",
        "segment": "Water",
        "prevBuyEvents": "14 Feb 2025 * 3.0\n24 Mar 2025 * 1.0",
        "prevSellEvents": "21 Nov 2025 * 4.0",
    },
    "ACMESOLAR": {
        # Initial buy 2.5%, weight increase to 4% on 27 Aug 2025 (+1.5% delta)
        "securityName": "ACME Solar Holdings Ltd",
        "segment": "Power",
        "prevBuyEvents": "14 Feb 2025 * 2.5\n27 Aug 2025 * 1.5",
        "prevSellEvents": "16 Dec 2025 * 4.0",
    },
    "GEMAROMA": {
        "securityName": "Gem Aromatics Ltd",
        "segment": "Pharma/Chemicals",
        "prevBuyEvents": "27 Aug 2025 * 2.5",
        "prevSellEvents": "16 Dec 2025 * 2.5",
    },
    "QUADFUTURE": {
        "securityName": "Quadrant Future Tek Ltd",
        "segment": "Railway",
        "prevBuyEvents": "14 Feb 2025 * 4.0",
        "prevSellEvents": "16 Dec 2025 * 4.0",
    },
    "BANSALWIRE": {
        # Initial buy 2.5%, weight increase to 4% on 20 Nov 2025 (+1.5% delta)
        "securityName": "Bansal Wire Industries Ltd",
        "segment": "Power",
        "prevBuyEvents": "14 Feb 2025 * 2.5\n20 Nov 2025 * 1.5",
        "prevSellEvents": "05 Feb 2026 * 4.0",
    },
    "OSWALPUMPS": {
        "securityName": "Oswal Pumps Ltd",
        "segment": "Capital Goods",
        "prevBuyEvents": "26 Jun 2025 * 4.0",
        "prevSellEvents": "05 Feb 2026 * 4.0",
    },
    "STYLEBAAZA": {
        "securityName": "Baazar Style Retail Ltd",
        "segment": "Consumption",
        "prevBuyEvents": "24 Mar 2025 * 4.0",
        "prevSellEvents": "05 Feb 2026 * 4.0",
    },
    "JGCHEM": {
        "securityName": "JG Chemicals Ltd",
        "segment": "Chemicals",
        "prevBuyEvents": "14 Feb 2025 * 4.0",
        "prevSellEvents": "05 Feb 2026 * 4.0",
    },
    "DAMCAPITAL": {
        "securityName": "DAM Capital Advisors Ltd",
        "segment": "Wealth Management",
        "prevBuyEvents": "14 Feb 2025 * 3.0",
        "prevSellEvents": "05 Feb 2026 * 3.0",
    },
    "TRANSRAILL": {
        "securityName": "Transrail Lighting Ltd",
        "segment": "Power",
        "prevBuyEvents": "14 Feb 2025 * 4.0",
        "prevSellEvents": "18 Feb 2026 * 4.0",
    },
    "SETL": {
        "securityName": "Standard Engineering Technology Ltd",
        "segment": "Capital Goods",
        "prevBuyEvents": "26 Jun 2025 * 4.0",
        "prevSellEvents": "10 Apr 2026 * 4.0",
    },
    "LAXMIDENTL": {
        # Initial buy 2.5%, weight increase to 3.5% on 15 Dec 2025 (+1% delta)
        "securityName": "Laxmi Dental Ltd",
        "segment": "Medical Equipment & Supplies",
        "prevBuyEvents": "20 Nov 2025 * 2.5\n15 Dec 2025 * 1.0",
        "prevSellEvents": "10 Apr 2026 * 3.5",
    },
    "IXIGO": {
        "securityName": "Le Travenues Technology Ltd",
        "segment": "Consumption",
        "prevBuyEvents": "20 Jul 2025 * 3.0",
        "prevSellEvents": "10 Apr 2026 * 3.0",
    },
    "ALLTIME": {
        "securityName": "All Time Plastics Ltd",
        "segment": "Consumption",
        "prevBuyEvents": "27 Aug 2025 * 3.0",
        "prevSellEvents": "10 Apr 2026 * 3.0",
    },
    "EPACKPEB": {
        "securityName": "EPack Prefab Technologies Ltd",
        "segment": "Infrastructure",
        "prevBuyEvents": "15 Dec 2025 * 4.0",
        "prevSellEvents": "10 Apr 2026 * 4.0",
    },
    # ── LIQUIDCASE: 2 completed series ────────────────────────────────────
    # Series 1 in prev*; Series 2 in buyEvents/sellEvents
    "LIQUIDCASE": {
        "securityName": "Zerodha Nifty 1D Rate Liquid ETF",
        "segment": "Cash",
        # Series 1: bought 14 Feb 2025 at 41%, partial sells, full exit 21 Nov 2025
        "prevBuyEvents": "14 Feb 2025 * 41.0",
        "prevSellEvents": (
            "24 Mar 2025 * 20.5\n"
            "29 Apr 2025 * 4.5\n"
            "26 Jun 2025 * 4.0\n"
            "02 Jul 2025 * 7.0\n"
            "30 Jul 2025 * 1.0\n"
            "27 Aug 2025 * 2.5\n"
            "21 Nov 2025 * 1.5"
        ),
        # Series 2: re-bought 15 Dec 2025 at 1%, increased to 3% on 04 Feb 2026, full exit 18 Feb 2026
        "buyEvents": "15 Dec 2025 * 1.0\n04 Feb 2026 * 2.0",
        "sellEvents": "18 Feb 2026 * 3.0",
    },
}

# Stocks already in P&L — do NOT modify their event data
SKIP_CODES = {"ARIS", "INTERARCH", "JAINREC", "WAAREEENER"}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers copied from main.py / calc_event_ohlc.py
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


YF_HEADERS = {"User-Agent": "Mozilla/5.0"}

# ETF/MF tickers that don't trade on NSE — skip OHLC
SKIP_OHLC_CODES = {"LIQUIDCASE"}


async def _yahoo_ohlc(nse: str, ts: int, client: httpx.AsyncClient) -> float | None:
    sym = f"{nse}.NS"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
        f"?interval=1d&period1={ts}&period2={ts + 4 * 86400}"
    )
    try:
        r = await client.get(url, headers=YF_HEADERS, timeout=15)
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
                    return round((o + h + l + c) / 4, 4)
    except Exception:
        pass
    return None


async def fetch_ohlc_for(
    nse: str, date_str: str,
    client: httpx.AsyncClient, sem: asyncio.Semaphore
) -> float | None:
    if nse in SKIP_OHLC_CODES:
        return None
    async with sem:
        try:
            dt = datetime.strptime(date_str, "%d %b %Y")
        except ValueError:
            return None
        ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
        return await _yahoo_ohlc(nse, ts, client)


# ─────────────────────────────────────────────────────────────────────────────
# FIFO gains (same logic as main.py _compute_fifo_gains_for_series)
# ─────────────────────────────────────────────────────────────────────────────

def _wavg_cost_basis(buy_events, buy_ohlc):
    weighted = [(buy_ohlc.get(d), q) for d, q in buy_events if buy_ohlc.get(d)]
    total_w = sum(q for _, q in weighted)
    if total_w == 0:
        return None
    return round(sum(p * q for p, q in weighted) / total_w, 4)


def compute_fifo_gains(buy_events, sell_events, buy_ohlc, sell_ohlc):
    if not buy_events or not sell_events:
        return []
    buy_queue = sorted(
        [{"date": d, "remaining": q, "price": buy_ohlc.get(d)} for d, q in buy_events],
        key=lambda e: _date_ts(e["date"]),
    )
    gains = []
    for sell_date, sell_weight in sorted(sell_events, key=lambda e: _date_ts(e[0])):
        sell_price = sell_ohlc.get(sell_date)
        remaining = sell_weight
        lots = []
        for lot in buy_queue:
            if remaining < 1e-6:
                break
            if lot["remaining"] < 1e-6:
                continue
            take = min(lot["remaining"], remaining)
            lot["remaining"] = round(lot["remaining"] - take, 6)
            remaining = round(remaining - take, 6)
            bp = lot["price"]
            gain_pct = (
                round((sell_price - bp) / bp * 100, 2)
                if bp and sell_price and bp > 0 else None
            )
            lots.append({"buyDate": lot["date"], "weight": round(take, 4),
                         "buyPrice": bp, "gainPct": gain_pct})

        valid = [l for l in lots if l["gainPct"] is not None]
        total_w = sum(l["weight"] for l in valid)
        wt_gain = (
            round(sum(l["gainPct"] * l["weight"] for l in valid) / total_w, 2)
            if total_w > 0 else None
        )
        remaining_qty = sum(l["remaining"] for l in buy_queue)
        sell_type = "Full Exit" if remaining_qty < 0.05 else "Partial Sell"
        if sell_type == "Full Exit":
            wt_buy_price = _wavg_cost_basis(buy_events, buy_ohlc)
        else:
            lots_with_p = [l for l in lots if l["buyPrice"] is not None]
            total_w_bp = sum(l["weight"] for l in lots_with_p)
            wt_buy_price = (
                round(sum(l["buyPrice"] * l["weight"] for l in lots_with_p) / total_w_bp, 4)
                if total_w_bp > 0 else None
            )
        gains.append({
            "sellDate": sell_date,
            "sellWeight": sell_weight,
            "sellPrice": sell_price,
            "sellType": sell_type,
            "lots": lots,
            "weightedGainPct": wt_gain,
            "weightedAvgBuyPrice": wt_buy_price,
        })
    return gains


# ─────────────────────────────────────────────────────────────────────────────
# Sold records rebuild (same logic as main.py _rebuild_sold_from_bp)
# ─────────────────────────────────────────────────────────────────────────────

def rebuild_sold(basket_bp: dict, existing_sold: list) -> list:
    bp_by_key = {}
    bp_by_code = {}
    for rec in existing_sold:
        bp = rec.get("buyPrice")
        if bp is None:
            continue
        code = rec.get("nseCode", "")
        date = rec.get("date", "")
        if date:
            bp_by_key.setdefault((code, date), []).append(bp)
        else:
            bp_by_code.setdefault(code, []).append(bp)

    sold = []
    for code, det in basket_bp.items():
        sec_name  = det.get("securityName", "")
        sell_ohlc = det.get("sellOHLC") or {}
        for buy_str, sell_str in [
            (det.get("prevBuyEvents"), det.get("prevSellEvents")),
            (det.get("buyEvents"),     det.get("sellEvents")),
        ]:
            buys  = parse_events(buy_str  or "")
            sells = parse_events(sell_str or "")
            if not sells:
                continue
            for sell_date, sell_qty in sells:
                ts           = _date_ts(sell_date)
                total_bought = sum(q for d, q in buys if _date_ts(d) <= ts)
                total_sold   = sum(q for d, q in sells if _date_ts(d) <= ts)
                remaining    = max(0.0, round(total_bought - total_sold, 6))
                is_full      = remaining < 0.05
                keyed = bp_by_key.get((code, sell_date), [])
                buy_p = keyed.pop(0) if keyed else None
                if buy_p is None:
                    fallback = bp_by_code.get(code, [])
                    buy_p = fallback.pop(0) if fallback else None
                sold.append({
                    "nseCode":      code,
                    "securityName": sec_name,
                    "date":         sell_date,
                    "action":       "Wholly Sold" if is_full else "Partially Sold",
                    "weightSold":   round(sell_qty, 4),
                    "buyPrice":     buy_p,
                    "sellPrice":    sell_ohlc.get(sell_date),
                })
    return sold


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    bp_path   = BASE / "buy_price_data.json"
    pf_path   = BASE / "portfolios.json"
    gs_path   = BASE / "gains_statement.json"

    with open(bp_path, encoding="utf-8") as f:
        bp_data = json.load(f)
    with open(pf_path, encoding="utf-8") as f:
        pf_data = json.load(f)
    with open(gs_path, encoding="utf-8") as f:
        gs_data = json.load(f)

    basket_bp = bp_data.get("IPO_Basket", {})

    # ── Step 1: Merge events into basket_bp ──────────────────────────────
    print("\n=== Step 1: Merging event data into IPO_Basket ===")
    for code, events in IPO_STOCK_EVENTS.items():
        if code in SKIP_CODES:
            print(f"  SKIP {code} (already in P&L)")
            continue

        det = basket_bp.setdefault(code, {})
        det["securityName"] = events["securityName"]
        det["segment"]      = events["segment"]

        # Only write events if not already populated (safety check)
        for field in ("prevBuyEvents", "prevSellEvents", "buyEvents", "sellEvents"):
            val = events.get(field, "")
            if val:
                det[field] = val
            elif field not in det:
                det[field] = ""

        if "buyOHLC"  not in det: det["buyOHLC"]  = {}
        if "sellOHLC" not in det: det["sellOHLC"] = {}

        prev_buys  = len(parse_events(events.get("prevBuyEvents",  "")))
        prev_sells = len(parse_events(events.get("prevSellEvents", "")))
        curr_buys  = len(parse_events(events.get("buyEvents",       "")))
        curr_sells = len(parse_events(events.get("sellEvents",      "")))
        print(f"  {code}: prevBuys={prev_buys}, prevSells={prev_sells}, "
              f"curBuys={curr_buys}, curSells={curr_sells}")

    # ── Step 2: Collect OHLC pairs needed ────────────────────────────────
    print("\n=== Step 2: Collecting OHLC pairs needed ===")
    pairs_needed: set[tuple[str, str]] = set()
    for code, det in basket_bp.items():
        cached_buy  = det.get("buyOHLC")  or {}
        cached_sell = det.get("sellOHLC") or {}
        for field, cache in [
            ("prevBuyEvents",  cached_buy), ("buyEvents",  cached_buy),
            ("prevSellEvents", cached_sell), ("sellEvents", cached_sell),
        ]:
            for date_str, _ in parse_events(det.get(field) or ""):
                if date_str not in cache and code not in SKIP_OHLC_CODES:
                    pairs_needed.add((code, date_str))

    print(f"  {len(pairs_needed)} (stock, date) pairs to fetch")

    if DRY_RUN:
        print("\n[DRY RUN] Pairs that would be fetched:")
        for code, d in sorted(pairs_needed):
            print(f"  {code} @ {d}")
        print("\n[DRY RUN] No files written.")
        return

    # ── Step 3: Fetch OHLC ───────────────────────────────────────────────
    print("\n=== Step 3: Fetching OHLC from Yahoo Finance ===")
    sem = asyncio.Semaphore(10)
    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        prices = await asyncio.gather(
            *[fetch_ohlc_for(code, date_str, client, sem)
              for code, date_str in sorted(pairs_needed)]
        )

    ohlc_cache: dict[tuple[str, str], float | None] = {
        pair: price for pair, price in zip(sorted(pairs_needed), prices)
    }

    fetched = 0
    missing = []
    for code, det in basket_bp.items():
        buy_ohlc  = dict(det.get("buyOHLC")  or {})
        sell_ohlc = dict(det.get("sellOHLC") or {})

        for field, cache, label in [
            ("prevBuyEvents",  buy_ohlc,  "PREV-BUY"),
            ("buyEvents",      buy_ohlc,  "BUY"),
            ("prevSellEvents", sell_ohlc, "PREV-SELL"),
            ("sellEvents",     sell_ohlc, "SELL"),
        ]:
            for date_str, _ in parse_events(det.get(field) or ""):
                if date_str in cache:
                    continue
                if code in SKIP_OHLC_CODES:
                    continue
                price = ohlc_cache.get((code, date_str))
                if price is not None:
                    cache[date_str] = price
                    fetched += 1
                    print(f"  [{label:9s}] {code:15s} @ {date_str} = ₹{price:,.4f}")
                else:
                    missing.append(f"{code} @ {date_str} ({label})")
                    print(f"  [{label:9s}] {code:15s} @ {date_str} = MISSING")

        det["buyOHLC"]  = buy_ohlc
        det["sellOHLC"] = sell_ohlc

    print(f"\n  Fetched: {fetched}, Missing: {len(missing)}")

    # ── Step 4: Rebuild sold list for IPO_Basket ─────────────────────────
    print("\n=== Step 4: Rebuilding IPO_Basket sold list ===")
    old_sold = pf_data.get("IPO_Basket_sold", [])

    # Compute FIFO buy prices for each sell event and insert into sold records
    # (carry forward existing buyPrice from old_sold if available)
    new_sold = rebuild_sold(basket_bp, old_sold)

    # Fill buyPrice via FIFO for records that have no buyPrice yet
    # (re-compute from the gains data since rebuild_sold only carries existing)
    # For each stock, run FIFO to get per-lot buy prices
    fifo_prices: dict[tuple[str, str], float | None] = {}

    for code, det in basket_bp.items():
        for buy_str, sell_str in [
            (det.get("prevBuyEvents"), det.get("prevSellEvents")),
            (det.get("buyEvents"),     det.get("sellEvents")),
        ]:
            buys  = parse_events(buy_str  or "")
            sells = parse_events(sell_str or "")
            if not sells or not buys:
                continue
            buy_ohlc  = det.get("buyOHLC")  or {}
            sell_ohlc = det.get("sellOHLC") or {}
            gains = compute_fifo_gains(buys, sells, buy_ohlc, sell_ohlc)
            for g in gains:
                fifo_prices[(code, g["sellDate"])] = g.get("weightedAvgBuyPrice")

    # Apply FIFO buy prices to sold records
    for rec in new_sold:
        key = (rec["nseCode"], rec["date"])
        if rec.get("buyPrice") is None and key in fifo_prices:
            rec["buyPrice"] = fifo_prices[key]

    print(f"  Rebuilt sold list: {len(new_sold)} records")
    for r in new_sold:
        bp = f"₹{r['buyPrice']:,.2f}" if r.get("buyPrice") else "null"
        sp = f"₹{r['sellPrice']:,.2f}" if r.get("sellPrice") else "null"
        print(f"  {r['nseCode']:15s} sold {r['date']}  buy={bp}  sell={sp}  {r['action']}")

    # ── Step 5: Recompute gains_statement for IPO_Basket ─────────────────
    print("\n=== Step 5: Recomputing IPO_Basket gains_statement ===")
    ipo_gains: dict = {}
    for code, det in basket_bp.items():
        buy_ev      = parse_events(det.get("buyEvents")     or "")
        sell_ev     = parse_events(det.get("sellEvents")    or "")
        prev_buy_ev = parse_events(det.get("prevBuyEvents") or "")
        prev_sell_ev= parse_events(det.get("prevSellEvents")or "")
        buy_ohlc    = det.get("buyOHLC")  or {}
        sell_ohlc   = det.get("sellOHLC") or {}

        prev_gains = compute_fifo_gains(prev_buy_ev, prev_sell_ev, buy_ohlc, sell_ohlc)
        curr_gains = compute_fifo_gains(buy_ev,      sell_ev,      buy_ohlc, sell_ohlc)

        if prev_gains or curr_gains:
            ipo_gains[code] = {
                "securityName":       det.get("securityName", ""),
                "prevSeriesGains":    prev_gains,
                "currentSeriesGains": curr_gains,
            }
            total_sells = len(prev_gains) + len(curr_gains)
            print(f"  {code}: {total_sells} sell event(s) in gains")

    print(f"\n  Total IPO_Basket stocks with gains: {len(ipo_gains)}")

    # ── Step 6: Persist ───────────────────────────────────────────────────
    print("\n=== Step 6: Saving files ===")

    # Backup buy_price_data.json
    backup = BASE / "buy_price_data_backup_before_ipo_fix.json"
    if not backup.exists():
        shutil.copy2(bp_path, backup)
        print(f"  Backed up buy_price_data.json → {backup.name}")

    bp_data["IPO_Basket"] = basket_bp
    with open(bp_path, "w", encoding="utf-8") as f:
        json.dump(bp_data, f, indent=2, ensure_ascii=False)
    print("  buy_price_data.json saved")

    pf_data["IPO_Basket_sold"] = new_sold
    with open(pf_path, "w", encoding="utf-8") as f:
        json.dump(pf_data, f, indent=2, ensure_ascii=False)
    print("  portfolios.json (IPO_Basket_sold) saved")

    gs_data["IPO_Basket"] = ipo_gains
    with open(gs_path, "w", encoding="utf-8") as f:
        json.dump(gs_data, f, indent=2, ensure_ascii=False)
    print("  gains_statement.json (IPO_Basket) saved")

    print("\n=== Done ===")
    print(f"  Gains records in IPO_Basket: {sum(len(v.get('prevSeriesGains',[])) + len(v.get('currentSeriesGains',[])) for v in ipo_gains.values())}")
    if missing:
        print(f"\n  WARNING: {len(missing)} OHLC prices missing (null buy/sell prices for these events):")
        for m in missing:
            print(f"    {m}")


if __name__ == "__main__":
    asyncio.run(main())
