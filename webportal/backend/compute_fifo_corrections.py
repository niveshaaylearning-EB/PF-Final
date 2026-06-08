"""
Compute true FIFO buy prices for all sold records and compare against
what's currently stored in portfolios.json.

Event format: "DATE * WEIGHT\nDATE * WEIGHT\n..."
Prices come from buyOHLC dict {date: price}.

True FIFO: replay all buys then consume lots oldest-first for each sell.
"""
import json, sys, io, datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

_MONTHS = {m: i+1 for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
)}

def _date_key(date_str: str) -> datetime.date:
    """Convert 'DD MMM YYYY' to a datetime.date for proper chronological sorting."""
    try:
        parts = date_str.strip().split()
        d, m, y = int(parts[0]), _MONTHS[parts[1]], int(parts[2])
        return datetime.date(y, m, d)
    except Exception:
        return datetime.date(1900, 1, 1)

BASE = Path(__file__).parent
portfolios = json.loads((BASE / "portfolios.json").read_text(encoding="utf-8"))
bp_data    = json.loads((BASE / "buy_price_data.json").read_text(encoding="utf-8"))

BASKETS = [
    "Mid_Small_Cap", "Green_Energy", "IPO_Basket", "Trends_Triology",
    "Techstack", "Make_in_India", "Consumer_Trends", "IPO_Recommendations",
]

def parse_events(event_str):
    """Parse 'DATE * WEIGHT\\n...' into sorted list of (date, weight)."""
    result = []
    if not event_str:
        return result
    for line in event_str.strip().split("\n"):
        line = line.strip()
        if not line or "*" not in line:
            continue
        parts = line.split("*")
        if len(parts) < 2:
            continue
        date = parts[0].strip()
        try:
            weight = float(parts[1].strip())
        except ValueError:
            continue
        result.append((date, weight))
    return sorted(result, key=lambda x: _date_key(x[0]))

def compute_fifo_for_series(buy_events_str, sell_events_str, buy_ohlc, target_sell_date):
    """
    Compute true FIFO weighted-avg buy price for the sell on target_sell_date.
    Uses buy_events_str (with prices from buy_ohlc) and processes all prior sells first.
    Returns (fifo_price, sell_weight) or (None, None).
    """
    buys  = parse_events(buy_events_str)
    sells = parse_events(sell_events_str)

    if not buys:
        return None, None

    # Build lot queue with prices
    lot_queue = []
    for date, weight in buys:
        price = buy_ohlc.get(date)
        if price is None:
            return None, None
        lot_queue.append([weight, price, date])

    # Find the target sell weight
    target_weight = None
    for date, weight in sells:
        if date == target_sell_date:
            target_weight = weight
            break
    if target_weight is None:
        return None, None

    # Process all sells BEFORE the target date
    target_key = _date_key(target_sell_date)
    prior_sells = [(d, w) for d, w in sells if _date_key(d) < target_key]
    for _, sold_weight in prior_sells:
        remaining = sold_weight
        while remaining > 1e-9 and lot_queue:
            if lot_queue[0][0] <= remaining + 1e-9:
                remaining -= lot_queue[0][0]
                lot_queue.pop(0)
            else:
                lot_queue[0][0] -= remaining
                remaining = 0.0

    # Consume lots for the target sell (FIFO)
    remaining = target_weight
    consumed = []
    for lot in lot_queue:
        if remaining <= 1e-9:
            break
        take = min(lot[0], remaining)
        consumed.append((take, lot[1]))
        remaining -= take

    if not consumed or sum(w for w, _ in consumed) < 1e-9:
        return None, None

    total = sum(w for w, _ in consumed)
    wavg  = sum(w * p for w, p in consumed) / total
    return round(wavg, 4), target_weight


rows = []

