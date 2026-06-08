"""
fix_mii_basket_pnl.py

Derives all buy/sell events for Make_in_India basket from Excel rebalance history,
fetches OHLC prices (Yahoo Finance -> Google Finance -> Screener.in),
computes FIFO P&L, and updates:
  - buy_price_data.json   (Make_in_India section)
  - gains_statement.json  (Make_in_India section replaced)
  - portfolios.json       (Make_in_India_sold rebuilt)

Run: python fix_mii_basket_pnl.py
"""

import asyncio, json, os, re, shutil, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx, openpyxl

sys.stdout.reconfigure(encoding="utf-8")

BASE        = Path(__file__).parent
EXCEL_PATH  = Path(r"C:\Users\PC\OneDrive - Niveshaay Investment Advisors (1)\Nukul Madaan NIA\Niveshaay Office Work\Rebalance Historical Data\Historical Index Values_22.05.2026\Make in India Index Value.xlsx")
BASKET      = "Make_in_India"
DELTA_THR   = 0.003   # min weight change (%) to register as event
CONCURRENCY = 10

YF_SYMBOL_MAP = {
    "LIQUIDBEES": "LIQUIDBEES.NS",
    "LIQUIDCASE": "LIQUIDCASE.NS",
    "544531":     "TRUECOLORS.BO",
    "ACUTAAS":    "ACUTAAS.BO",
    "HBLENGINE":  "HBLENGINE.BO",
    "ARIS":       "ARIS.BO",
    "SETL":       "SETL.BO",
}

# ── event string helpers ───────────────────────────────────────────────────────

def parse_events(text: str) -> list[tuple[str, float]]:
    events = []
    for line in (text or "").strip().splitlines():
        parts = re.split(r"[*×]", line.strip())
        if len(parts) == 2:
            try:
                events.append((parts[0].strip(), float(parts[1].strip())))
            except ValueError:
                pass
    return events

def fmt_events(pairs: list[tuple[str, float]]) -> str:
    return "\n".join(f"{d} * {w:g}" for d, w in pairs)

# ── OHLC fetchers ──────────────────────────────────────────────────────────────

async def _yahoo_ohlc(nse: str, ts: int, client: httpx.AsyncClient) -> float | None:
    sym = YF_SYMBOL_MAP.get(nse, f"{nse}.NS")
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?interval=1d&period1={ts}&period2={ts + 4 * 86400}")
    try:
        r = await client.get(url, timeout=12)
        res = r.json().get("chart", {}).get("result", [])
        if not res:
            return None
        q = res[0].get("indicators", {}).get("quote", [{}])[0]
        timestamps = res[0].get("timestamp", [])
        for i, (o, h, l, c) in enumerate(zip(
            q.get("open", []), q.get("high", []),
            q.get("low",  []), q.get("close", [])
        )):
            if None not in (o, h, l, c):
                if not timestamps or timestamps[i] >= ts - 86400:
                    return round((o + h + l + c) / 4, 2)
    except Exception:
        pass
    return None

async def _google_ohlc(nse: str, dt: datetime, client: httpx.AsyncClient) -> float | None:
    target_ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
    url = (f"https://finance.google.com/finance/getprices"
           f"?q={nse}&x=NSE&i=86400&p=40d&f=d,o,h,l,c,v&df=cpct&auto=1")
    try:
        r = await client.get(url, timeout=12)
        base_ts = None
        for line in r.text.strip().splitlines():
            if line.startswith(("TIMEZONE", "MARKET", "EXCHANGE", "DATA")):
                continue
            parts = line.split(",")
            if line.startswith("a"):
                base_ts = int(parts[0][1:]); offset = 0
            else:
                try: offset = int(parts[0])
                except ValueError: continue
            if base_ts is None or len(parts) < 5:
                continue
            row_ts = base_ts + offset * 86400
            if abs(row_ts - target_ts) < 4 * 86400:
                try:
                    o, c, h, l = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
                    if 0 not in (o, h, l, c):
                        return round((o + h + l + c) / 4, 2)
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass
    return None

