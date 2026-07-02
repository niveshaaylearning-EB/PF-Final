"""Simulator: return calculation, per-basket overrides (mods) and SIPs."""
import asyncio
import time as _time
from datetime import datetime, timedelta

import pandas as pd
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

import database
from main import (
    get_db, _io_pool, yf, SimulationModCreate, SimulationSipCreate,
)
from routers.stocks import _stock_hist_price_cache, _STOCK_HIST_PRICE_TTL

router = APIRouter()

class SimulatorCalculateRequest(BaseModel):
    holdings: list
    sips: list

@router.post("/api/simulator/calculate-return")
async def calculate_simulator_return(req: SimulatorCalculateRequest):
    """
    Calculates absolute return based on 10L initial investment + SIPs.
    """
    INITIAL_INVESTMENT = 1000000.0
    holdings = req.holdings
    sips = req.sips

    if not holdings:
        total_invested = INITIAL_INVESTMENT + sum(s.get('amount', 0) for s in sips)
        return {
            "absolute_return": 0.0,
            "total_invested": round(total_invested, 2),
            "current_value": round(total_invested, 2)
        }

    # Parse holdings
    total_shares = {}
    total_initial_alloc = sum(h.get('allocation', 0) for h in holdings)

    # 1. Distribute Initial 10L
    for h in holdings:
        code    = h['code']
        alloc   = h.get('allocation', 0)
        cmp_val = h.get('cmp', 0) or 0
        perf    = h.get('performance', 0) or 0   # percent, e.g. 97.0 for 97%

        # Always derive buy price from performance + CMP so the investment model
        # stays consistent with the weighted-performance basket_return formula.
        # When the user modifies a stock in the simulator the client recomputes
        # performance = (cmp − bp) / bp * 100, so this inversion gives back the
        # exact buy price the user intended. Stale DB buy prices are ignored.
        bp = (cmp_val / (1 + perf / 100)) if (cmp_val > 0 and perf != -100) else 0

        if total_initial_alloc > 0 and bp > 0:
            invested = INITIAL_INVESTMENT * (alloc / total_initial_alloc)
            total_shares[code] = invested / bp
        else:
            total_shares[code] = 0

    total_invested = INITIAL_INVESTMENT

    if not sips:
        # Fast path if no SIPs
        current_value = sum(total_shares.get(h['code'], 0) * h.get('cmp', 0) for h in holdings)
        abs_ret = ((current_value - total_invested) / total_invested) * 100 if total_invested > 0 else 0
        return {"absolute_return": round(abs_ret, 2), "total_invested": total_invested,
                "current_value": round(current_value, 2), "sip_details": []}

    # ── Helper: fetch OHLC-avg price for one stock on a given date ──────────────
    # Reuses _stock_hist_price_cache so repeated calls are free.
    async def _price_on_date(code: str, date_str: str, loop) -> tuple:
        """Returns (price_float, actual_date_str). actual_date_str is the trading day used."""
        cache_key = f"{code.upper()}:{date_str}"
        cached_hp = _stock_hist_price_cache.get(cache_key)
        if cached_hp and (_time.time() - cached_hp['time']) < _STOCK_HIST_PRICE_TTL:
            return cached_hp['price'], date_str

        dt         = datetime.strptime(date_str, "%Y-%m-%d")
        ticker     = f"{code}.NS" if not code.endswith(".NS") else code
        start_date = dt - timedelta(days=2)
        end_date   = dt + timedelta(days=10)  # exclusive in yfinance, covers ~9 days ahead

        def _fetch():
            return yf.download(ticker, start=start_date.strftime("%Y-%m-%d"),
                               end=end_date.strftime("%Y-%m-%d"), progress=False)

        data = await loop.run_in_executor(_io_pool, _fetch)
        if data is None or (hasattr(data, 'empty') and data.empty):
            return 0.0, date_str

        idx    = data.index.tz_localize(None) if data.index.tz is not None else data.index
        cutoff = pd.to_datetime(dt)

        # 1st choice: exact date  2nd choice: next trading day  3rd: most recent past
        exact  = data[idx == cutoff]
        if not exact.empty:
            row         = exact.iloc[0]
            actual_date = exact.index[0].strftime("%Y-%m-%d")
        else:
            future = data[idx > cutoff]
            if not future.empty:
                row         = future.iloc[0]
                actual_date = future.index[0].strftime("%Y-%m-%d")
            else:
                past = data[idx < cutoff]
                if past.empty:
                    return 0.0, date_str
                row         = past.iloc[-1]
                actual_date = past.index[-1].strftime("%Y-%m-%d")

        def _f(v):
            if hasattr(v, 'item'): v = v.item()
            try: return float(v) if pd.notna(v) else None
            except: return None

        vals = [_f(row[c]) for c in ('Open', 'High', 'Low', 'Close') if c in row.index]
        vals = [v for v in vals if v is not None and v > 0]
        if not vals:
            return 0.0, date_str

        price = round(sum(vals) / len(vals), 2)
        _stock_hist_price_cache[cache_key] = {'time': _time.time(), 'price': price}
        return price, actual_date

    # ── 2. Process SIPs chronologically ────────────────────────────────────────
    sips_sorted = sorted(sips, key=lambda x: x['sip_date'])
    loop        = asyncio.get_running_loop()

    # Fetch prices for every (stock, sip_date) pair in parallel
    all_combos  = [(h['code'], sip['sip_date']) for sip in sips_sorted for h in holdings]
    price_tasks = [_price_on_date(code, date_str, loop) for code, date_str in all_combos]
    price_results = await asyncio.gather(*price_tasks, return_exceptions=True)

    # Restructure into {sip_date: {code: (price, actual_date)}}
    prices_map: dict = {}   # sip_date → {code: (price, actual_date)}
    for (code, date_str), result in zip(all_combos, price_results):
        if isinstance(result, Exception) or result is None:
            result = (0.0, date_str)
        price_val, actual_date = result
        prices_map.setdefault(date_str, {})[code] = (float(price_val), actual_date)

    sip_details = []
    for sip in sips_sorted:
        amount    = sip['amount']
        date_str  = sip['sip_date']
        total_invested += amount

        stock_prices = prices_map.get(date_str, {})

        # Stocks that had a valid price on this trading day
        valid_stocks = [(h, stock_prices[h['code']][0]) for h in holdings
                        if stock_prices.get(h['code'], (0,))[0] > 0]
        valid_alloc  = sum(h.get('allocation', 0) for h, _ in valid_stocks)

        # Determine "actual date used" — first stock's actual date (all should be same trading day)
        actual_date_used = date_str
        for h in holdings:
            if stock_prices.get(h['code'], (0,))[0] > 0:
                actual_date_used = stock_prices[h['code']][1]
                break

        sip_stock_breakdown = []
        if valid_alloc > 0:
            for h, price in valid_stocks:
                code           = h['code']
                alloc          = h.get('allocation', 0)
                # Distribute proportionally among stocks that had prices
                amount_invested = round(amount * (alloc / valid_alloc), 2)
                shares_bought   = amount_invested / price
                total_shares[code] = total_shares.get(code, 0) + shares_bought
                sip_stock_breakdown.append({
                    "code":            code,
                    "allocation":      round(alloc, 2),
                    "price":           round(price, 2),
                    "amount_invested": round(amount_invested, 2),
                    "shares":          round(shares_bought, 4),
                })

        sip_details.append({
            "input_date":  date_str,
            "actual_date": actual_date_used,
            "amount":      amount,
            "distributed": valid_alloc > 0,
            "stocks":      sip_stock_breakdown,
        })

    current_value   = sum(total_shares.get(h['code'], 0) * h.get('cmp', 0) for h in holdings)
    uninvested_cash = sum(sip['amount'] for sip in sips_sorted
                          if prices_map.get(sip['sip_date'], {}) == {} or
                             not any(v[0] > 0 for v in prices_map.get(sip['sip_date'], {}).values()))
    current_value  += uninvested_cash

    abs_ret = ((current_value - total_invested) / total_invested) * 100 if total_invested > 0 else 0
    return {
        "absolute_return": round(abs_ret, 2),
        "total_invested":  round(total_invested, 2),
        "current_value":   round(current_value, 2),
        "sip_details":     sip_details,
    }

