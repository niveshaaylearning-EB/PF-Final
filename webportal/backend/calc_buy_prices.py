"""
Calculates buy prices for sold stock events in portfolios.json.
Buy price = OHLC average (open+high+low+close)/4 on the last rebalance date
before the sell event (i.e., the date when the stock was last in the basket
before being partially or wholly sold).
"""

import json, time, requests
from datetime import datetime, timedelta
from pathlib import Path

BASE    = Path(__file__).parent
RH_FILE = BASE / "rebalance_history.json"
PF_FILE = BASE / "portfolios.json"

BASKETS = ['Mid_Small_Cap','Green_Energy','IPO_Basket','Trends_Triology','Techstack','Make_in_India','Consumer_Trends']

HEADERS = {"User-Agent": "Mozilla/5.0"}

def parse_date(s):
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try: return datetime.strptime(s.strip(), fmt)
        except: pass
    return None

def fetch_ohlc(symbol, date_obj):
    """Fetch OHLC average for symbol.NS on given date. Tries up to 5 forward days for holidays."""
    for offset in range(6):
        d = date_obj + timedelta(days=offset)
        p1 = int(d.timestamp())
        p2 = p1 + 86400
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
               f"?period1={p1}&period2={p2}&interval=1d")
        try:
            r = requests.get(url, headers=HEADERS, timeout=8)
            if r.status_code != 200:
                continue
            res = r.json().get("chart", {}).get("result", [])
            if not res:
                continue
            q = res[0].get("indicators", {}).get("quote", [{}])[0]
            o, h, l, c = q.get("open",[None])[0], q.get("high",[None])[0], q.get("low",[None])[0], q.get("close",[None])[0]
            if all(v is not None for v in [o, h, l, c]):
                return round((o + h + l + c) / 4, 2)
        except Exception as ex:
            print(f"  {symbol} OHLC error: {ex}")
    return None

def get_buy_date(rh_entries, nse_code, sell_date_obj):
    """
    Last rebalance date for nse_code that is strictly before sell_date_obj.
    Works for both Partial Sell (sell_date = rebalance date of weight drop)
    and Wholly Sold (sell_date = next rebalance after disappearance).
    """
    dates = sorted(
        [parse_date(e["date"]) for e in rh_entries
         if e.get("nseCode") == nse_code and parse_date(e["date"]) is not None
         and parse_date(e["date"]) < sell_date_obj],
        reverse=True
    )
    return dates[0] if dates else None

def main():
    # Backup portfolios.json before overwriting
    backup_dir = BASE.parent / "_backups" / "pre_update"
    backup_dir.mkdir(parents=True, exist_ok=True)
    import shutil; shutil.copy2(PF_FILE, backup_dir / "portfolios.json")
    print(f"Backup saved to {backup_dir / 'portfolios.json'}")

    rh = json.loads(RH_FILE.read_text(encoding="utf-8"))
    pf = json.loads(PF_FILE.read_text(encoding="utf-8"))

    total_fetched = total_missing = 0

    for basket in BASKETS:
        print(f"\n{'='*50}\n{basket}")
        sold = pf.get(f"{basket}_sold", [])
        entries = rh.get(basket, [])
        if not sold:
            print("  No sold events.")
            continue

        # Collect unique (nseCode, buy_date) pairs that need fetching
        needed = {}  # (nseCode, date_str) -> buy_date_obj
        for ev in sold:
            sell_d = parse_date(ev["date"])
            if not sell_d:
                continue
            buy_d = get_buy_date(entries, ev["nseCode"], sell_d)
            if not buy_d:
                continue
            key = (ev["nseCode"], buy_d.strftime("%d %b %Y"))
            if key not in needed:
                needed[key] = buy_d

        print(f"  {len(sold)} events (recalculating all buy prices)")
        print(f"  Fetching OHLC for {len(needed)} unique (stock, buy_date) pairs...")

        # Fetch OHLC for all needed pairs
        ohlc_cache = {}
        for i, ((code, date_str), date_obj) in enumerate(needed.items()):
            price = fetch_ohlc(code, date_obj)
            ohlc_cache[(code, date_str)] = price
            status = f"Rs{price}" if price else "MISSING"
            print(f"  [{i+1}/{len(needed)}] {code} @ {date_str} = {status}")
            time.sleep(0.15)

        # Assign buy prices to events
        assigned = 0
        for ev in sold:
            sell_d = parse_date(ev["date"])
            if not sell_d:
                continue
            buy_d = get_buy_date(entries, ev["nseCode"], sell_d)
            if not buy_d:
                continue
            key = (ev["nseCode"], buy_d.strftime("%d %b %Y"))
            price = ohlc_cache.get(key)
            if price:
                ev["buyPrice"] = price
                assigned += 1
            else:
                total_missing += 1

        total_fetched += assigned
        print(f"  Assigned buyPrice to {assigned} events")
        pf[f"{basket}_sold"] = sold

    PF_FILE.write_text(json.dumps(pf, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nDone. {total_fetched} buy prices filled, {total_missing} still missing.")

if __name__ == "__main__":
    main()