async def _screener_ohlc(nse: str, dt: datetime, client: httpx.AsyncClient) -> float | None:
    try:
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
        hdrs = {"User-Agent": ua, "Accept": "application/json, */*",
                "Referer": "https://www.screener.in/", "X-Requested-With": "XMLHttpRequest"}
        await client.get(f"https://www.screener.in/company/{nse}/consolidated/",
                         headers={"User-Agent": ua})
        sr = await client.get(
            f"https://www.screener.in/api/company/search/?q={nse}&v=3&fts=1", headers=hdrs)
        company_id = None
        for item in sr.json():
            if f"/company/{nse}/" in item.get("url", ""):
                company_id = item.get("id"); break
        if not company_id:
            return None
        cr = await client.get(
            f"https://www.screener.in/api/company/{company_id}/chart/"
            f"?q=Price-DMA50-DMA200-Volume&days=400&consolidated=true", headers=hdrs)
        prices: dict[str, float] = {}
        for ds in cr.json().get("datasets", []):
            if ds.get("metric") == "Price":
                for entry in ds.get("values", []):
                    prices[entry[0]] = float(entry[1])
                break
        for i in range(5):
            check = (dt + timedelta(days=i)).strftime("%Y-%m-%d")
            if check in prices:
                return round(prices[check], 2)
    except Exception:
        pass
    return None

async def fetch_ohlc(nse: str, date_str: str, client: httpx.AsyncClient,
                     sem: asyncio.Semaphore) -> float | None:
    async with sem:
        try:
            dt = datetime.strptime(date_str, "%d %b %Y")
        except ValueError:
            return None
        ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
        val = await _yahoo_ohlc(nse, ts, client)
        if val is not None:
            return val
        val = await _google_ohlc(nse, dt, client)
        if val is not None:
            return val
        return await _screener_ohlc(nse, dt, client)

# ── Excel → event history ──────────────────────────────────────────────────────

def load_periods(name_to_nse: dict) -> list[tuple[str, dict[str, float]]]:
    try:
        wb = openpyxl.load_workbook(EXCEL_PATH, data_only=True)
    except PermissionError:
        tmp = Path(os.environ["TEMP"]) / "mii_temp.xlsx"
        wb = openpyxl.load_workbook(tmp, data_only=True)
    ws = wb.worksheets[1]
    periods, cur_period, cur_stks = [], None, {}
    for row in ws.iter_rows(values_only=True):
        dr, name, wt = row[0], row[1], row[2]
        if dr:
            if cur_period and cur_stks:
                periods.append((cur_period, dict(cur_stks)))
            cur_period, cur_stks = str(dr).strip(), {}
        if name and wt is not None:
            nse = name_to_nse.get(str(name).strip().lower())
            if nse:
                cur_stks[nse] = round(cur_stks.get(nse, 0) + float(wt) * 100, 4)
    if cur_period and cur_stks:
        periods.append((cur_period, dict(cur_stks)))
    return periods[1:]   # skip header row (first row is header, not a period)


def build_stock_series(periods: list) -> dict[str, list[dict]]:
    """
    Returns nse -> list of series dicts.
    Each series: {'buys': [(date, weight)], 'sells': [(date, weight)], 'is_active': bool}
    """
    stock_series: dict[str, list[dict]] = {}
    stock_active: dict[str, dict] = {}
    prev_w: dict[str, float] = {}

    for i, (dr, nse_wts) in enumerate(periods):
        p_start  = datetime.strptime(dr.split(" to ")[0].strip(), "%Y-%m-%d")
        sell_dt  = p_start.strftime("%d %b %Y")
        buy_dt   = p_start.strftime("%d %b %Y")

        if i == 0:
            for nse, w in nse_wts.items():
                stock_active[nse] = {"buys": [(buy_dt, w)], "sells": [], "is_active": True}
            prev_w = nse_wts.copy()
            continue

        for nse in set(prev_w) | set(nse_wts):
            pw = prev_w.get(nse, 0.0)
            cw = nse_wts.get(nse, 0.0)
            delta = round(cw - pw, 4)

            if delta > DELTA_THR:
                if pw < DELTA_THR:
                    stock_active[nse] = {"buys": [(buy_dt, round(delta, 4))],
                                         "sells": [], "is_active": True}
                elif nse in stock_active:
                    stock_active[nse]["buys"].append((buy_dt, round(delta, 4)))

            elif delta < -DELTA_THR:
                sdelta = round(abs(delta), 4)
                if nse in stock_active:
                    stock_active[nse]["sells"].append((sell_dt, sdelta))
                    if cw < DELTA_THR:
                        ser = stock_active.pop(nse)
                        ser["is_active"] = False
                        stock_series.setdefault(nse, []).append(ser)

        prev_w = nse_wts.copy()

    for nse, ser in stock_active.items():
        ser["is_active"] = True
        stock_series.setdefault(nse, []).append(ser)

    return stock_series