@router.post("/api/simulator/{basket_id}/reset")
def reset_simulation(basket_id: str, db: Session = Depends(get_db)):
    db.query(database.SimulationMod).filter(database.SimulationMod.basket_id == basket_id).delete()
    db.query(database.SimulationSip).filter(database.SimulationSip.basket_id == basket_id).delete()
    db.commit()
    return {"status": "success"}

@router.get("/api/simulator/{basket_id}")
def get_simulation_mods(basket_id: str, db: Session = Depends(get_db)):
    mods = db.query(database.SimulationMod).filter(database.SimulationMod.basket_id == basket_id).all()
    return mods

@router.post("/api/simulator/{basket_id}")
def override_simulation(basket_id: str, item: SimulationModCreate, db: Session = Depends(get_db)):
    db_obj = db.query(database.SimulationMod).filter(
        database.SimulationMod.basket_id == basket_id,
        database.SimulationMod.stock_code == item.stock_code.upper()
    ).first()

    if item.override_type == "remove":
        if db_obj:
            db.delete(db_obj)
    else:
        if db_obj:
            db_obj.override_type = item.override_type
            if item.formula is not None: db_obj.formula = item.formula
            if item.allocation is not None: db_obj.allocation = item.allocation
            if item.buy_price is not None: db_obj.buy_price = item.buy_price
            if item.cmp is not None: db_obj.cmp = item.cmp
        else:
            db_obj = database.SimulationMod(
                basket_id=basket_id,
                stock_code=item.stock_code.upper(),
                override_type=item.override_type,
                formula=item.formula,
                allocation=item.allocation,
                buy_price=item.buy_price,
                cmp=item.cmp
            )
            db.add(db_obj)

    db.commit()
    return {"status": "success"}

@router.get("/api/simulator/{basket_id}/sips")
def get_simulation_sips(basket_id: str, db: Session = Depends(get_db)):
    sips = db.query(database.SimulationSip).filter(database.SimulationSip.basket_id == basket_id).order_by(database.SimulationSip.sip_date.asc()).all()
    return [{"id": s.id, "sip_date": s.sip_date, "amount": s.amount} for s in sips]

@router.post("/api/simulator/{basket_id}/sips")
def add_simulation_sip(basket_id: str, item: SimulationSipCreate, db: Session = Depends(get_db)):
    db_obj = database.SimulationSip(
        basket_id=basket_id,
        sip_date=item.sip_date,
        amount=item.amount
    )
    db.add(db_obj)
    db.commit()
    return {"status": "success", "id": db_obj.id}

@router.delete("/api/simulator/{basket_id}/sips/{sip_id}")
def remove_simulation_sip(basket_id: str, sip_id: int, db: Session = Depends(get_db)):
    db.query(database.SimulationSip).filter(
        database.SimulationSip.basket_id == basket_id,
        database.SimulationSip.id == sip_id
    ).delete()
    db.commit()
    return {"status": "success"}
