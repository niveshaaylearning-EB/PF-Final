import json, copy

with open("buy_price_data.json", "r") as f:
    bpd = json.load(f)
with open("portfolios.json", "r") as f:
    port = json.load(f)

ACTIVE_BASKETS = [
    "Mid_Small_Cap", "Green_Energy", "IPO_Basket",
    "Trends_Triology", "Techstack", "Make_in_India", "Consumer_Trends"
]

def parse_events(s):
    result = []
    for line in (s or "").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("*")
        result.append((parts[0].strip(), float(parts[1].strip())))
    return result

def run_fifo(buy_lots, sell_events):
    remaining = [[d, w, p] for d, w, p in buy_lots]
    for _, sell_wt in sell_events:
        to_sell = sell_wt
        for lot in remaining:
            if to_sell <= 1e-9:
                break
            if lot[1] <= 1e-9:
                continue
            consumed = min(lot[1], to_sell)
            lot[1] -= consumed
            to_sell -= consumed
    return [(d, w, p) for d, w, p in remaining if w > 1e-5]

def wavg_buyprice(lots):
    total_w = sum(w for _, w, _ in lots)
    return round(sum(w * p for _, w, p in lots) / total_w, 4) if total_w > 0 else None

updated = []
skipped = []

for basket in ACTIVE_BASKETS:
    basket_bpd  = bpd.get(basket, {})
    basket_rows = port.get(basket, [])

    for row in basket_rows:
        code = row.get("nseCode")
        if not code or code not in basket_bpd:
            skipped.append(f"{basket}/{code} — not in buy_price_data")
            continue

        stock       = basket_bpd[code]
        buy_events  = parse_events(stock.get("buyEvents", ""))
        sell_events = parse_events(stock.get("sellEvents", ""))
        buy_ohlc    = stock.get("buyOHLC", {})

        if not buy_events:
            skipped.append(f"{basket}/{code} — no buy events")
            continue
        if not sell_events:
            skipped.append(f"{basket}/{code} — no sell events, buy price unchanged")
            continue

        buy_lots = []
        missing = False
        for date, wt in buy_events:
            price = buy_ohlc.get(date)
            if price is None:
                missing = True
                break
            buy_lots.append((date, wt, price))

        if missing:
            skipped.append(f"{basket}/{code} — missing buyOHLC price")
            continue

        remaining = run_fifo(buy_lots, sell_events)
        if not remaining:
            skipped.append(f"{basket}/{code} — no remaining lots after FIFO (fully sold?)")
            continue

        correct = wavg_buyprice(remaining)
        old_price = row.get("buyPrice")

        if correct is not None and abs((correct - old_price) if old_price else 999) > 0.005:
            row["buyPrice"] = correct
            updated.append(f"{basket:<18} {code:<14} {old_price:>10.4f}  ->  {correct:>10.4f}  (diff {correct-old_price:+.4f})")

print(f"Updated {len(updated)} stocks:\n")
for u in updated:
    print(" ", u)

print(f"\nSkipped {len(skipped)} stocks:")
for s in skipped:
    print(" ", s)

with open("portfolios.json", "w") as f:
    json.dump(port, f, indent=2)

print(f"\nportfolios.json written successfully.")