# ── FIFO gains ─────────────────────────────────────────────────────────────────

def fifo_gains(buys: list[tuple[str, float]], sells: list[tuple[str, float]],
               buy_ohlc: dict, sell_ohlc: dict,
               series_is_active: bool) -> list[dict]:
    lots = [{"date": d, "qty": q, "rem": q, "price": buy_ohlc.get(d)} for d, q in buys]
    gains, lot_ptr = [], 0

    for s_idx, (sell_date, sell_qty) in enumerate(sells):
        sell_price = sell_ohlc.get(sell_date)
        consumed, rem_sell = [], sell_qty

        j = lot_ptr
        while rem_sell > 1e-5 and j < len(lots):
            lot = lots[j]
            if lot["rem"] < 1e-5:
                j += 1; continue
            take = min(lot["rem"], rem_sell)
            gp = (round((sell_price - lot["price"]) / lot["price"] * 100, 2)
                  if (sell_price and lot["price"]) else None)
            consumed.append({"buyDate": lot["date"], "weight": round(take, 4),
                             "buyPrice": lot["price"], "gainPct": gp})
            lot["rem"] = round(lot["rem"] - take, 6)
            rem_sell   = round(rem_sell - take, 6)
            if lot["rem"] < 1e-5:
                j += 1
        lot_ptr = j

        if not consumed:
            continue

        valid = [c for c in consumed if c["gainPct"] is not None]
        tw = sum(c["weight"] for c in (valid or consumed))
        wg  = (sum(c["weight"] * c["gainPct"]  for c in valid) / sum(c["weight"] for c in valid)) if valid else None
        wbp = (sum(c["weight"] * c["buyPrice"] for c in valid) / sum(c["weight"] for c in valid)) if valid else None

        is_last = s_idx == len(sells) - 1
        stype = "Full Exit" if (is_last and not series_is_active) else "Partial Sell"

        gains.append({
            "sellDate":           sell_date,
            "sellWeight":         sell_qty,
            "sellPrice":          sell_price,
            "sellType":           stype,
            "lots":               consumed,
            "weightedGainPct":    round(wg, 2)  if wg  is not None else None,
            "weightedAvgBuyPrice": round(wbp, 4) if wbp is not None else None,
        })

    return gains

# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    bp_path   = BASE / "buy_price_data.json"
    port_path = BASE / "portfolios.json"
    gs_path   = BASE / "gains_statement.json"

    with open(bp_path,   encoding="utf-8") as f: bp_data    = json.load(f)
    with open(port_path, encoding="utf-8") as f: portfolios = json.load(f)
    with open(gs_path,   encoding="utf-8") as f: gains_stmt = json.load(f)
    with open(BASE / "rebalance_history.json", encoding="utf-8") as f:
        rh = json.load(f)

    # Backup
    shutil.copy(bp_path,   BASE / "buy_price_data_backup_before_mii_fix.json")
    shutil.copy(gs_path,   BASE / "gains_statement_backup_before_mii_fix.json")
    shutil.copy(port_path, BASE / "portfolios_backup_before_mii_fix.json")
    print("Backups created.")

    # Build name→NSE map
    mii_all = portfolios.get(BASKET, []) + portfolios.get(f"{BASKET}_sold", [])
    name_to_nse: dict[str, str] = {}
    for s in mii_all:
        name_to_nse[s.get("securityName", "").strip().lower()] = s["nseCode"]
    for item in rh.get(BASKET, []):
        name_to_nse[item.get("securityName", "").strip().lower()] = item["nseCode"]
    nse_to_name: dict[str, str] = {s["nseCode"]: s.get("securityName", s["nseCode"]) for s in mii_all}

    # Parse Excel and build event history
    periods      = load_periods(name_to_nse)
    stock_series = build_stock_series(periods)
    print(f"Parsed {len(periods)} periods, {len(stock_series)} stocks with events.")

    # ── Update buy_price_data.json ─────────────────────────────────────────────
    basket_bp = bp_data.setdefault(BASKET, {})

    for nse, series_list in stock_series.items():
        det = basket_bp.setdefault(nse, {
            "securityName": nse_to_name.get(nse, nse),
            "buyEvents": "", "sellEvents": "",
            "prevBuyEvents": "", "prevSellEvents": "",
            "buyOHLC": {}, "sellOHLC": {},
        })
        if not det.get("securityName"):
            det["securityName"] = nse_to_name.get(nse, nse)

        if len(series_list) == 1:
            det["prevBuyEvents"]  = ""
            det["prevSellEvents"] = ""
            det["buyEvents"]      = fmt_events(series_list[0]["buys"])
            det["sellEvents"]     = fmt_events(series_list[0]["sells"])
        elif len(series_list) == 2:
            det["prevBuyEvents"]  = fmt_events(series_list[0]["buys"])
            det["prevSellEvents"] = fmt_events(series_list[0]["sells"])
            det["buyEvents"]      = fmt_events(series_list[1]["buys"])
            det["sellEvents"]     = fmt_events(series_list[1]["sells"])
        else:
            # 3+ series (e.g. LIQUIDBEES) — store all prev series combined; last as current
            det["prevBuyEvents"]  = fmt_events([(d,w) for s in series_list[:-1] for d,w in s["buys"]])
            det["prevSellEvents"] = fmt_events([(d,w) for s in series_list[:-1] for d,w in s["sells"]])
            det["buyEvents"]      = fmt_events(series_list[-1]["buys"])
            det["sellEvents"]     = fmt_events(series_list[-1]["sells"])

    # ── Collect all (nse, date) pairs that need OHLC ──────────────────────────
    pairs_needed: set[tuple[str, str]] = set()
    for nse, det in basket_bp.items():
        buy_ohlc  = det.get("buyOHLC")  or {}
        sell_ohlc = det.get("sellOHLC") or {}
        for date_str, _ in parse_events(det.get("buyEvents")      or ""):
            if date_str not in buy_ohlc:  pairs_needed.add((nse, date_str))
        for date_str, _ in parse_events(det.get("prevBuyEvents")  or ""):
            if date_str not in buy_ohlc:  pairs_needed.add((nse, date_str))
        for date_str, _ in parse_events(det.get("sellEvents")     or ""):
            if date_str not in sell_ohlc: pairs_needed.add((nse, date_str))
        for date_str, _ in parse_events(det.get("prevSellEvents") or ""):
            if date_str not in sell_ohlc: pairs_needed.add((nse, date_str))

    unique_pairs = sorted(pairs_needed)
    print(f"\nFetching {len(unique_pairs)} new OHLC prices...")

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(follow_redirects=True, timeout=15,
                                 headers={"User-Agent": "Mozilla/5.0"}) as client:
        prices = await asyncio.gather(
            *[fetch_ohlc(nse, ds, client, sem) for nse, ds in unique_pairs]
        )
    ohlc_map: dict[tuple[str,str], float | None] = dict(zip(unique_pairs, prices))

    # Write OHLC back
    fetched = missing = 0
    for nse, det in basket_bp.items():
        buy_ohlc  = dict(det.get("buyOHLC")  or {})
        sell_ohlc = dict(det.get("sellOHLC") or {})
        for date_str, _ in (parse_events(det.get("buyEvents") or "") +
                            parse_events(det.get("prevBuyEvents") or "")):
            if date_str not in buy_ohlc:
                p = ohlc_map.get((nse, date_str))
                if p is not None:
                    buy_ohlc[date_str] = p; fetched += 1
                    print(f"  [BUY ] {nse:15s} @ {date_str} = ₹{p:,.2f}")
                else:
                    missing += 1
                    print(f"  [BUY ] {nse:15s} @ {date_str} = MISSING")
        for date_str, _ in (parse_events(det.get("sellEvents") or "") +
                            parse_events(det.get("prevSellEvents") or "")):
            if date_str not in sell_ohlc:
                p = ohlc_map.get((nse, date_str))
                if p is not None:
                    sell_ohlc[date_str] = p; fetched += 1
                    print(f"  [SELL] {nse:15s} @ {date_str} = ₹{p:,.2f}")
                else:
                    missing += 1
                    print(f"  [SELL] {nse:15s} @ {date_str} = MISSING")
        det["buyOHLC"]  = buy_ohlc
        det["sellOHLC"] = sell_ohlc

    print(f"\nOHLC: fetched={fetched}  missing={missing}")

    # ── Compute FIFO gains for all stocks ─────────────────────────────────────
    mii_gains: dict[str, dict] = {}

    for nse, series_list in stock_series.items():
        det       = basket_bp.get(nse, {})
        buy_ohlc  = det.get("buyOHLC")  or {}
        sell_ohlc = det.get("sellOHLC") or {}

        prev_gains: list[dict] = []
        curr_gains: list[dict] = []

        if len(series_list) == 1:
            s = series_list[0]
            if s["sells"]:
                curr_gains = fifo_gains(s["buys"], s["sells"], buy_ohlc, sell_ohlc, s["is_active"])

        elif len(series_list) == 2:
            s0, s1 = series_list
            if s0["sells"]:
                prev_gains = fifo_gains(s0["buys"], s0["sells"], buy_ohlc, sell_ohlc, False)
            if s1["sells"]:
                curr_gains = fifo_gains(s1["buys"], s1["sells"], buy_ohlc, sell_ohlc, s1["is_active"])

        else:
            # 3+ series: compute each, store all in prevSeriesGains
            for s in series_list:
                if s["sells"]:
                    prev_gains.extend(fifo_gains(s["buys"], s["sells"],
                                                 buy_ohlc, sell_ohlc, s["is_active"]))

        if prev_gains or curr_gains:
            mii_gains[nse] = {
                "securityName":      nse_to_name.get(nse, nse),
                "prevSeriesGains":   prev_gains,
                "currentSeriesGains": curr_gains,
            }
            total = len(prev_gains) + len(curr_gains)
            computed = sum(1 for g in prev_gains + curr_gains if g.get("weightedGainPct") is not None)
            print(f"  {nse:15s}  {total} sell events  ({computed} with gain%)")

    # ── Update gains_statement.json ────────────────────────────────────────────
    gains_stmt[BASKET] = mii_gains

    # ── Rebuild Make_in_India_sold ─────────────────────────────────────────────
    active_nse = {nse for nse, sl in stock_series.items() if sl[-1]["is_active"]}
    sold: list[dict] = []
    for nse, data in mii_gains.items():
        if nse in active_nse:
            continue
        all_g = data["prevSeriesGains"] + data["currentSeriesGains"]
        if not all_g:
            continue
        last = max(all_g, key=lambda x: datetime.strptime(x["sellDate"], "%d %b %Y"))
        sold.append({
            "nseCode":      nse,
            "securityName": data["securityName"],
            "buyPrice":     last.get("weightedAvgBuyPrice"),
            "sellPrice":    last["sellPrice"],
            "sellDate":     last["sellDate"],
        })
    portfolios[f"{BASKET}_sold"] = sold

    # ── Save ───────────────────────────────────────────────────────────────────
    with open(bp_path,   "w", encoding="utf-8") as f: json.dump(bp_data,    f, indent=2, ensure_ascii=False)
    with open(gs_path,   "w", encoding="utf-8") as f: json.dump(gains_stmt, f, indent=2, ensure_ascii=False)
    with open(port_path, "w", encoding="utf-8") as f: json.dump(portfolios, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"  Make_in_India P&L rebuilt: {len(mii_gains)} stocks with gains records")
    print(f"  Make_in_India_sold rebuilt: {len(sold)} records")
    print(f"  OHLC fetched: {fetched}   missing/null: {missing}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
