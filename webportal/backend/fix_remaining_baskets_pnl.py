"""
fix_remaining_baskets_pnl.py

Fixes buy date convention (buy_dt = period_start, not period_start-1) for:
  Mid_Small_Cap, Green_Energy, IPO_Basket, Trends_Triology, Techstack

Fetches missing OHLC prices, recomputes FIFO gains, and updates:
  - buy_price_data.json
  - gains_statement.json
  - portfolios.json  (*_sold lists)

Run: python fix_remaining_baskets_pnl.py
"""

import asyncio, json, os, re, shutil, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx, openpyxl

sys.stdout.reconfigure(encoding="utf-8")

BASE       = Path(__file__).parent
EXCEL_BASE = Path(r"C:\Users\PC\OneDrive - Niveshaay Investment Advisors (1)\Nukul Madaan NIA\Niveshaay Office Work\Rebalance Historical Data\Historical Index Values_22.05.2026")
DELTA_THR  = 0.003
CONCURRENCY = 10

BASKETS_CONFIG = {
    "Mid_Small_Cap": {
        "excel": "Mid & Small Cap Index Value.xlsx",
    },
    "Green_Energy": {
        "excel": "Green Energy Index Value.xlsx",
    },
    "IPO_Basket": {
        "excel": "Niveshaay IPO Basket Fundamental Index Value.xlsx",
    },
    "Trends_Triology": {
        "excel": "Trends Trilogy Index Value.xlsx",
    },
    "Techstack": {
        "excel": "Techstack Index Value.xlsx",
    },
}

YF_SYMBOL_MAP = {
    "LIQUIDBEES":  "LIQUIDBEES.NS",
    "LIQUIDCASE":  "LIQUIDCASE.NS",
    "LIQUIDIETF":  "LIQUIDIETF.NS",
    "544531":      "TRUECOLORS.BO",
    "ACUTAAS":     "ACUTAAS.BO",
    "HBLENGINE":   "HBLENGINE.BO",
    "ARIS":        "ARIS.BO",
    "SETL":        "SETL.BO",
}

# ── helpers ────────────────────────────────────────────────────────────────────

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

# ── Excel parser ───────────────────────────────────────────────────────────────

def load_periods(excel_path: Path, name_to_nse: dict) -> list[tuple[str, dict[str, float]]]:
    try:
        wb = openpyxl.load_workbook(excel_path, data_only=True)
    except PermissionError:
        tmp = Path(os.environ["TEMP"]) / f"basket_temp_{excel_path.stem}.xlsx"
        import shutil as _sh; _sh.copy2(excel_path, tmp)
        wb = openpyxl.load_workbook(tmp, data_only=True)
    ws = wb.worksheets[1]
    periods, cur_period, cur_stks = [], None, {}
    for row in ws.iter_rows(values_only=True):
        dr, name, wt = row[0], row[1], row[2]
        if dr and str(dr).strip() not in ("Date Range", ""):
            if cur_period and cur_stks:
                periods.append((str(cur_period).strip(), dict(cur_stks)))
            cur_period = str(dr).strip()
            cur_stks = {}
        if name and wt is not None and str(name).strip() not in ("Constituents", ""):
            nse = name_to_nse.get(str(name).strip().lower())
            if nse:
                try:
                    cur_stks[nse] = round(cur_stks.get(nse, 0) + float(wt) * 100, 4)
                except (ValueError, TypeError):
                    pass
    if cur_period and cur_stks:
        periods.append((cur_period, dict(cur_stks)))
    return [p for p in periods if " to " in p[0]]


def build_stock_series(periods: list) -> dict[str, list[dict]]:
    stock_series: dict[str, list[dict]] = {}
    stock_active: dict[str, dict] = {}
    prev_w: dict[str, float] = {}

    for i, (dr, nse_wts) in enumerate(periods):
        p_start = datetime.strptime(dr.split(" to ")[0].strip(), "%Y-%m-%d")
        sell_dt = p_start.strftime("%d %b %Y")
        buy_dt  = p_start.strftime("%d %b %Y")   # period_start for all buys

        if i == 0:
            for nse, w in nse_wts.items():
                stock_active[nse] = {"buys": [(buy_dt, w)], "sells": [], "is_active": True}
            prev_w = nse_wts.copy()
            continue

        for nse in set(prev_w) | set(nse_wts):
            pw    = prev_w.get(nse, 0.0)
            cw    = nse_wts.get(nse, 0.0)
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

