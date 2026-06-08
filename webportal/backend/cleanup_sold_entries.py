"""
cleanup_sold_entries.py

Removes spurious/duplicate sold entries from portfolios.json for ALL baskets:

1. Entries predating the basket's earliest valid sell event (e.g. "19 Sep 2019")
2. Entries with future/estimated dates that have no OHLC data and no
   matching sell event in buy_price_data.json (e.g. "05 May 2026", "11 May 2026")
3. Rebalance-date duplicates: when both an old rebalance-date entry AND a
   correct next-trading-day entry (from buy_price_data.json sellEvents) exist
   for the same stock/action, remove the rebalance-date one.

Run:  python cleanup_sold_entries.py
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE    = Path(__file__).parent
BP_FILE = BASE / "buy_price_data.json"
PF_FILE = BASE / "portfolios.json"

BASKETS = [
    "Mid_Small_Cap", "Green_Energy", "IPO_Basket",
    "Trends_Triology", "Techstack", "Make_in_India", "Consumer_Trends",
]

# Entries on or before this date are clearly pre-portfolio and should be removed
# (MSC started Nov 2019; using Sep 2019 as a safe floor)
PRE_PORTFOLIO_CUTOFF = datetime(2019, 10, 31)

# Latest known rebalance date; entries significantly after this with no OHLC and
# no matching sell event are estimates to be removed
LAST_KNOWN_REBALANCE = datetime(2026, 4, 12)
FUTURE_CUTOFF        = LAST_KNOWN_REBALANCE + timedelta(days=14)  # 26 Apr 2026


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


def _ts(date_str: str) -> int | None:
    try:
        return int(datetime.strptime(date_str.strip(), "%d %b %Y")
                   .replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return None


def _dt(date_str: str) -> datetime | None:
    try:
        return datetime.strptime(date_str.strip(), "%d %b %Y")
    except Exception:
        return None


def get_valid_sell_dates(det: dict) -> set[str]:
    """All sell event dates for a stock from buy_price_data.json."""
    dates = set()
    for field in ("sellEvents", "prevSellEvents"):
        for d, _ in parse_events(det.get(field) or ""):
            dates.add(d)
    return dates


def is_pre_portfolio(date_str: str) -> bool:
    dt = _dt(date_str)
    return dt is not None and dt <= PRE_PORTFOLIO_CUTOFF


def is_future_estimate(date_str: str, has_sell_price: bool,
                       valid_sell_dates: set[str]) -> bool:
    """Entry is after last known rebalance, has no sell price, and has no matching
    sell event in buy_price_data.json → it's a forward-estimated entry."""
    if date_str in valid_sell_dates:
        return False          # legitimate sell event date, keep it
    if has_sell_price:
        return False          # has actual data, don't remove automatically
    dt = _dt(date_str)
    return dt is not None and dt > FUTURE_CUTOFF


def is_rebalance_date_duplicate(date_str: str, valid_sell_dates: set[str],
                                  existing_valid_dates_in_set: set[str]) -> bool:
    """
    An entry is a rebalance-date duplicate if:
    - Its date is NOT a valid sell event date (it's the rebalance date), AND
    - The next-trading-day (or next 1-5 calendar days) IS a valid sell event
      date that already exists in the kept entries.
    """
    if date_str in valid_sell_dates:
        return False  # this IS a valid sell event date — keep

    dt = _dt(date_str)
    if dt is None:
        return False

    # Check if any valid sell event date is within 1-5 days after this date
    for offset in range(1, 6):
        candidate = dt + timedelta(days=offset)
        MONTHS = {1:'Jan',2:'Feb',3:'Mar',4:'Apr',5:'May',6:'Jun',
                  7:'Jul',8:'Aug',9:'Sep',10:'Oct',11:'Nov',12:'Dec'}
        cand_str = f"{candidate.day:02d} {MONTHS[candidate.month]} {candidate.year}"
        if cand_str in valid_sell_dates and cand_str in existing_valid_dates_in_set:
            return True
    return False


