"""JSON-file persistence for portfolios, buy-price data, rebalance history,
gains statement, historical index, undo snapshots, and rollback points.

Independent of main.py (no circular imports) so every router can import
directly from here, same pattern as backend/common/ on the main backend.
"""
import json
import time
from pathlib import Path

from fastapi import HTTPException, Request

from portfolio_data import BUY_PRICE_DETAILS, PORTFOLIOS_DATA
from common.admin import is_admin_email
from common.json_store import save_json as _common_save_json

BASKET_DISPLAY_NAMES = {
    "Green_Energy":        "Green Energy",
    "Mid_Small_Cap":       "Mid & Small Cap",
    "IPO_Basket":          "IPO Basket",
    "Trends_Triology":     "Trends Triology",
    "Techstack":           "Techstack",
    "Make_in_India":       "Make in India",
    "Consumer_Trends":     "Consumer Trends",
    "IPO_Recommendations": "IPO Recommendations",
}

_PORTFOLIOS_FILE  = Path(__file__).parent / "portfolios.json"
_BUY_PRICE_FILE   = Path(__file__).parent / "buy_price_data.json"
_RH_FILE          = Path(__file__).parent / "rebalance_history.json"
_GAINS_FILE       = Path(__file__).parent / "gains_statement.json"
_HIST_INDEX_FILE  = Path(__file__).parent / "historical_index.json"
_UNDO_FILE        = Path(__file__).parent / "undo_snapshots.json"
_ROLLBACK_FILE    = Path(__file__).parent / "rollback_points.json"
_MAX_ROLLBACK_PTS = 5
_ACTIVITY_LOG_FILE = Path(__file__).parent / "activity_log.json"

# ── In-memory JSON cache — files are read once then served from RAM ───────────
# Invalidated immediately on every write so stale data is never served.
_portfolios_mem:  dict | None = None
_buy_price_mem:   dict | None = None
_rh_mem:          dict | None = None

# ─────────────────────────────────────────────────────────────────────────────
# Admin auth helpers (JWT decode without jose dependency)
# ─────────────────────────────────────────────────────────────────────────────

def _get_request_email(request: Request) -> str | None:
    import base64 as _b64
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        parts = auth.split(".")
        if len(parts) != 3:
            return None
        padding = 4 - len(parts[1]) % 4
        payload = json.loads(_b64.urlsafe_b64decode(parts[1] + "=" * padding))
        return (payload.get("sub") or "").lower().strip() or None
    except Exception:
        return None

def _require_admin(request: Request) -> str:
    email = _get_request_email(request)
    if not email or not is_admin_email(email):
        raise HTTPException(status_code=403, detail="Admin access required.")
    return email

