"""Single source of truth for who counts as an admin.

Previously duplicated independently in backend/auth.py and
webportal/backend/main.py (and, on the frontend, in
frontend/src/utils/auth.js and frontend/src/pages/ActualPortfolio.jsx).
"""
import sqlite3
from pathlib import Path

ADMIN_EMAILS = {
    "jay.chaudhari@niveshaay.com",
    "nukul.madaan@niveshaay.com",
    "nakshatra.rathi@niveshaay.com",
}

# Deliberately NOT importing backend/database.py's engine here -- its
# connection string is a *relative* path ("sqlite:///./portfolio.db"),
# resolved against whichever process's cwd first imports it. The main
# backend runs with cwd=backend/ (correct), but webportal runs with
# cwd=webportal/backend/ -- reusing that engine from webportal's process
# would silently create/query a second, empty portfolio.db there instead of
# the real one (the same class of cwd-dependent bug that broke Playwright's
# browser launch earlier in this app's history). Going straight at the file
# via its own absolute path, from this file's own known location, sidesteps
# that regardless of which process calls in.
_DB_PATH = Path(__file__).resolve().parent.parent / "portfolio.db"


def _is_db_admin(email: str) -> bool:
    try:
        conn = sqlite3.connect(str(_DB_PATH))
        try:
            row = conn.execute(
                "SELECT is_admin FROM allowed_emails WHERE lower(email) = ?", (email,)
            ).fetchone()
            return bool(row and row[0])
        finally:
            conn.close()
    except Exception as e:
        print(f"[common.admin] DB admin check failed: {e}")
        return False


def is_admin_email(email: str) -> bool:
    if not email:
        return False
    email = email.lower().strip()
    return email in ADMIN_EMAILS or _is_db_admin(email)
