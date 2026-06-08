"""
Fix Mid & Small Cap data issues:
1. Populate buyEvents/sellEvents for INTERARCH, MOLDTKPAC, SUPRAJIT (were empty)
2. Fetch buyOHLC + sellOHLC for these stocks and INTELLECT's 13 Apr 2026 sell
3. Clean up duplicate wrong-date entries in Mid_Small_Cap_sold
4. Populate buyPrice/sellPrice on correct-date sold entries
"""

import sys, json, re, time, requests
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).parent
BP_FILE = BASE / "buy_price_data.json"
PF_FILE = BASE / "portfolios.json"

HEADERS = {"User-Agent": "Mozilla/5.0"}

# ─── Correct buy/sell events for the 3 stocks with empty data ───────────────
# Derived from rebalance_history.json using process_msc_rebalances.py logic
# buyEvents: total cumulative weight at each buy event (next trading day)
# sellEvents: delta sold at each sell event (next trading day)

FIXES = {
    "INTERARCH": {
        # Added 22 Oct 2024 @ 3%, increased 22 May 2025 to 4%
        # Partial sell 19 Feb 2026 (4%→2.5%, delta=1.5%), fully removed 12 Apr 2026
        "buyEvents":  "23 Oct 2024 * 3.0\n23 May 2025 * 4.0",
        "sellEvents": "20 Feb 2026 * 1.5\n13 Apr 2026 * 2.5",
    },
    "MOLDTKPAC": {
        # Added 29 Jul 2025 @ 3%
        # Partial sell 19 Feb 2026 (3%→2.5%, delta=0.5%), fully removed 12 Apr 2026
        "buyEvents":  "30 Jul 2025 * 3.0",
        "sellEvents": "20 Feb 2026 * 0.5\n13 Apr 2026 * 2.5",
    },
    "SUPRAJIT": {
        # Added 08 Jan 2026 @ 3%, fully removed 12 Apr 2026
        "buyEvents":  "09 Jan 2026 * 3.0",
        "sellEvents": "13 Apr 2026 * 3.0",
    },
}

# ─── Wrong-date sold entries to remove (created by calc_sell_events.py) ─────
# Format: (nseCode, wrong_date, action) → correct_date
WRONG_DATE_MAP = {
    ("INTELLECT",  "12 Apr 2026", "Wholly Sold"):  "13 Apr 2026",
    ("INTERARCH",  "19 Feb 2026", "Partial Sell"): "20 Feb 2026",
    ("INTERARCH",  "12 Apr 2026", "Wholly Sold"):  "13 Apr 2026",
    ("MOLDTKPAC",  "19 Feb 2026", "Partial Sell"): "20 Feb 2026",
    ("MOLDTKPAC",  "12 Apr 2026", "Wholly Sold"):  "13 Apr 2026",
    ("SUPRAJIT",   "12 Apr 2026", "Wholly Sold"):  "13 Apr 2026",
}


def parse_events(text: str) -> list[tuple[str, float]]:
    events = []
    for line in (text or "").strip().split("\n"):
        parts = re.split(r"[*×]", line.strip())
        if len(parts) == 2:
            try:
                events.append((parts[0].strip(), float(parts[1].strip())))
            except ValueError:
                pass
    return events


def fetch_ohlc(symbol: str, date_str: str) -> float | None:
    """Fetch OHLC avg for symbol.NS on given date. Tries up to 5 trading days forward."""
    dt = datetime.strptime(date_str, "%d %b %Y")
    for offset in range(6):
        d = dt + timedelta(days=offset)
        p1 = int(d.timestamp())
        p2 = p1 + 86400
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
            f"?period1={p1}&period2={p2}&interval=1d"
        )
        try:
            r = requests.get(url, headers=HEADERS, timeout=12)
            if r.status_code != 200:
                continue
            res = r.json().get("chart", {}).get("result", [])
            if not res:
                continue
            q = res[0].get("indicators", {}).get("quote", [{}])[0]
            o = q.get("open",  [None])[0]
            h = q.get("high",  [None])[0]
            lo= q.get("low",   [None])[0]
            c = q.get("close", [None])[0]
            if all(v is not None for v in [o, h, lo, c]):
                avg = round((o + h + lo + c) / 4, 4)
                if offset > 0:
                    print(f"    {symbol} {date_str}: shifted +{offset}d → {d.strftime('%d %b %Y')}")
                return avg
        except Exception as ex:
            print(f"    {symbol} {date_str}: error — {ex}")
        time.sleep(0.1)
    return None


