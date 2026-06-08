"""
Detailed FIFO trace for every sold stock — shows exactly which lots are consumed
for every sell event so the logic can be verified manually.
"""
import json, sys, io, datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

_MONTHS = {m: i+1 for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
)}

def _dk(s):
    try:
        p = s.strip().split(); return datetime.date(int(p[2]), _MONTHS[p[1]], int(p[0]))
    except: return datetime.date(1900,1,1)

BASE = Path(__file__).parent
portfolios = json.loads((BASE/"portfolios.json").read_text(encoding="utf-8"))
bp_data    = json.loads((BASE/"buy_price_data.json").read_text(encoding="utf-8"))

BASKETS = [
    "Mid_Small_Cap","Green_Energy","IPO_Basket","Trends_Triology",
    "Techstack","Make_in_India","Consumer_Trends","IPO_Recommendations",
]

def parse_events(s):
    result=[]
    if not s: return result
    for line in s.strip().split("\n"):
        line=line.strip()
        if not line or "*" not in line: continue
        p=line.split("*")
        if len(p)<2: continue
        date=p[0].strip()
        try: w=float(p[1].strip())
        except: continue
        result.append((date,w))
    return sorted(result, key=lambda x: _dk(x[0]))

def fifo_trace(buy_events_str, sell_events_str, buy_ohlc):
    """
    Returns a dict: sell_date -> {weight, consumed_lots:[(lot_date, weight, price)], wavg, note}
    """
    buys  = parse_events(buy_events_str)
    sells = parse_events(sell_events_str)
    if not buys: return {}

    lot_queue = []
    for date, weight in buys:
        price = buy_ohlc.get(date)
        if price is None:
            return {"_error": f"No price for buy date {date}"}
        lot_queue.append([date, weight, price])

    results = {}
    for sell_date, sell_weight in sells:
        remaining = sell_weight
        consumed  = []
        # Work on a snapshot of current queue state
        for lot in lot_queue:
            if remaining <= 1e-9: break
            take = min(lot[1], remaining)
            consumed.append((lot[0], take, lot[2]))
            remaining -= take

        total_w = sum(c[1] for c in consumed)
        wavg = sum(c[1]*c[2] for c in consumed)/total_w if total_w>1e-9 else None
        results[sell_date] = {
            "sell_weight": sell_weight,
            "consumed": consumed,
            "wavg": round(wavg,4) if wavg else None,
        }

        # Now actually consume from lot_queue (modifying in place)
        remaining = sell_weight
        i = 0
        while remaining > 1e-9 and i < len(lot_queue):
            if lot_queue[i][1] <= remaining + 1e-9:
                remaining -= lot_queue[i][1]
                lot_queue[i][1] = 0
                i += 1
            else:
                lot_queue[i][1] -= remaining
                remaining = 0
        lot_queue = [l for l in lot_queue if l[1] > 1e-9]

    return results


# ── Generate full trace for every stock with a sell record ──────────────────
out = []

for basket in BASKETS:
    sold_key  = f"{basket}_sold"
    sold_recs = portfolios.get(sold_key, [])
    bp_basket = bp_data.get(basket, {})

    # Group sold records by code
    by_code = {}
    for rec in sold_recs:
        code = rec.get("nseCode","")
        by_code.setdefault(code,[]).append(rec)

    for code in sorted(by_code.keys()):
        recs = by_code[code]
        stock_bp = bp_basket.get(code, {})
        buy_ohlc = stock_bp.get("buyOHLC",{}) or {}

        prev_buys  = stock_bp.get("prevBuyEvents","")  or ""
        prev_sells = stock_bp.get("prevSellEvents","") or ""
        curr_buys  = stock_bp.get("buyEvents","")      or ""
        curr_sells = stock_bp.get("sellEvents","")     or ""

        # Compute traces for both series
        prev_trace = fifo_trace(prev_buys, prev_sells, buy_ohlc)
        curr_trace = fifo_trace(curr_buys, curr_sells, buy_ohlc)

        out.append(f"\n{'='*90}")
        out.append(f"  BASKET: {basket}   STOCK: {code}")
        out.append(f"{'='*90}")

        # Show buy events
        out.append("  BUY EVENTS (prevBuyEvents):")
        for d,w in parse_events(prev_buys):
            price=buy_ohlc.get(d,"?")
            out.append(f"    {d:15s}  weight={w:.4f}  price={price}")
        if parse_events(curr_buys):
            out.append("  BUY EVENTS (buyEvents / current series):")
            for d,w in parse_events(curr_buys):
                price=buy_ohlc.get(d,"?")
                out.append(f"    {d:15s}  weight={w:.4f}  price={price}")

        out.append("")

        # Show each sell with trace
        for rec in sorted(recs, key=lambda r: _dk(r.get("date",""))):
            sell_date  = rec.get("date","")
            stored_bp  = rec.get("buyPrice")
            action     = rec.get("action","")
            w_sold     = rec.get("weightSold","?")

            # find trace
            trace_info = prev_trace.get(sell_date) or curr_trace.get(sell_date)

            out.append(f"  SELL: {sell_date}  action={action}  weight_sold={w_sold}")
            if trace_info is None:
                out.append(f"    *** SELL DATE NOT FOUND IN EVENTS ***")
                out.append(f"    stored buyPrice = {stored_bp}")
            else:
                out.append(f"    Lots consumed (FIFO oldest-first):")
                for lot_date, lot_w, lot_p in trace_info["consumed"]:
                    out.append(f"      from {lot_date:15s}: {lot_w:.4f} wt @ Rs.{lot_p:.4f}")
                wavg = trace_info["wavg"]
                diff = round(wavg - stored_bp, 4) if (wavg is not None and stored_bp is not None) else "?"
                match = "OK" if (isinstance(diff,float) and abs(diff)<=0.005) else "MISMATCH"
                out.append(f"    FIFO weighted avg buy price = Rs.{wavg}")
                out.append(f"    Stored buyPrice             = Rs.{stored_bp}")
                out.append(f"    Difference                  = {diff}  [{match}]")
            out.append("")

print("\n".join(out))
