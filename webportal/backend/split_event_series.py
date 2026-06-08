"""
One-time migration: splits buyEvents / sellEvents in buy_price_data.json into
current-series and previous-series fields for every stock across all baskets.

After this runs:
  buyEvents     → current active series buy events only
  sellEvents    → current active series sell events only
  prevBuyEvents → all prior series buy events (flattened, same text format)
  prevSellEvents→ all prior series sell events (flattened, same text format)

Run: python split_event_series.py
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).parent

BASKETS = [
    "Mid_Small_Cap", "Green_Energy", "IPO_Basket",
    "Trends_Triology", "Techstack", "Make_in_India", "Consumer_Trends",
]


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


def events_to_str(events: list[tuple[str, float]]) -> str:
    return "\n".join(f"{d} * {q:g}" for d, q in events)


def date_ts(s: str) -> int:
    try:
        return int(datetime.strptime(s.strip(), "%d %b %Y")
                   .replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return 0


def split_series(
    buy_events: list[tuple[str, float]],
    sell_events: list[tuple[str, float]],
) -> tuple[list, list, list, list]:
    """
    Returns (current_buy, prev_buy, current_sell, prev_sell).

    Walks all events chronologically, tracks running net weight, and
    records when the current series started (last time weight crossed
    zero and a fresh buy followed).
    """
    combined = (
        [(d, "buy",  q) for d, q in buy_events] +
        [(d, "sell", q) for d, q in sell_events]
    )
    combined.sort(key=lambda e: date_ts(e[0]))

    net: float = 0.0
    series_start_ts: int | None = None

    for d, t, q in combined:
        if t == "buy":
            if net <= 0.001:
                series_start_ts = date_ts(d)
            net += q
        else:
            net = max(0.0, net - q)
            if net <= 0.001:
                series_start_ts = None   # series closed

    if series_start_ts is None:
        # Stock is fully exited — everything is "previous"
        return [], buy_events, [], sell_events

    cur_buy  = [(d, q) for d, q in buy_events  if date_ts(d) >= series_start_ts]
    prev_buy = [(d, q) for d, q in buy_events  if date_ts(d) <  series_start_ts]
    cur_sell  = [(d, q) for d, q in sell_events if date_ts(d) >= series_start_ts]
    prev_sell = [(d, q) for d, q in sell_events if date_ts(d) <  series_start_ts]

    return cur_buy, prev_buy, cur_sell, prev_sell


def main():
    bp_path = BASE / "buy_price_data.json"
    with open(bp_path, encoding="utf-8") as f:
        bp_data = json.load(f)

    changed = 0

    for basket in BASKETS:
        basket_bp = bp_data.get(basket, {})
        print(f"\n── {basket} ({len(basket_bp)} stocks) ──")

        for nse, det in basket_bp.items():
            all_buy  = parse_events(det.get("buyEvents")  or "")
            all_sell = parse_events(det.get("sellEvents") or "")

            cur_buy, prev_buy, cur_sell, prev_sell = split_series(all_buy, all_sell)

            # Only update if there are actually previous-series events to split off
            has_prev = bool(prev_buy or prev_sell)
            already_split = ("prevBuyEvents" in det or "prevSellEvents" in det)

            if not has_prev:
                if not already_split:
                    # No prev events — ensure fields exist as empty strings
                    det.setdefault("prevBuyEvents", "")
                    det.setdefault("prevSellEvents", "")
                continue

            det["buyEvents"]      = events_to_str(cur_buy)
            det["sellEvents"]     = events_to_str(cur_sell)
            det["prevBuyEvents"]  = events_to_str(prev_buy)
            det["prevSellEvents"] = events_to_str(prev_sell)
            changed += 1

            # Print summary
            prev_buy_dates  = ", ".join(d for d, _ in prev_buy)
            prev_sell_dates = ", ".join(d for d, _ in prev_sell)
            print(f"  {nse:15s}  prev_buy=[{prev_buy_dates}]"
                  + (f"  prev_sell=[{prev_sell_dates}]" if prev_sell_dates else ""))

    with open(bp_path, "w", encoding="utf-8") as f:
        json.dump(bp_data, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"Done. {changed} stocks split into current + previous series.")
    print(f"buy_price_data.json updated.")


if __name__ == "__main__":
    main()
