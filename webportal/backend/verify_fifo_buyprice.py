import json

with open("buy_price_data.json", "r") as f:
    bpd = json.load(f)
with open("portfolios.json", "r") as f:
    port = json.load(f)

# Stocks to check: basket -> [nse_codes]
TARGETS = {
    "Mid_Small_Cap":   ["IMFA","HBLENGINE","SBCL","KRN","GANECOS"],
    "Green_Energy":    ["POCL","ATHERENERG","GANECOS","HBLENGINE","SBCL","FIEMIND","VEDL","SKIPPER","POWERMECH","JASH"],
    "Trends_Triology": ["ATHERENERG","E2E","EBGNG","SAMBHV"],
    "Techstack":       ["GENESYS"],
    "Make_in_India":   ["CENTUM","AARTIPHARM","KDDL"],
    "Consumer_Trends": ["ATHERENERG","PICCADIL"],
}

def parse_events(s):
    result = []
    for line in s.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("*")
        date = parts[0].strip()
        wt   = float(parts[1].strip())
        result.append((date, wt))
    return result

def run_fifo(buy_lots, sell_events):
    # buy_lots: list of [date, remaining_wt, price]
    remaining = [[d, w, p] for d, w, p in buy_lots]
    for sell_date, sell_wt in sell_events:
        to_sell = sell_wt
        for lot in remaining:
            if to_sell <= 0:
                break
            if lot[1] <= 0:
                continue
            consumed = min(lot[1], to_sell)
            lot[1] -= consumed
            to_sell -= consumed
    return [(d, w, p) for d, w, p in remaining if w > 1e-6]

print(f"{'Basket':<18} {'Code':<12} {'Stored':>10} {'Correct':>10} {'Diff':>8}  Remaining lots")
print("-" * 110)

for basket, codes in TARGETS.items():
    basket_data = bpd.get(basket, {})
    for code in codes:
        if code not in basket_data:
            print(f"{basket:<18} {code:<12} {'NOT FOUND':>10}")
            continue
        stock = basket_data[code]
        buy_events  = parse_events(stock.get("buyEvents",""))
        sell_events = parse_events(stock.get("sellEvents",""))
        buy_ohlc    = stock.get("buyOHLC", {})

        if not sell_events:
            continue  # no sells, skip

        # Build buy lots with prices
        buy_lots = []
        missing_price = False
        for date, wt in buy_events:
            price = buy_ohlc.get(date)
            if price is None:
                missing_price = True
                break
            buy_lots.append((date, wt, price))

        if missing_price:
            print(f"{basket:<18} {code:<12} {'NO PRICE':>10}")
            continue

        remaining = run_fifo(buy_lots, sell_events)

        if not remaining:
            continue  # fully sold

        total_wt = sum(w for _, w, _ in remaining)
        wavg = sum(w * p for _, w, p in remaining) / total_wt

        # Get stored wAvgBuyPrice from portfolios.json
        active_key = basket
        stored = None
        if active_key in port:
            for row in port[active_key]:
                if row.get("nseCode") == code:
                    stored = row.get("wAvgBuyPrice")
                    break

        diff = round(wavg - stored, 2) if stored is not None else None
        stored_str = f"{stored:>10.2f}" if stored is not None else f"{'N/A':>10}"
        diff_str   = f"{diff:>+8.2f}" if diff is not None else f"{'N/A':>8}"
        lots_str = "  |  ".join(f"{d} ×{w:.4f} @{p}" for d, w, p in remaining)
        print(f"{basket:<18} {code:<12} {stored_str} {wavg:>10.4f} {diff_str}  [{lots_str}]")
