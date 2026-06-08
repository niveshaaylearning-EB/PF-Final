"""
Detects all sell events from rebalance_history.json and calculates:
- weightSold: how much weight was sold
- sellPrice: OHLC average on the sell date (Yahoo Finance)
- action: "Partial Sell" or "Wholly Sold"
Updates portfolios.json with {basket}_sold arrays.
"""

import json, time, requests
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
RH_FILE  = BASE / "rebalance_history.json"
PF_FILE  = BASE / "portfolios.json"
BP_FILE  = BASE / "buy_price_data.json"

BASKETS = ['Mid_Small_Cap','Green_Energy','IPO_Basket','Trends_Triology','Techstack','Make_in_India','Consumer_Trends']

def parse_date(s):
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try: return datetime.strptime(s.strip(), fmt)
        except: pass
    return None

def date_to_unix(d):
    return int(d.timestamp())

HEADERS = {"User-Agent": "Mozilla/5.0"}

def fetch_ohlc(symbol, date_obj):
    """Fetch OHLC for symbol.NS on a given date. Returns avg or None."""
    # Try up to 5 business days forward (in case of holidays)
    for offset in range(6):
        d = date_obj + timedelta(days=offset)
        p1 = date_to_unix(d)
        p2 = p1 + 86400
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS?period1={p1}&period2={p2}&interval=1d"
        try:
            r = requests.get(url, headers=HEADERS, timeout=8)
            if r.status_code != 200:
                continue
            j = r.json()
            res = j.get("chart", {}).get("result", [])
            if not res:
                continue
            q = res[0].get("indicators", {}).get("quote", [{}])[0]
            o = q.get("open", [None])[0]
            h = q.get("high", [None])[0]
            l = q.get("low",  [None])[0]
            c = q.get("close",[None])[0]
            if all(v is not None for v in [o, h, l, c]):
                avg = round((o + h + l + c) / 4, 2)
                if offset > 0:
                    print(f"  {symbol} sell date shifted +{offset}d to {d.strftime('%d %b %Y')}")
                return avg
        except Exception as ex:
            print(f"  {symbol} OHLC error: {ex}")
    return None

def detect_sell_events(basket_entries):
    """
    Given list of {date, nseCode, securityName, weight} entries for a basket,
    detect all sell events (weight reductions + stock disappearances).
    Returns list of sell event dicts.
    """
    # Group by stock
    by_stock = {}
    for e in basket_entries:
        code = e["nseCode"]
        if code not in by_stock:
            by_stock[code] = []
        by_stock[code].append(e)

    # Sort each stock's entries by date
    for code in by_stock:
        by_stock[code].sort(key=lambda x: parse_date(x["date"]) or datetime.min)

    # Find latest rebalance date globally
    all_dates = [parse_date(e["date"]) for e in basket_entries if parse_date(e["date"])]
    last_date = max(all_dates) if all_dates else None

    # Get stocks present on last date
    last_date_str = last_date.strftime("%d %b %Y") if last_date else ""
    active_on_last = {e["nseCode"] for e in basket_entries if e.get("date", "").strip() == last_date_str}

    sell_events = []

    for code, entries in by_stock.items():
        sec_name = entries[-1].get("securityName", "")

        # Detect weight reductions between consecutive appearances
        for i in range(1, len(entries)):
            prev = entries[i-1]
            curr = entries[i]
            prev_w = float(prev.get("weight") or 0)
            curr_w = float(curr.get("weight") or 0)
            if curr_w < prev_w - 0.01:  # weight reduced
                sell_events.append({
                    "nseCode":      code,
                    "securityName": sec_name,
                    "date":         curr["date"],
                    "action":       "Partial Sell",
                    "weightSold":   round(prev_w - curr_w, 2),
                    "buyPrice":     None,
                    "sellPrice":    None,
                })

        # Detect wholly sold: stock not in last rebalance
        if code not in active_on_last:
            last_entry = entries[-1]
            last_w = float(last_entry.get("weight") or 0)
            # The sell date = the next rebalance date after last appearance
            # We approximate: the first date in the basket that is AFTER the last appearance
            last_appear = parse_date(last_entry["date"])
            later_dates = sorted([d for d in all_dates if d > last_appear])
            sell_date = later_dates[0] if later_dates else last_appear
            sell_events.append({
                "nseCode":      code,
                "securityName": sec_name,
                "date":         sell_date.strftime("%d %b %Y"),
                "action":       "Wholly Sold",
                "weightSold":   last_w,
                "buyPrice":     None,
                "sellPrice":    None,
            })

    # Sort by date then nseCode
    sell_events.sort(key=lambda x: (parse_date(x["date"]) or datetime.min, x["nseCode"]))
    return sell_events