def _log_activity(action: str, user: str, details: dict):
    from datetime import datetime, timezone
    try:
        log = json.loads(_ACTIVITY_LOG_FILE.read_text()) if _ACTIVITY_LOG_FILE.exists() else []
        log.insert(0, {
            "action": action, "user": user,
            "ts": datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC"),
            "details": details,
        })
        _ACTIVITY_LOG_FILE.write_text(json.dumps(log[:500], indent=2))
    except Exception as e:
        print(f"[activity-log] {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Portfolio persistence
# ─────────────────────────────────────────────────────────────────────────────

def _load_portfolios() -> dict:
    global _portfolios_mem
    if _portfolios_mem is not None:
        return _portfolios_mem
    if _PORTFOLIOS_FILE.exists():
        with open(_PORTFOLIOS_FILE, "r", encoding="utf-8") as f:
            _portfolios_mem = json.load(f)
            return _portfolios_mem
    _portfolios_mem = dict(PORTFOLIOS_DATA)
    return _portfolios_mem


def _save_and_push(file_path: Path, data: dict) -> None:
    """Write JSON to disk and push to GitHub in background (shared with backend/main.py)."""
    _common_save_json(str(file_path), data, f"webportal/backend/{file_path.name}", sync=False)


def _save_portfolios(data: dict) -> None:
    global _portfolios_mem
    _portfolios_mem = data          # update memory cache immediately
    _save_and_push(_PORTFOLIOS_FILE, data)


def _load_buy_price_data() -> dict:
    global _buy_price_mem
    if _buy_price_mem is not None:
        return _buy_price_mem
    if _BUY_PRICE_FILE.exists():
        with open(_BUY_PRICE_FILE, "r", encoding="utf-8") as f:
            _buy_price_mem = json.load(f)
            return _buy_price_mem
    _buy_price_mem = dict(BUY_PRICE_DETAILS)
    return _buy_price_mem


def _save_buy_price_data(data: dict) -> None:
    global _buy_price_mem
    _buy_price_mem = data           # update memory cache immediately
    _save_and_push(_BUY_PRICE_FILE, data)


def _load_rebalance_history() -> dict:
    global _rh_mem
    if _rh_mem is not None:
        return _rh_mem
    if _RH_FILE.exists():
        with open(_RH_FILE, "r", encoding="utf-8") as f:
            _rh_mem = json.load(f)
            return _rh_mem
    _rh_mem = {}
    return _rh_mem


def _save_rebalance_history(data: dict) -> None:
    global _rh_mem
    _rh_mem = data
    _save_and_push(_RH_FILE, data)


def _save_gains(gains: dict) -> None:
    """Write gains_statement.json locally (not pushed to GitHub -- derived/recomputable data)."""
    with open(_GAINS_FILE, "w", encoding="utf-8") as f:
        json.dump(gains, f, indent=2, ensure_ascii=False)


def _load_historical_index() -> dict:
    if not _HIST_INDEX_FILE.exists():
        raise HTTPException(status_code=404, detail="historical_index.json not found")
    with open(_HIST_INDEX_FILE, encoding="utf-8") as f:
        return json.load(f)


def _save_historical_index(hi: dict) -> None:
    with open(_HIST_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(hi, f, indent=2, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# Persistent undo snapshots (per basket, max 10)
# ─────────────────────────────────────────────────────────────────────────────

def _load_undo_snapshots() -> dict:
    try:
        if _UNDO_FILE.exists():
            with open(_UNDO_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_undo_snapshots(data: dict) -> None:
    with open(_UNDO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _auto_save_rollback() -> None:
    """Auto-save full system state before any write. Overwrites the single rollback point.
    Never raises — snapshot failure must never abort the actual operation."""
    try:
        point = {
            "id":               "latest",
            "label":            time.strftime("%d %b %Y %H:%M"),
            "createdAt":        time.strftime("%d %b %Y %H:%M"),
            "portfolios":       json.loads(_PORTFOLIOS_FILE.read_text(encoding="utf-8")),
            "buyPriceData":     json.loads(_BUY_PRICE_FILE.read_text(encoding="utf-8")),
            "rebalanceHistory": json.loads(_RH_FILE.read_text(encoding="utf-8")),
        }
        _ROLLBACK_FILE.write_text(json.dumps([point], ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _push_undo_snapshot(basket: str, label: str = "") -> None:
    """Snapshot current basket state before a destructive change. Keeps last 10."""
    pf = _load_portfolios()
    bp = _load_buy_price_data()
    rh = _load_rebalance_history()
    snapshot = {
        "ts":               time.time(),
        "label":            label or f"snapshot at {time.strftime('%d %b %Y %H:%M')}",
        "stocks":           pf.get(basket, []),
        "sold":             pf.get(f"{basket}_sold", []),
        "buyPriceData":     bp.get(basket, {}),
        "rebalanceHistory": rh.get(basket, []),
    }
    snaps = _load_undo_snapshots()
    basket_snaps = snaps.get(basket, [])
    basket_snaps.append(snapshot)
    snaps[basket] = basket_snaps[-10:]   # keep last 10
    _save_undo_snapshots(snaps)


def _all_nse_codes() -> list:
    seen, codes = set(), []
    for stocks in _load_portfolios().values():
        for s in stocks:
            c = s.get("nseCode", "").strip().upper()
            if c and c not in seen:
                seen.add(c)
                codes.append(c)
    return codes
