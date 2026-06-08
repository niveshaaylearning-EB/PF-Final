"""
fix_buy_events.py
=================
Fixes incorrect buyEvents in buy_price_data.json.

Problems fixed:
  REMOVE     - spurious duplicate events (weight unchanged from prev, no real buy)
  CHANGE     - cumulative weight stored instead of delta (e.g., 6.0 stored, should be 2.0)
  WRONG-TYPE - weight *decrease* stored as buy event (should be in sellEvents)

Algorithm:
  For each buy event date D with stored weight Q:
    1. Build stock's weight history from rebalance_history.json
    2. Detect series boundary from prevSellEvents (restrict history lookups to after boundary)
    3. Find prev_w = last known weight BEFORE D (in history, restricted by boundary)
    4. If weight at D in history == prev_w AND days_gap <= 2:
         → consecutive-day duplicate pattern (e.g., rebalance date + confirmation date)
         → find pre_block_w (weight before the same-weight block)
         → expected_delta = hist_weight_at_D - pre_block_w
         → if expected_delta <= 0: REMOVE (truly spurious)
         → elif |Q - expected_delta| > 0.05: CHANGE (wrong amount)
    5. Elif weight at D in history == prev_w AND days_gap > 2:
         → REMOVE (spurious, not a real buy)
    6. Elif hist_weight_at_D > prev_w + 0.001:
         → expected_delta = hist_weight_at_D - prev_w
         → if |Q - expected_delta| > 0.05: CHANGE
    7. Elif hist_weight_at_D < prev_w - 0.001:
         → this is a sell event, WRONG-TYPE: remove from buyEvents, add delta to sellEvents

Run:
  python fix_buy_events.py --dry-run   (print changes, don't write)
  python fix_buy_events.py             (apply changes and save)
"""

import json
import re
import sys
import shutil
from datetime import datetime, date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DRY_RUN = "--dry-run" in sys.argv

BASE      = Path(__file__).parent
BP_FILE   = BASE / "buy_price_data.json"
RH_FILE   = BASE / "rebalance_history.json"
BACKUP    = BASE / "buy_price_data_backup_before_fix_buy_events.json"

BASKETS = [
    "Mid_Small_Cap", "Green_Energy", "IPO_Basket",
    "Trends_Triology", "Techstack", "Make_in_India", "Consumer_Trends",
]

MON = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
       "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}


def _date_key(s: str) -> date:
    parts = s.strip().split()
    if len(parts) != 3:
        return date.min
    try:
        return date(int(parts[2]), MON.get(parts[1], 0), int(parts[0]))
    except Exception:
        return date.min


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
    sorted_ev = sorted(events, key=lambda e: _date_key(e[0]))
    return "\n".join(f"{d} * {round(w, 6):g}" for d, w in sorted_ev)


def build_history_map(rh_data: dict, basket: str, code: str) -> dict[date, float]:
    """Returns {date: weight} from rebalance_history for this stock."""
    hist: dict[date, float] = {}
    for entry in rh_data.get(basket, []):
        if entry.get("nseCode", "").strip() == code:
            d = _date_key(entry.get("date", ""))
            if d != date.min:
                hist[d] = float(entry.get("weight", 0))
    return hist


