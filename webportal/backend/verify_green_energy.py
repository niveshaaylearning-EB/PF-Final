import json

with open("buy_price_data.json", "r") as f:
    bpd = json.load(f)
with open("portfolios.json", "r") as f:
    port = json.load(f)

# Build active stock set for Green_Energy
active_codes = set()
for row in port.get("Green_Energy", []):
    code = row.get("nseCode") or row.get("NSECode") or row.get("ticker")
    if code:
        active_codes.add(code)

def parse_events(s):
    result = []
    for line in (s or "").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("*")
        date = parts[0].strip()
        wt   = float(parts[1].strip())
        result.append((date, wt))
    return result

def run_fifo(buy_lots, sell_events):
    remaining = [[d, w, p] for d, w, p in buy_lots]
    for sell_date, sell_wt in sell_events:
        to_sell = sell_wt
        for lot in remaining:
            if to_sell <= 1e-9:
                break
            if lot[1] <= 1e-9:
                continue
            consumed = min(lot[1], to_sell)
            lot[1] -= consumed
            to_sell -= consumed
    return [(d, round(w,6), p) for d, w, p in remaining if w > 1e-5]

def wavg(lots):
    total_w = sum(w for _, w, _ in lots)
    return sum(w * p for _, w, p in lots) / total_w if total_w > 0 else 0

basket_data = bpd.get("Green_Energy", {})

# Stored wAvgBuyPrice from portfolios.json
stored_map = {}
for row in port.get("Green_Energy", []):
    code = row.get("nseCode") or row.get("NSECode") or row.get("ticker")
    if code:
        stored_map[code] = row.get("wAvgBuyPrice")

print(f"\n{'Code':<14} {'Stored':>10} {'Correct':>10} {'Diff':>8}  Status")
print("-" * 90)

affected = []
no_split = []
no_sells = []
missing  = []

for code, stock in sorted(basket_data.items()):
    # Only active stocks
    if code not in active_codes:
        continue

    buy_events  = parse_events(stock.get("buyEvents",""))
    sell_events = parse_events(stock.get("sellEvents",""))
    buy_ohlc    = stock.get("buyOHLC", {})

    if not sell_events:
        no_sells.append(code)
        continue

    # Check all buy prices available
    buy_lots = []
    missing_price = False
    for date, wt in buy_events:
        price = buy_ohlc.get(date)
        if price is None:
            missing_price = True
            break
        buy_lots.append((date, wt, price))

    if missing_price:
        missing.append(code)
        continue

    remaining = run_fifo(buy_lots, sell_events)

    if not remaining:
        continue  # fully sold active? skip

    # Check if any original lot was partially split
    original_lots_dict = {d: w for d, w, p in buy_lots}
    remaining_dict     = {d: w for d, w, p in remaining}
    split_occurred = any(
        abs(remaining_dict.get(d, 0) - original_lots_dict[d]) > 1e-4
        and remaining_dict.get(d, 0) > 1e-5
        for d in original_lots_dict
    )

    correct = round(wavg(remaining), 4)
    stored  = stored_map.get(code)
    diff    = round(correct - stored, 2) if stored is not None else None

    stored_str = f"{stored:>10.2f}" if stored is not None else f"{'N/A':>10}"
    diff_str   = f"{diff:>+8.2f}"   if diff is not None   else f"{'N/A':>8}"

    if split_occurred:
        affected.append((code, buy_lots, remaining, correct, stored, diff))
        print(f"{code:<14} {stored_str} {correct:>10.4f} {diff_str}  SPLIT")
    else:
        no_split.append(code)
        print(f"{code:<14} {stored_str} {correct:>10.4f} {diff_str}  no split (sells exact lots)")

print(f"\n--- Active stocks with no sell events: {no_sells}")
print(f"--- Active stocks sells consume exact lots (no split): {no_split}")
print(f"--- Missing buyOHLC price: {missing}")

print(f"\n\n=== DETAIL: {len(affected)} stocks with FIFO lot splits ===\n")
for code, buy_lots, remaining, correct, stored, diff in affected:
    print(f"\n{code}")
    print(f"  Buy lots:      " + "  |  ".join(f"{d} x{w} @{p}" for d,w,p in buy_lots))
    print(f"  Sell events:   " + "  |  ".join(f"{d} x{w}" for d,w in parse_events(basket_data[code].get("sellEvents",""))))
    print(f"  Remaining:     " + "  |  ".join(f"{d} x{w:.4f} @{p}" for d,w,p in remaining))
    total_w = sum(w for _,w,_ in remaining)
    print(f"  Calc: ({' + '.join(f'{w:.4f}x{p}' for _,w,p in remaining)}) / {total_w:.4f} = {correct}")
    print(f"  Stored: {stored}  |  Diff: {'+' if diff and diff>0 else ''}{diff}")