def total_to_delta(buy_events: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Convert total-weight buy events to delta weights (for weighted avg buy price)."""
    sorted_evs = sorted(buy_events, key=lambda e: datetime.strptime(e[0], "%d %b %Y"))
    prev = 0.0
    deltas = []
    for d, total in sorted_evs:
        delta = round(total - prev, 6)
        if delta > 0.001:
            deltas.append((d, delta))
        prev = total
    return deltas


def calc_buy_price(delta_buys: list[tuple[str, float]], buy_ohlc: dict) -> float | None:
    """Weighted average buy price using delta weights."""
    valid = [(d, w) for d, w in delta_buys if buy_ohlc.get(d) is not None]
    if not valid or len(valid) != len(delta_buys):
        return None
    total_w = sum(w for _, w in valid)
    if total_w < 1e-6:
        return None
    return round(sum(w * buy_ohlc[d] for d, w in valid) / total_w, 2)


def main():
    print("Loading data files...")
    with open(BP_FILE, encoding="utf-8") as f:
        bp = json.load(f)
    with open(PF_FILE, encoding="utf-8") as f:
        pf = json.load(f)

    msc_bp   = bp.setdefault("Mid_Small_Cap", {})
    msc_sold = pf.get("Mid_Small_Cap_sold", [])

    # ── STEP 1: Fix buy/sell events for 3 stocks ────────────────────────────
    print("\n[Step 1] Fixing buyEvents/sellEvents for INTERARCH, MOLDTKPAC, SUPRAJIT...")
    for ticker, fix in FIXES.items():
        if ticker not in msc_bp:
            msc_bp[ticker] = {}
        det = msc_bp[ticker]
        old_be = det.get("buyEvents", "")
        old_se = det.get("sellEvents", "")
        det["buyEvents"]  = fix["buyEvents"]
        det["sellEvents"] = fix["sellEvents"]
        if "buyOHLC"  not in det: det["buyOHLC"]  = {}
        if "sellOHLC" not in det: det["sellOHLC"] = {}
        if "prevBuyEvents"  not in det: det["prevBuyEvents"]  = ""
        if "prevSellEvents" not in det: det["prevSellEvents"] = ""
        print(f"  {ticker}: buyEvents set ({len(parse_events(fix['buyEvents']))} events), "
              f"sellEvents set ({len(parse_events(fix['sellEvents']))} events)")

    # ── STEP 2: Fetch OHLC for buy+sell dates of fixed stocks ───────────────
    print("\n[Step 2] Fetching OHLC for buy/sell events...")
    buy_prices_for_sold: dict[str, float] = {}

    for ticker in ["INTERARCH", "MOLDTKPAC", "SUPRAJIT"]:
        det = msc_bp[ticker]
        buy_evs  = parse_events(det["buyEvents"])
        sell_evs = parse_events(det["sellEvents"])

        # Buy OHLC
        buy_ohlc = det.get("buyOHLC", {})
        for date_str, _ in buy_evs:
            if date_str in buy_ohlc:
                print(f"  BUY  {ticker} {date_str}: cached = {buy_ohlc[date_str]}")
                continue
            price = fetch_ohlc(ticker, date_str)
            if price:
                buy_ohlc[date_str] = price
                print(f"  BUY  {ticker} {date_str} = Rs{price}")
            else:
                print(f"  BUY  {ticker} {date_str}: FAILED")
            time.sleep(0.3)
        det["buyOHLC"] = buy_ohlc

        # Sell OHLC
        sell_ohlc = det.get("sellOHLC", {})
        for date_str, _ in sell_evs:
            if date_str in sell_ohlc:
                print(f"  SELL {ticker} {date_str}: cached = {sell_ohlc[date_str]}")
                continue
            price = fetch_ohlc(ticker, date_str)
            if price:
                sell_ohlc[date_str] = price
                print(f"  SELL {ticker} {date_str} = Rs{price}")
            else:
                print(f"  SELL {ticker} {date_str}: FAILED")
            time.sleep(0.3)
        det["sellOHLC"] = sell_ohlc

        # Calculate buy price using delta weights
        delta_buys = total_to_delta(buy_evs)
        bp_val = calc_buy_price(delta_buys, buy_ohlc)
        if bp_val:
            buy_prices_for_sold[ticker] = bp_val
            print(f"  {ticker}: buy price = Rs{bp_val}")

    # ── Also ensure INTELLECT has sellOHLC for 13 Apr 2026 ──────────────────
    intellect = msc_bp.get("INTELLECT", {})
    if intellect:
        sell_ohlc = intellect.get("sellOHLC", {})
        if "13 Apr 2026" not in sell_ohlc:
            price = fetch_ohlc("INTELLECT", "13 Apr 2026")
            if price:
                sell_ohlc["13 Apr 2026"] = price
                intellect["sellOHLC"] = sell_ohlc
                print(f"  INTELLECT 13 Apr 2026 sell OHLC = Rs{price}")
        # Also check other sell event dates
        for date_str in ["11 May 2022"]:
            if date_str not in sell_ohlc:
                price = fetch_ohlc("INTELLECT", date_str)
                if price:
                    sell_ohlc[date_str] = price
                    print(f"  INTELLECT {date_str} sell OHLC = Rs{price}")
                time.sleep(0.3)
        intellect["sellOHLC"] = sell_ohlc
        # Also buy OHLC for INTELLECT current series (09 Mar 2022, 19 Nov 2025)
        buy_ohlc_int = intellect.get("buyOHLC", {})
        for date_str in ["09 Mar 2022", "19 Nov 2025"]:
            if date_str not in buy_ohlc_int:
                price = fetch_ohlc("INTELLECT", date_str)
                if price:
                    buy_ohlc_int[date_str] = price
                    print(f"  INTELLECT {date_str} buy OHLC = Rs{price}")
                time.sleep(0.3)
        intellect["buyOHLC"] = buy_ohlc_int
        msc_bp["INTELLECT"] = intellect

    # ── STEP 3: Save buy_price_data.json ─────────────────────────────────────
    print("\n[Step 3] Saving buy_price_data.json...")
    bp["Mid_Small_Cap"] = msc_bp
    with open(BP_FILE, "w", encoding="utf-8") as f:
        json.dump(bp, f, indent=2, ensure_ascii=False)
    print("  Saved.")

    # ── STEP 4: Fix portfolios.json sold entries ──────────────────────────────
    print("\n[Step 4] Fixing Mid_Small_Cap_sold entries...")

    # Collect sell prices from wrong-date entries to transfer to correct-date entries
    # (only for entries where wrong_date → next_trading_day logic applies, i.e. 12 Apr→13 Apr)
    sell_price_transfer: dict[tuple[str, str], float] = {}
    for (ticker, wrong_date, action), correct_date in WRONG_DATE_MAP.items():
        wrong_entry = next(
            (s for s in msc_sold
             if s.get("nseCode") == ticker
             and s.get("date") == wrong_date
             and s.get("action") == action),
            None,
        )
        if wrong_entry and wrong_entry.get("sellPrice") is not None:
            # Only transfer when the wrong entry fetched the correct date's OHLC
            # (i.e. 12 Apr 2026 is Sunday → calc_sell_events shifted to 13 Apr 2026)
            # For 19 Feb 2026 entries, the sell price is for 19 Feb itself, not 20 Feb
            if wrong_date == "12 Apr 2026":
                sell_price_transfer[(ticker, correct_date)] = wrong_entry["sellPrice"]
                print(f"  Transfer sellPrice {wrong_entry['sellPrice']} from {ticker}/{wrong_date} → {correct_date}")

    # Remove wrong-date entries
    remove_keys = set(WRONG_DATE_MAP.keys())
    original_count = len(msc_sold)
    msc_sold_clean = [
        s for s in msc_sold
        if (s.get("nseCode"), s.get("date"), s.get("action")) not in remove_keys
    ]
    print(f"  Removed {original_count - len(msc_sold_clean)} wrong-date entries")

    # Update correct-date entries with sell prices + buy prices
    for entry in msc_sold_clean:
        ticker = entry.get("nseCode")
        date   = entry.get("date")
        key    = (ticker, date)

        # Transfer sell price from wrong-date entry (only 13 Apr 2026 transfers)
        if key in sell_price_transfer and entry.get("sellPrice") is None:
            entry["sellPrice"] = sell_price_transfer[key]

        # Populate sellPrice from sellOHLC if still missing
        if entry.get("sellPrice") is None and ticker in msc_bp:
            sell_ohlc = msc_bp[ticker].get("sellOHLC", {})
            if date in sell_ohlc:
                entry["sellPrice"] = sell_ohlc[date]
                print(f"  Populated sellPrice for {ticker}/{date} = Rs{sell_ohlc[date]}")

        # Populate buyPrice for newly fixed removed stocks
        if entry.get("buyPrice") is None and ticker in buy_prices_for_sold:
            entry["buyPrice"] = buy_prices_for_sold[ticker]
            print(f"  Populated buyPrice for {ticker}/{date} = Rs{buy_prices_for_sold[ticker]}")

    pf["Mid_Small_Cap_sold"] = msc_sold_clean

    # Save portfolios.json
    with open(PF_FILE, "w", encoding="utf-8") as f:
        json.dump(pf, f, indent=2, ensure_ascii=False)
    print("  portfolios.json saved.")

    print("\nDone. Run calc_all_buy_prices.py next to populate buyPrice for active stocks.")


if __name__ == "__main__":
    main()