def clean_basket(basket: str, bp_data: dict, portfolios: dict) -> tuple[int, int, int, int]:
    basket_bp  = bp_data.get(basket, {})
    sold_key   = f"{basket}_sold"
    sold_list  = portfolios.get(sold_key, [])
    if not sold_list:
        return 0, 0, 0, len(sold_list)

    removed_pre       = 0
    removed_future    = 0
    removed_duplicate = 0
    original_count    = len(sold_list)

    # Build set of valid sell dates per ticker from buy_price_data.json
    valid_dates_by_ticker: dict[str, set[str]] = {}
    for nse, det in basket_bp.items():
        valid_dates_by_ticker[nse] = get_valid_sell_dates(det)

    # Two-pass: first identify what to keep, then filter
    kept: list[dict] = []

    # Pass 1: remove pre-portfolio and future-estimate entries
    for entry in sold_list:
        ticker     = entry.get("nseCode", "")
        date_str   = entry.get("date", "")
        has_sp     = entry.get("sellPrice") is not None
        valid_dates= valid_dates_by_ticker.get(ticker, set())

        if is_pre_portfolio(date_str):
            removed_pre += 1
            print(f"  [PRE ] Remove {ticker:15s} {date_str} ({entry.get('action')})")
            continue

        if is_future_estimate(date_str, has_sp, valid_dates):
            removed_future += 1
            print(f"  [FUTR] Remove {ticker:15s} {date_str} ({entry.get('action')}) — no OHLC, no sell event")
            continue

        kept.append(entry)

    # Pass 2: remove rebalance-date duplicates (need to know which valid-date entries exist)
    kept_dates_by_ticker: dict[str, set[str]] = {}
    for entry in kept:
        ticker   = entry.get("nseCode", "")
        date_str = entry.get("date", "")
        if ticker not in kept_dates_by_ticker:
            kept_dates_by_ticker[ticker] = set()
        kept_dates_by_ticker[ticker].add(date_str)

    final: list[dict] = []
    for entry in kept:
        ticker     = entry.get("nseCode", "")
        date_str   = entry.get("date", "")
        valid_dates= valid_dates_by_ticker.get(ticker, set())
        kept_dates = kept_dates_by_ticker.get(ticker, set())

        if is_rebalance_date_duplicate(date_str, valid_dates, kept_dates):
            removed_duplicate += 1
            print(f"  [DUPL] Remove {ticker:15s} {date_str} ({entry.get('action')}) — rebalance-date dup")
            continue

        final.append(entry)

    portfolios[sold_key] = final
    total_removed = removed_pre + removed_future + removed_duplicate
    print(f"  {basket}: {original_count} → {len(final)} entries "
          f"(−{removed_pre} pre, −{removed_future} future, −{removed_duplicate} dup)")
    return removed_pre, removed_future, removed_duplicate, len(final)


def main():
    print("Loading data files...")
    with open(BP_FILE, encoding="utf-8") as f:
        bp_data = json.load(f)
    with open(PF_FILE, encoding="utf-8") as f:
        portfolios = json.load(f)

    total_pre = total_future = total_dup = 0

    for basket in BASKETS:
        sold_key = f"{basket}_sold"
        if sold_key not in portfolios:
            continue
        print(f"\n── {basket} ──")
        p, f_, d, _ = clean_basket(basket, bp_data, portfolios)
        total_pre    += p
        total_future += f_
        total_dup    += d

    print(f"\n{'='*60}")
    print(f"Total removed:")
    print(f"  Pre-portfolio  : {total_pre}")
    print(f"  Future estimate: {total_future}")
    print(f"  Duplicates     : {total_dup}")
    print(f"  TOTAL          : {total_pre + total_future + total_dup}")

    print("\nSaving portfolios.json...")
    with open(PF_FILE, "w", encoding="utf-8") as f:
        json.dump(portfolios, f, indent=2, ensure_ascii=False)
    print("Saved.")

    # Quick check on MSC
    msc_sold = portfolios.get("Mid_Small_Cap_sold", [])
    null_bp  = sum(1 for e in msc_sold if e.get("buyPrice")  is None)
    null_sp  = sum(1 for e in msc_sold if e.get("sellPrice") is None)
    print(f"\nMSC sold entries: {len(msc_sold)} total, "
          f"{null_bp} missing buyPrice, {null_sp} missing sellPrice")


if __name__ == "__main__":
    main()