def fifo_gains(buys, sells, buy_ohlc, sell_ohlc, series_is_active) -> list[dict]:
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
        wg  = (sum(c["weight"] * c["gainPct"]  for c in valid) / sum(c["weight"] for c in valid)) if valid else None
        wbp = (sum(c["weight"] * c["buyPrice"] for c in valid) / sum(c["weight"] for c in valid)) if valid else None

        is_last = s_idx == len(sells) - 1
        stype = "Full Exit" if (is_last and not series_is_active) else "Partial Sell"

        gains.append({
            "sellDate":            sell_date,
            "sellWeight":          sell_qty,
            "sellPrice":           sell_price,
            "sellType":            stype,
            "lots":                consumed,
            "weightedGainPct":     round(wg,  2) if wg  is not None else None,
            "weightedAvgBuyPrice": round(wbp, 4) if wbp is not None else None,
        })

    return gains

# ── Per-basket processor ───────────────────────────────────────────────────────

async def process_basket(basket: str, excel_path: Path,
                         bp_data: dict, portfolios: dict, gains_stmt: dict,
                         rh: dict, client: httpx.AsyncClient, sem: asyncio.Semaphore):
    print(f"\n{'='*60}")
    print(f"  Processing {basket}")
    print(f"{'='*60}")

    all_stocks = portfolios.get(basket, []) + portfolios.get(f"{basket}_sold", [])
    name_to_nse: dict[str, str] = {}
    for s in all_stocks:
        name_to_nse[s.get("securityName", "").strip().lower()] = s["nseCode"]
    for item in rh.get(basket, []):
        name_to_nse[item.get("securityName", "").strip().lower()] = item["nseCode"]
    nse_to_name: dict[str, str] = {s["nseCode"]: s.get("securityName", s["nseCode"]) for s in all_stocks}

    periods      = load_periods(excel_path, name_to_nse)
    stock_series = build_stock_series(periods)
    print(f"  {len(periods)} periods, {len(stock_series)} stocks with events")

    basket_bp = bp_data.setdefault(basket, {})

    for nse, series_list in stock_series.items():
        det = basket_bp.setdefault(nse, {
            "securityName": nse_to_name.get(nse, nse),
            "buyEvents": "", "sellEvents": "",
            "prevBuyEvents": "", "prevSellEvents": "",
            "buyOHLC": {}, "sellOHLC": {},
        })
        if not det.get("securityName"):
            det["securityName"] = nse_to_name.get(nse, nse)

        existing_buy_ohlc  = dict(det.get("buyOHLC")  or {})
        existing_sell_ohlc = dict(det.get("sellOHLC") or {})

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
            det["prevBuyEvents"]  = fmt_events([(d,w) for s in series_list[:-1] for d,w in s["buys"]])
            det["prevSellEvents"] = fmt_events([(d,w) for s in series_list[:-1] for d,w in s["sells"]])
            det["buyEvents"]      = fmt_events(series_list[-1]["buys"])
            det["sellEvents"]     = fmt_events(series_list[-1]["sells"])

        det["buyOHLC"]  = existing_buy_ohlc
        det["sellOHLC"] = existing_sell_ohlc

    # Collect missing OHLC pairs
    pairs_needed: set[tuple[str, str]] = set()
    for nse, det in basket_bp.items():
        buy_ohlc  = det.get("buyOHLC")  or {}
        sell_ohlc = det.get("sellOHLC") or {}
        for d, _ in parse_events(det.get("buyEvents","")) + parse_events(det.get("prevBuyEvents","")):
            if d not in buy_ohlc:  pairs_needed.add((nse, d))
        for d, _ in parse_events(det.get("sellEvents","")) + parse_events(det.get("prevSellEvents","")):
            if d not in sell_ohlc: pairs_needed.add((nse, d))

    unique_pairs = sorted(pairs_needed)
    print(f"  Fetching {len(unique_pairs)} new OHLC prices...")

    prices = await asyncio.gather(
        *[fetch_ohlc(nse, ds, client, sem) for nse, ds in unique_pairs]
    )
    ohlc_map = dict(zip(unique_pairs, prices))

    fetched = missing = 0
    for nse, det in basket_bp.items():
        buy_ohlc  = dict(det.get("buyOHLC")  or {})
        sell_ohlc = dict(det.get("sellOHLC") or {})
        for d, _ in parse_events(det.get("buyEvents","")) + parse_events(det.get("prevBuyEvents","")):
            if d not in buy_ohlc:
                p = ohlc_map.get((nse, d))
                if p is not None:
                    buy_ohlc[d] = p; fetched += 1
                    print(f"  [BUY ] {nse:15s} @ {d} = ₹{p:,.2f}")
                else:
                    missing += 1
                    print(f"  [BUY ] {nse:15s} @ {d} = MISSING")
        for d, _ in parse_events(det.get("sellEvents","")) + parse_events(det.get("prevSellEvents","")):
            if d not in sell_ohlc:
                p = ohlc_map.get((nse, d))
                if p is not None:
                    sell_ohlc[d] = p; fetched += 1
                    print(f"  [SELL] {nse:15s} @ {d} = ₹{p:,.2f}")
                else:
                    missing += 1
                    print(f"  [SELL] {nse:15s} @ {d} = MISSING")
        det["buyOHLC"]  = buy_ohlc
        det["sellOHLC"] = sell_ohlc

    print(f"  OHLC: fetched={fetched}  missing={missing}")

    # FIFO gains
    basket_gains: dict[str, dict] = {}
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
            for s in series_list:
                if s["sells"]:
                    prev_gains.extend(fifo_gains(s["buys"], s["sells"], buy_ohlc, sell_ohlc, s["is_active"]))

        if prev_gains or curr_gains:
            basket_gains[nse] = {
                "securityName":       nse_to_name.get(nse, nse),
                "prevSeriesGains":    prev_gains,
                "currentSeriesGains": curr_gains,
            }

    gains_stmt[basket] = basket_gains

    # Rebuild *_sold
    active_nse = {nse for nse, sl in stock_series.items() if sl[-1]["is_active"]}
    sold: list[dict] = []
    for nse, data in basket_gains.items():
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
    portfolios[f"{basket}_sold"] = sold

    total_g = sum(len(v["prevSeriesGains"]) + len(v["currentSeriesGains"]) for v in basket_gains.values())
    print(f"  {len(basket_gains)} stocks with gains | {total_g} sell events | {len(sold)} in *_sold")
    return fetched, missing

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
    shutil.copy(bp_path,   BASE / "buy_price_data_backup_before_remaining_fix.json")
    shutil.copy(gs_path,   BASE / "gains_statement_backup_before_remaining_fix.json")
    shutil.copy(port_path, BASE / "portfolios_backup_before_remaining_fix.json")
    print("Backups created.")

    sem = asyncio.Semaphore(CONCURRENCY)
    total_fetched = total_missing = 0

    async with httpx.AsyncClient(follow_redirects=True, timeout=15,
                                 headers={"User-Agent": "Mozilla/5.0"}) as client:
        for basket, cfg in BASKETS_CONFIG.items():
            excel_path = EXCEL_BASE / cfg["excel"]
            f, m = await process_basket(basket, excel_path, bp_data, portfolios,
                                        gains_stmt, rh, client, sem)
            total_fetched += f
            total_missing += m

    with open(bp_path,   "w", encoding="utf-8") as f: json.dump(bp_data,    f, indent=2, ensure_ascii=False)
    with open(gs_path,   "w", encoding="utf-8") as f: json.dump(gains_stmt, f, indent=2, ensure_ascii=False)
    with open(port_path, "w", encoding="utf-8") as f: json.dump(portfolios, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"  All 5 baskets fixed.")
    print(f"  Total OHLC fetched: {total_fetched}  missing: {total_missing}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
