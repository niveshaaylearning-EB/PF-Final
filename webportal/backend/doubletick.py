"""DoubleTick (WhatsApp Business API) integration -- sends a WhatsApp alert
to the research team whenever a basket rebalance is detected, listing which
stocks were added/removed/increased/decreased. Uses the pre-approved
"update_smallcase" template (2 placeholders: basket name, stock-change
summary) via DoubleTick's template-send endpoint, since Direct Send (plain
free-form text) is confirmed NOT provisioned on this account (live 422
DIRECT_SEND_NOT_ELIGIBLE, 2026-10-01) -- a WhatsApp template is required for
business-initiated messages here."""
import json
import os
from pathlib import Path

import httpx
from _shared_http import SHARED_SSL_CONTEXT

_API_URL = "https://public.doubletick.io/whatsapp/message/template"
_TEMPLATE_NAME = "update_smallcase"
_TEMPLATE_LANGUAGE = "en"
# The org's connected WABA number (GET /organization/channel/profile,
# confirmed live 2026-10-01: channelId intg_3w0bdNoptA, status CONNECTED).
_FROM_NUMBER = "+917859870559"

# Admin-managed via the (renamed) Admin Panel page -- same file
# backend/main.py's /api/admin/whatsapp-recipients endpoints read/write,
# referenced here by absolute path since this file's own cwd differs
# between local dev (webportal runs standalone) and production (mounted
# into the main app's process). This file at this path is the ROOT backend
# directory, not webportal's own -- doubletick.py:/webportal/backend/
# -> parent.parent.parent = project root -> /backend/whatsapp_recipients.json.
_RECIPIENTS_FILE = Path(__file__).resolve().parent.parent.parent / "backend" / "whatsapp_recipients.json"


def get_recipients() -> list[dict]:
    """[{"name","phone","email"}, ...] from the admin-managed list, falling
    back to DOUBLETICK_RESEARCH_RECIPIENTS (comma-separated phone numbers,
    no names) if that file doesn't exist yet or is empty -- keeps the
    original test-number setup working until someone adds real recipients
    through the UI."""
    try:
        if _RECIPIENTS_FILE.exists():
            data = json.loads(_RECIPIENTS_FILE.read_text(encoding="utf-8"))
            if data:
                return data
    except Exception as e:
        print(f"[doubletick] could not read {_RECIPIENTS_FILE}: {e}")
    fallback = os.environ.get("DOUBLETICK_RESEARCH_RECIPIENTS", "")
    return [{"name": None, "phone": p.strip(), "email": None} for p in fallback.split(",") if p.strip()]


def _format_change(entry: dict, kind: str) -> str:
    """Plain-sentence style to match the approved template's wording
    (Jay's example: "reliance reduced by 5%, xyz increased by 3%")."""
    name = entry.get("companyName", "?")
    new_w = entry.get("newWeight")
    delta = entry.get("delta")
    if kind == "added":
        return f"{name} added at {new_w}% weight"
    if kind == "removed":
        return f"{name} removed from portfolio"
    old_w = (new_w - delta) if (new_w is not None and delta is not None) else None
    verb = "increased" if kind == "increased" else "reduced"
    pct = abs(delta) if delta is not None else None
    if pct is not None and old_w is not None:
        return f"{name} {verb} by {pct:.2f}% ({old_w:.2f}% -> {new_w}%)"
    if pct is not None:
        return f"{name} {verb} by {pct:.2f}%"
    return f"{name} {verb} (now {new_w}%)"


def summarize_changes(preview: dict) -> str:
    """preview is the same added/removed/increased/decreased shape produced
    by portfolio_report._build_preview (and basket-rebalance-history's
    per-date `changes` list, remapped by the caller)."""
    lines = []
    for e in preview.get("added", []):
        lines.append(_format_change(e, "added"))
    for e in preview.get("increased", []):
        lines.append(_format_change(e, "increased"))
    for e in preview.get("decreased", []):
        lines.append(_format_change(e, "decreased"))
    for e in preview.get("removed", []):
        lines.append(_format_change(e, "removed"))
    return "\n".join(lines) if lines else "No stock-level changes detected."


async def send_rebalance_alert(to: str, basket_label: str, preview: dict) -> dict:
    """Sends one WhatsApp template message summarizing a rebalance's stock
    changes to `to` (E.164 format, e.g. "+919537407484"). Returns
    {"ok": bool, "status": int, "response": dict} -- never raises, so a
    WhatsApp failure never blocks the caller's own (more important) work."""
    key = os.environ.get("DOUBLETICK_API_KEY")
    if not key:
        return {"ok": False, "error": "DOUBLETICK_API_KEY not set"}

    stock_summary = summarize_changes(preview)
    payload = {
        "messages": [{
            "from": _FROM_NUMBER,
            "to": to,
            "content": {
                "templateName": _TEMPLATE_NAME,
                "language": _TEMPLATE_LANGUAGE,
                "templateData": {"body": {"placeholders": [basket_label, stock_summary]}},
            },
        }],
    }
    try:
        async with httpx.AsyncClient(
            verify=SHARED_SSL_CONTEXT,timeout=20) as client:
            resp = await client.post(
                _API_URL,
                headers={"Authorization": key, "Content-Type": "application/json", "Accept": "application/json"},
                json=payload,
            )
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return {"ok": resp.status_code < 300, "status": resp.status_code, "response": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}
