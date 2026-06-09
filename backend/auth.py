"""
Authentication — direct email login restricted to @niveshaay.com.
JWT tokens expire at 11:59 PM IST or 24 hours from login, whichever is earlier.
OTP will be added later once SMTP is properly configured.
"""
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
ALLOWED_DOMAIN = os.environ.get("ALLOWED_DOMAIN", "niveshaay.com")
ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL",    "jay.chaudhari@niveshaay.com")
JWT_SECRET     = os.environ.get("JWT_SECRET",     "nia-perf-secret-change-in-prod-32x")
JWT_ALGORITHM  = "HS256"

# ── Pydantic models ───────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email:     str
    latitude:  Optional[float] = None
    longitude: Optional[float] = None

# ── Helpers ───────────────────────────────────────────────────────────────────
import urllib.request
import json as _json

def get_location_from_ip(ip: str) -> str:
    if not ip or ip in ("127.0.0.1", "::1", "localhost"):
        return "Local"
    try:
        with urllib.request.urlopen(f"http://ip-api.com/json/{ip}", timeout=3) as r:
            d = _json.loads(r.read().decode())
            if d.get("status") == "success":
                parts = [d.get("city",""), d.get("regionName",""), d.get("country","")]
                return ", ".join(p for p in parts if p) or "Unknown"
    except Exception:
        pass
    return "Unknown"

def _now_iso() -> str:
    return datetime.utcnow().isoformat()

def create_token(email: str) -> str:
    IST     = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(IST)
    cap_24h = now_ist + timedelta(hours=24)
    eod     = now_ist.replace(hour=23, minute=59, second=59, microsecond=0)
    if eod <= now_ist:
        eod = eod + timedelta(days=1)
    exp_ist = min(cap_24h, eod)
    exp     = exp_ist.astimezone(timezone.utc).replace(tzinfo=None)
    return jwt.encode({"sub": email, "exp": exp}, JWT_SECRET, algorithm=JWT_ALGORITHM)

def verify_token(token: str) -> str:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        email   = payload.get("sub")
        if not email:
            raise ValueError
        return email
    except (JWTError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid or expired token. Please log in again.")

# ── FastAPI dependency ─────────────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)

def get_current_user(creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> str:
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return verify_token(creds.credentials)

def get_location_from_ip_safe(ip):
    try:
        return get_location_from_ip(ip)
    except Exception:
        return "Unknown"

# ── Router ─────────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/direct-login")
def direct_login(body: LoginRequest, request: Request,
                 db: Session = Depends(lambda: __import__('database').SessionLocal())):
    from database import LoginHistory, AllowedEmail
    email = body.email.lower().strip()

    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        raise HTTPException(status_code=403, detail=f"Only @{ALLOWED_DOMAIN} email addresses are allowed.")

    # Admin always allowed — never locked out
    if email != ADMIN_EMAIL:
        if not db.query(AllowedEmail).filter_by(email=email).first():
            raise HTTPException(status_code=403, detail="Your email is not approved for access. Use 'Request Access' on the login page.")

    ip  = request.client.host if request.client else None
    loc = get_location_from_ip_safe(ip)

    try:
        db.add(LoginHistory(email=email, logged_at=_now_iso(), ip_address=ip, location=loc))
        db.commit()
    except Exception:
        pass

    return {"token": create_token(email), "email": email}


@router.get("/me")
def me(current_user: str = Depends(get_current_user)):
    return {"email": current_user, "is_admin": current_user == ADMIN_EMAIL}


@router.post("/logout")
def logout(request: Request,
           db: Session = Depends(lambda: __import__('database').SessionLocal())):
    from database import AuditLog
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        try:
            email = verify_token(auth_header.split(" ", 1)[1])
            ip    = request.client.host if request.client else None
            db.add(AuditLog(user_email=email, event_type="logout",
                            details=None, created_at=_now_iso(),
                            ip_address=ip, location=get_location_from_ip_safe(ip)))
            db.commit()
        except Exception:
            pass
    return {"status": "logged_out"}