def main():
    print("Loading data...")
    with open(BP_FILE, encoding="utf-8") as f:
        bp = json.load(f)
    with open(RH_FILE, encoding="utf-8") as f:
        rh = json.load(f)

    all_changes: list[tuple] = []  # (action, basket, code, date_str, old_qty, new_qty, extra)

    for basket in BASKETS:
        basket_bp = bp.get(basket, {})
        for code, det in basket_bp.items():
            buy_ev  = parse_events(det.get("buyEvents")  or "")
            sell_ev = parse_events(det.get("sellEvents") or "")

            if not buy_ev:
                continue

            # Series boundary: last sell date in prevSellEvents
            prev_sell_ev = parse_events(det.get("prevSellEvents") or "")
            if prev_sell_ev:
                boundary = max(_date_key(d) for d, _ in prev_sell_ev)
            else:
                boundary = None

            # Build history map for this stock
            hist_map = build_history_map(rh, basket, code)
            if not hist_map:
                continue

            # Sorted history dates restricted after boundary (if any)
            if boundary:
                hist_dates = sorted(d for d in hist_map if d > boundary)
            else:
                hist_dates = sorted(hist_map.keys())

            new_buy_ev  = list(buy_ev)   # will be mutated
            new_sell_ev = list(sell_ev)  # may gain entries (WRONG-TYPE)
            changed = False

            for evt_date_str, stored_qty in list(buy_ev):
                evt_d = _date_key(evt_date_str)
                if evt_d == date.min:
                    continue

                # Find weight AT this event date in history
                hist_w_at_d = hist_map.get(evt_d)
                if hist_w_at_d is None:
                    # Date not found in history - skip (cannot determine)
                    continue

                # Get prev_w = last weight before evt_d in (boundary-restricted) history
                prev_entries = [(d, hist_map[d]) for d in hist_dates if d < evt_d]
                if prev_entries:
                    prev_w = prev_entries[-1][1]
                    days_gap = (evt_d - prev_entries[-1][0]).days
                else:
                    prev_w = 0.0
                    # Check if there are any history dates BEFORE boundary for this date
                    all_prev = [(d, hist_map[d]) for d in sorted(hist_map.keys()) if d < evt_d]
                    if all_prev and boundary and all_prev[-1][0] <= boundary:
                        # Right after series reset — first appearance post-boundary
                        days_gap = 999
                    else:
                        days_gap = 999

                # ── Case 1: history weight unchanged from prev ────────────────
                if abs(hist_w_at_d - prev_w) < 0.001:
                    if days_gap <= 2:
                        # Consecutive-day pattern: find weight before the same-weight block
                        pre_block_w = 0.0
                        for j in range(len(prev_entries) - 1, -1, -1):
                            if abs(prev_entries[j][1] - hist_w_at_d) >= 0.001:
                                pre_block_w = prev_entries[j][1]
                                break
                        expected_delta = hist_w_at_d - pre_block_w
                        if expected_delta <= 0.001:
                            # Spurious - no net new weight
                            all_changes.append(("REMOVE", basket, code, evt_date_str,
                                                stored_qty, 0.0, "consecutive-day, no net delta"))
                            new_buy_ev = [(d, q) for d, q in new_buy_ev if d != evt_date_str]
                            changed = True
                        elif abs(stored_qty - expected_delta) > 0.05:
                            # Amount is wrong
                            all_changes.append(("CHANGE", basket, code, evt_date_str,
                                                stored_qty, expected_delta, "consecutive-day, wrong delta"))
                            new_buy_ev = [(d, expected_delta if d == evt_date_str else q)
                                         for d, q in new_buy_ev]
                            changed = True
                        # else: stored amount matches expected delta → valid
                    else:
                        # Gap > 2 days, weight unchanged → spurious duplicate
                        all_changes.append(("REMOVE", basket, code, evt_date_str,
                                            stored_qty, 0.0, f"weight unchanged, gap={days_gap}d"))
                        new_buy_ev = [(d, q) for d, q in new_buy_ev if d != evt_date_str]
                        changed = True

                # ── Case 2: weight increased (genuine buy or wrong amount) ───
                elif hist_w_at_d > prev_w + 0.001:
                    expected_delta = hist_w_at_d - prev_w
                    if abs(stored_qty - expected_delta) > 0.05:
                        all_changes.append(("CHANGE", basket, code, evt_date_str,
                                            stored_qty, expected_delta, "cumulative stored instead of delta"))
                        new_buy_ev = [(d, expected_delta if d == evt_date_str else q)
                                     for d, q in new_buy_ev]
                        changed = True

                # ── Case 3: weight decreased → wrong type, should be sell ───
                elif hist_w_at_d < prev_w - 0.001:
                    sell_delta = round(prev_w - hist_w_at_d, 4)
                    all_changes.append(("WRONG-TYPE", basket, code, evt_date_str,
                                        stored_qty, sell_delta, "decrease stored as buy→move to sell"))
                    new_buy_ev  = [(d, q) for d, q in new_buy_ev if d != evt_date_str]
                    # Add to sell events (avoid duplicate date)
                    sell_dates = {d for d, _ in new_sell_ev}
                    if evt_date_str not in sell_dates:
                        new_sell_ev.append((evt_date_str, sell_delta))
                    changed = True

            if changed and not DRY_RUN:
                det["buyEvents"]  = fmt_events(new_buy_ev)  if new_buy_ev  else ""
                det["sellEvents"] = fmt_events(new_sell_ev) if new_sell_ev else ""

    # ── Report ────────────────────────────────────────────────────────────────
    if not all_changes:
        print("No changes needed — all buy events look correct.")
        return

    print(f"\nFinal changes: {len(all_changes)}")
    print(f"{'Action':<12} {'Basket':<20} {'Code':<16} {'Date':<16} {'Old':>8} {'New':>8}  Note")
    print("-" * 105)
    counts = {"REMOVE": 0, "CHANGE": 0, "WRONG-TYPE": 0}
    for action, basket, code, date_str, old, new, note in sorted(
        all_changes, key=lambda x: (x[1], x[2], _date_key(x[3]))
    ):
        counts[action] = counts.get(action, 0) + 1
        print(f"  {action:<10} | {basket:<20} | {code:<14} | {date_str:<16} | {old:>8.4f} -> {new:>8.4f}  # {note}")

    print(f"\nSummary: {counts.get('REMOVE',0)} REMOVE, {counts.get('CHANGE',0)} CHANGE, {counts.get('WRONG-TYPE',0)} WRONG-TYPE")

    if DRY_RUN:
        print("\n[DRY RUN] No files written. Run without --dry-run to apply.")
        return

    # ── Save ──────────────────────────────────────────────────────────────────
    print(f"\nBacking up to {BACKUP.name}...")
    shutil.copy2(BP_FILE, BACKUP)

    print("Saving buy_price_data.json...")
    with open(BP_FILE, "w", encoding="utf-8") as f:
        json.dump(bp, f, indent=2, ensure_ascii=False)

    print("Done. Run calc_all_buy_prices.py to recalculate buy prices.")


if __name__ == "__main__":
    main()