for basket in BASKETS:
    sold_key  = f"{basket}_sold"
    sold_recs = portfolios.get(sold_key, [])
    bp_basket = bp_data.get(basket, {})

    for rec in sold_recs:
        code       = rec.get("nseCode", "")
        sell_date  = rec.get("date", "")
        stored_bp  = rec.get("buyPrice")
        action     = rec.get("action", "")
        sec_name   = rec.get("securityName", code)

        if not sell_date:
            continue
        # null stored_bp is treated as 0 for diff calculation — we still want the FIFO price
        is_null_bp = stored_bp is None

        stock_bp = bp_basket.get(code, {})
        buy_ohlc  = stock_bp.get("buyOHLC",  {}) or {}
        sell_ohlc = stock_bp.get("sellOHLC", {}) or {}

        # Determine which series this sell belongs to.
        # If the date appears in prevSellEvents, use prev series.
        # Otherwise use current series.
        prev_buys  = stock_bp.get("prevBuyEvents",  "") or ""
        prev_sells = stock_bp.get("prevSellEvents", "") or ""
        curr_buys  = stock_bp.get("buyEvents",      "") or ""
        curr_sells = stock_bp.get("sellEvents",     "") or ""

        prev_sell_dates = {d for d, _ in parse_events(prev_sells)}
        curr_sell_dates = {d for d, _ in parse_events(curr_sells)}

        if sell_date in prev_sell_dates:
            fifo_bp, _ = compute_fifo_for_series(prev_buys, prev_sells, buy_ohlc, sell_date)
            series = "prev"
        elif sell_date in curr_sell_dates:
            fifo_bp, _ = compute_fifo_for_series(curr_buys, curr_sells, buy_ohlc, sell_date)
            series = "curr"
        else:
            fifo_bp = None
            series  = "?"

        if fifo_bp is None:
            note = f"failed(series={series})"
        else:
            note = ""

        if fifo_bp is not None and stored_bp is not None:
            diff = round(fifo_bp - stored_bp, 4)
        else:
            diff = None

        rows.append({
            "basket": basket, "code": code, "name": sec_name,
            "sell_date": sell_date, "action": action,
            "stored_bp": stored_bp, "fifo_bp": fifo_bp,
            "diff": diff, "is_null": is_null_bp, "note": note,
        })

# ── Summary ──────────────────────────────────────────────────────────────────
changes   = [r for r in rows if r["diff"] is not None and abs(r["diff"]) > 0.005]
null_recs = [r for r in rows if r["is_null"] and r["fifo_bp"] is not None]
correct   = [r for r in rows if r["diff"] is not None and abs(r["diff"]) <= 0.005]
errors    = [r for r in rows if r["fifo_bp"] is None]

print(f"Total sold records : {len(rows)}")
print(f"  Wrong buy price  : {len(changes)}")
print(f"  Null buy price   : {len(null_recs)}")
print(f"  Already correct  : {len(correct)}")
print(f"  Could not compute: {len(errors)}")
print()

# Print wrong-price table
print("=== A) RECORDS WITH WRONG BUY PRICE ===")
hdr = f"{'#':<4} {'Basket':<20} {'Code':<14} {'Name':<30} {'Date':<14} {'Action':<18} {'StoredBP':>10} {'FifoBP':>10} {'Diff':>9}"
print(hdr)
print("-" * len(hdr))
for i, r in enumerate(sorted(changes, key=lambda x: (x["basket"], x["code"], x["sell_date"])), 1):
    diff_sign = f"{r['diff']:+.2f}"
    print(f"{i:<4} {r['basket']:<20} {r['code']:<14} {r['name'][:29]:<30} {r['sell_date']:<14} {r['action']:<18} {r['stored_bp']:>10.2f} {r['fifo_bp']:>10.2f} {diff_sign:>9}")

print()
print("=== B) RECORDS WITH NULL BUY PRICE (to be populated) ===")
hdr2 = f"{'#':<4} {'Basket':<20} {'Code':<14} {'Name':<30} {'Date':<14} {'Action':<18} {'FifoBP':>10}"
print(hdr2)
print("-" * len(hdr2))
for i, r in enumerate(sorted(null_recs, key=lambda x: (x["basket"], x["code"], x["sell_date"])), 1):
    print(f"{i:<4} {r['basket']:<20} {r['code']:<14} {r['name'][:29]:<30} {r['sell_date']:<14} {r['action']:<18} {r['fifo_bp']:>10.4f}")

print()
if errors:
    print("=== COULD NOT COMPUTE (no buy events) ===")
    for r in errors:
        print(f"  {r['basket']:<20} {r['code']:<14} {r['sell_date']:<14} {r['note']}")

# CSV
print()
print("=== CSV: WRONG PRICES ===")
print("basket,code,name,sell_date,action,stored_bp,fifo_bp,diff")
for r in sorted(changes, key=lambda x: (x["basket"], x["code"], x["sell_date"])):
    print(f"{r['basket']},{r['code']},{r['name']},{r['sell_date']},{r['action']},{r['stored_bp']},{r['fifo_bp']},{r['diff']}")