def main():
    # Backup portfolios.json before overwriting
    backup_dir = BASE.parent / "_backups" / "pre_update"
    backup_dir.mkdir(parents=True, exist_ok=True)
    import shutil; shutil.copy2(PF_FILE, backup_dir / "portfolios.json")
    print(f"Backup saved to {backup_dir / 'portfolios.json'}")

    rh   = json.loads(RH_FILE.read_text(encoding="utf-8"))
    pf   = json.loads(PF_FILE.read_text(encoding="utf-8"))

    for basket in BASKETS:
        print(f"\n{'='*50}")
        print(f"Processing {basket}...")

        entries = rh.get(basket, [])
        if not entries:
            print("  No history.")
            continue

        events = detect_sell_events(entries)
        print(f"  Detected {len(events)} sell events "
              f"({sum(1 for e in events if e['action']=='Wholly Sold')} wholly, "
              f"{sum(1 for e in events if e['action']=='Partial Sell')} partial)")

        # Preserve existing buy prices from current sold array
        existing = {(e.get("nseCode",""), e.get("date","")): e
                    for e in pf.get(f"{basket}_sold", [])}

        # Collect unique (nseCode, date) pairs needing OHLC
        needed = []
        seen = set()
        for ev in events:
            key = (ev["nseCode"], ev["date"])
            if key not in seen:
                seen.add(key)
                needed.append(key)

        print(f"  Fetching OHLC for {len(needed)} (stock, date) pairs...")

        ohlc_cache = {}
        for i, (code, date_str) in enumerate(needed):
            d = parse_date(date_str)
            if not d:
                print(f"  SKIP bad date: {date_str}")
                continue
            key = (code, date_str)
            # Check existing data first
            if key in existing and existing[key].get("sellPrice"):
                ohlc_cache[key] = existing[key]["sellPrice"]
                continue
            price = fetch_ohlc(code, d)
            ohlc_cache[key] = price
            if price:
                print(f"  [{i+1}/{len(needed)}] {code} @ {date_str} = Rs{price}")
            else:
                print(f"  [{i+1}/{len(needed)}] {code} @ {date_str} = MISSING")
            time.sleep(0.15)

        # Assign prices and buy prices to events
        for ev in events:
            key = (ev["nseCode"], ev["date"])
            ev["sellPrice"] = ohlc_cache.get(key)
            # Preserve existing sell price if already had one
            if key in existing and existing[key].get("sellPrice") and not ev["sellPrice"]:
                ev["sellPrice"] = existing[key]["sellPrice"]
            # Get buy price from active stocks list
            active_stock = next((s for s in pf.get(basket, [])
                                 if s.get("nseCode") == ev["nseCode"]), None)
            if active_stock and active_stock.get("buyPrice"):
                ev["buyPrice"] = active_stock["buyPrice"]
            # Preserve from existing if available
            if key in existing and existing[key].get("buyPrice"):
                ev["buyPrice"] = existing[key]["buyPrice"]

        pf[f"{basket}_sold"] = events
        print(f"  Saved {len(events)} sell events for {basket}")

    PF_FILE.write_text(json.dumps(pf, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nDone. portfolios.json updated.")

if __name__ == "__main__":
    main()
