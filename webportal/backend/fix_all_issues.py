"""
fix_all_issues.py

1. Convert MSC buyEvents from cumulative-weight to delta-weight format
   (all other baskets already use delta; this makes everything consistent)
2. Populate securityName in buy_price_data.json for ALL baskets from rebalance_history.json
3. Remove 0-allocation stocks from portfolios.json active arrays
4. Populate securityName in portfolios.json active stocks
5. Regenerate gains_statement.json (without the now-unnecessary _total_to_delta step)
6. Report any remaining duplicates

Run:  python fix_all_issues.py
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE    = Path(__file__).parent
BP_FILE = BASE / "buy_price_data.json"
PF_FILE = BASE / "portfolios.json"
RH_FILE = BASE / "rebalance_history.json"
GS_FILE = BASE / "gains_statement.json"

BASKETS = [
    "Mid_Small_Cap", "Green_Energy", "IPO_Basket",
    "Trends_Triology", "Techstack", "Make_in_India", "Consumer_Trends",
]


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


def fmt_events(events: list[tuple[str, float]]) -> str:
    return "\n".join(f"{d} * {round(w, 6)}" for d, w in events)


def _ts(date_str: str) -> int:
    try:
        return int(datetime.strptime(date_str.strip(), "%d %b %Y")
                   .replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return 0


def cumulative_to_delta(
    buy_events: list[tuple[str, float]],
    sell_events: list[tuple[str, float]],
) -> list[tuple[str, float]]:
    """Convert cumulative-weight buyEvents to delta weights.
    Sell events (already delta) reduce the running weight so re-buys are
    computed correctly against the actual holding at that point in time."""
    combined = (
        [(d, "buy",  q) for d, q in buy_events] +
        [(d, "sell", q) for d, q in sell_events]
    )
    combined.sort(key=lambda e: _ts(e[0]))
    cw, result = 0.0, []
    for date_str, etype, qty in combined:
        if etype == "buy":
            delta = qty - cw
            if delta > 0.001:
                result.append((date_str, round(delta, 6)))
            cw = qty
        else:
            cw = max(0.0, cw - qty)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# FIFO gains (delta format only — no cumulative-to-delta conversion needed)
# ─────────────────────────────────────────────────────────────────────────────

def _fifo_gains(buy_ev, sell_ev, buy_ohlc, sell_ohlc):
    if not buy_ev or not sell_ev:
        return []
    buy_q = sorted(
        [{"date": d, "remaining": q, "price": buy_ohlc.get(d)} for d, q in buy_ev],
        key=lambda e: _ts(e["date"]),
    )
    gains = []
    for sell_date, sell_weight in sorted(sell_ev, key=lambda e: _ts(e[0])):
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
            gp = round((sell_price - bp) / bp * 100, 2) if (bp and sell_price and bp > 0) else None
            lots.append({"buyDate": lot["date"], "weight": round(take, 4),
                         "buyPrice": bp, "gainPct": gp})
        valid   = [l for l in lots if l["gainPct"] is not None]
        total_w = sum(l["weight"] for l in valid)
        wt_gain = round(sum(l["gainPct"] * l["weight"] for l in valid) / total_w, 2) if total_w > 0 else None
        gains.append({
            "sellDate": sell_date, "sellWeight": sell_weight, "sellPrice": sell_price,
            "lots": lots, "weightedGainPct": wt_gain,
        })
    return gains


def compute_all_gains(bp_data: dict) -> dict:
    """All baskets use delta-weight buyEvents after this script runs."""
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

            # Both current and prev series are now in delta format
            prev_gains = _fifo_gains(prev_buy_ev, prev_sel_ev, buy_ohlc, sell_ohlc)
            curr_gains = _fifo_gains(buy_ev,      sell_ev,     buy_ohlc, sell_ohlc)

            if prev_gains or curr_gains:
                basket_result[nse] = {
                    "securityName":       det.get("securityName", ""),
                    "prevSeriesGains":    prev_gains,
                    "currentSeriesGains": curr_gains,
                }
        if basket_result:
            result[basket] = basket_result
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("Loading data files...")
    with open(BP_FILE, encoding="utf-8") as f:
        bp_data = json.load(f)
    with open(PF_FILE, encoding="utf-8") as f:
        portfolios = json.load(f)
    with open(RH_FILE, encoding="utf-8") as f:
        rh_data = json.load(f)

    # ── Build name lookup from rebalance_history ──────────────────────────────
    name_lookup: dict[str, str] = {}  # nseCode → securityName
    for basket in BASKETS:
        for entry in rh_data.get(basket, []):
            code = entry.get("nseCode", "").strip()
            name = entry.get("securityName", "").strip()
            if code and name and not name_lookup.get(code):
                name_lookup[code] = name
    print(f"  Built name lookup: {len(name_lookup)} unique stocks")

    # ── Step 1: Convert MSC buyEvents cumulative → delta ──────────────────────
    print("\n[Step 1] Converting MSC buyEvents from cumulative to delta format...")
    msc_bp = bp_data.get("Mid_Small_Cap", {})
    converted = 0
    skipped   = 0

    for nse, det in msc_bp.items():
        buy_ev  = parse_events(det.get("buyEvents")  or "")
        sell_ev = parse_events(det.get("sellEvents") or "")

        if not buy_ev:
            skipped += 1
            continue

        # Detect if already in delta format:
        # Cumulative format has at least one entry where qty >= previous entry's qty,
        # BUT can also have decreases after partial sells.
        # Reliable detection: apply the conversion; if result differs, it was cumulative.
        delta_ev = cumulative_to_delta(buy_ev, sell_ev)

        if delta_ev == buy_ev:
            # Already delta (or single event = same either way)
            skipped += 1
        else:
            det["buyEvents"] = fmt_events(delta_ev)
            converted += 1
            print(f"  {nse}: {len(buy_ev)} events → delta format")

    print(f"  Converted: {converted}, already-delta/skipped: {skipped}")

    # ── Step 2: Populate securityName in buy_price_data.json ─────────────────
    print("\n[Step 2] Populating securityName in buy_price_data.json...")
    names_added = 0
    for basket in BASKETS:
        basket_bp = bp_data.get(basket, {})
        for nse, det in basket_bp.items():
            if not det.get("securityName") and name_lookup.get(nse):
                det["securityName"] = name_lookup[nse]
                names_added += 1
    print(f"  Added {names_added} security names")

    # ── Step 3: Remove 0-allocation stocks from portfolios.json ──────────────
    print("\n[Step 3] Removing 0-allocation stocks from portfolios.json...")
    for basket in BASKETS:
        original = portfolios.get(basket, [])
        cleaned  = [s for s in original if (s.get("allocation") or 0) > 0]
        removed  = len(original) - len(cleaned)
        if removed:
            portfolios[basket] = cleaned
            print(f"  {basket}: removed {removed} zero-allocation stock(s)")

    # ── Step 4: Populate securityName in portfolios.json active stocks ────────
    print("\n[Step 4] Populating securityName in portfolios.json...")
    names_pf = 0
    for basket in BASKETS:
        for stk in portfolios.get(basket, []):
            if not stk.get("securityName") and name_lookup.get(stk.get("nseCode", "")):
                stk["securityName"] = name_lookup[stk["nseCode"]]
                names_pf += 1
    print(f"  Added {names_pf} security names to active stocks")

    # ── Step 5: Save files ────────────────────────────────────────────────────
    print("\n[Step 5] Saving buy_price_data.json and portfolios.json...")
    with open(BP_FILE, "w", encoding="utf-8") as f:
        json.dump(bp_data, f, indent=2, ensure_ascii=False)
    with open(PF_FILE, "w", encoding="utf-8") as f:
        json.dump(portfolios, f, indent=2, ensure_ascii=False)
    print("  Saved.")

    # ── Step 6: Regenerate gains_statement.json ───────────────────────────────
    print("\n[Step 6] Regenerating gains_statement.json...")
    gains = compute_all_gains(bp_data)
    total_ev = sum(
        len(s.get("prevSeriesGains", [])) + len(s.get("currentSeriesGains", []))
        for basket_data in gains.values()
        for s in basket_data.values()
    )
    computed  = sum(
        1
        for basket_data in gains.values()
        for s in basket_data.values()
        for ev in (s.get("prevSeriesGains", []) + s.get("currentSeriesGains", []))
        if ev.get("weightedGainPct") is not None
    )
    with open(GS_FILE, "w", encoding="utf-8") as f:
        json.dump(gains, f, indent=2, ensure_ascii=False)
    print(f"  {total_ev} sell events, {computed} with computed gain% ({round(computed/total_ev*100) if total_ev else 0}%)")
    print("  gains_statement.json saved.")

    # ── Step 7: Check for duplicates in portfolios.json active stocks ─────────
    print("\n[Step 7] Checking for duplicates in portfolios.json active stocks...")
    for basket in BASKETS:
        codes = [s.get("nseCode") for s in portfolios.get(basket, [])]
        dupes = [c for c in set(codes) if codes.count(c) > 1]
        if dupes:
            print(f"  {basket}: DUPLICATE NSE codes found: {dupes}")
        else:
            print(f"  {basket}: no duplicates ✓")

    print(f"\n{'='*60}")
    print("Done. Next: run calc_all_buy_prices.py to recalculate buy prices")
    print("with correct delta weights.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
