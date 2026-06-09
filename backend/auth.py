"""
Authentication — TOTP 2-Factor Authentication login restricted to @niveshaay.com.
JWT tokens expire at 11:59 PM IST or 24 hours from login, whichever is earlier.
"""
import os
from datetime import datetime, timedelta, timezone
import threading
import base64 as _b64
import urllib.request as _ur
import json as _json

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

import totp

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

class VerifyEnrollRequest(BaseModel):
    code: str

class VerifyTotpRequest(BaseModel):
    code: str
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

def create_temp_token(email: str) -> str:
    """Create a short-lived temporary token for 2FA verification flow."""
    exp = datetime.utcnow() + timedelta(minutes=10)
    return jwt.encode({"sub": email, "type": "temp_2fa", "exp": exp}, JWT_SECRET, algorithm=JWT_ALGORITHM)

def verify_temp_token(token: str) -> str:
    """Verify the 2FA temporary token and return the associated email."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "temp_2fa":
            raise ValueError
        email = payload.get("sub")
        if not email:
            raise ValueError
        return email
    except (JWTError, ValueError):
        raise HTTPException(status_code=401, detail="Session expired. Please start login again.")

def get_location_from_ip_safe(ip):
    try:
        return get_location_from_ip(ip)
    except Exception:
        return "Unknown"

def _push_login():
    """Background task to write login_history.json locally and push to GitHub."""
    try:
        from database import SessionLocal, LoginHistory
        db = SessionLocal()
        rows = db.query(LoginHistory).order_by(LoginHistory.id.desc()).limit(500).all()
        data = [{
            "email": r.email,
            "logged_at": r.logged_at,
            "ip_address": r.ip_address,
            "location": r.location
        } for r in rows]
        db.close()
        
        import os as _os
        content = _json.dumps(data, indent=2)
        
        # Write to local file first
        json_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "login_history.json")
        with open(json_path, "w", encoding="utf-8") as f:
            f.write(content)

        token = os.environ.get("GITHUB_TOKEN","")
        repo  = os.environ.get("GITHUB_REPO","")
        if not token or not repo:
            return

        api = f"https://api.github.com/repos/{repo}/contents/backend/login_history.json"
        hdrs = {"Authorization":f"Bearer {token}","Accept":"application/vnd.github+json","Content-Type":"application/json","X-GitHub-Api-Version":"2022-11-28"}
        try:
            sha = _json.loads(_ur.urlopen(_ur.Request(api,headers=hdrs),timeout=8).read())["sha"]
        except Exception:
            sha = None
        body = _json.dumps({"message":"auto: login event","content":_b64.b64encode(content.encode()).decode(),**( {"sha":sha} if sha else {})}).encode()
        _ur.urlopen(_ur.Request(api,data=body,headers=hdrs,method="PUT"),timeout=10)
    except Exception as e:
        print(f"[login-push] {e}")

def _push_allowed_emails():
    """Background task to write allowed_emails_data.json locally and push to GitHub."""
    try:
        from database import SessionLocal, AllowedEmail
        db = SessionLocal()
        rows = db.query(AllowedEmail).all()
        data = [{
            "email": r.email,
            "added_by": r.added_by,
            "added_at": r.added_at,
            "totp_secret": r.totp_secret,
            "totp_enabled": r.totp_enabled,
            "backup_codes": r.backup_codes
        } for r in rows]
        db.close()
        
        import os as _os
        content = _json.dumps(data, indent=2)
        
        # Write to local file first
        json_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "allowed_emails_data.json")
        with open(json_path, "w", encoding="utf-8") as f:
            f.write(content)
            
        token = os.environ.get("GITHUB_TOKEN","")
        repo  = os.environ.get("GITHUB_REPO","")
        if not token or not repo:
            return

        api = f"https://api.github.com/repos/{repo}/contents/backend/allowed_emails_data.json"
        hdrs = {"Authorization":f"Bearer {token}","Accept":"application/vnd.github+json","Content-Type":"application/json","X-GitHub-Api-Version":"2022-11-28"}
        try:
            sha = _json.loads(_ur.urlopen(_ur.Request(api,headers=hdrs),timeout=8).read())["sha"]
        except Exception:
            sha = None
        body = _json.dumps({"message":"auto: update allowed emails TOTP","content":_b64.b64encode(content.encode()).decode(),**( {"sha":sha} if sha else {})}).encode()
        _ur.urlopen(_ur.Request(api,data=body,headers=hdrs,method="PUT"),timeout=10)
    except Exception as e:
        print(f"[allowed-emails-push] {e}")

# ── FastAPI dependency ─────────────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)

def get_current_user(creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> str:
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return verify_token(creds.credentials)

# ── Router ─────────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/direct-login")
def direct_login(body: LoginRequest, request: Request,
                 db: Session = Depends(lambda: __import__('database').SessionLocal())):
    from database import AllowedEmail
    email = body.email.lower().strip()

    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        raise HTTPException(status_code=403, detail=f"Only @{ALLOWED_DOMAIN} email addresses are allowed.")

    # Check if user exists in AllowedEmail database
    allowed = db.query(AllowedEmail).filter_by(email=email).first()
    if email != ADMIN_EMAIL:
        if not allowed:
            raise HTTPException(status_code=403, detail="Your email is not approved for access. Use 'Request Access' on the login page.")
    else:
        # Auto-seed admin if missing
        if not allowed:
            from datetime import datetime
            allowed = AllowedEmail(email=email, added_by="system", added_at=datetime.utcnow().isoformat())
            db.add(allowed)
            db.commit()

    # Determine 2FA state
    if allowed and allowed.totp_enabled == 1:
        return {
            "status": "2fa_required",
            "email": email,
            "temp_token": create_temp_token(email)
        }
    else:
        return {
            "status": "2fa_setup_required",
            "email": email,
            "temp_token": create_temp_token(email)
        }


@router.post("/totp/enroll")
def totp_enroll(authorization: str = Header(...),
                db: Session = Depends(lambda: __import__('database').SessionLocal())):
    """Initialize TOTP enrollment for a user. Returns secret and QR code SVG."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header.")
    temp_token = authorization.split(" ", 1)[1]
    email = verify_temp_token(temp_token)
    
    from database import AllowedEmail
    allowed = db.query(AllowedEmail).filter_by(email=email).first()
    if not allowed:
        raise HTTPException(status_code=404, detail="User not found.")
        
    # Create temporary secret (enabled remains 0)
    secret = totp.generate_totp_secret()
    allowed.totp_secret = secret
    allowed.totp_enabled = 0
    db.commit()
    
    # Sync AllowedEmail JSON to disk & GitHub
    threading.Thread(target=_push_allowed_emails, daemon=True).start()
    
    # Generate QR code SVG
    qr_svg = totp.generate_totp_qr_svg(email, secret)
    
    return {
        "secret": secret,
        "qr_svg": qr_svg
    }


@router.post("/totp/verify-enroll")
def totp_verify_enroll(body: VerifyEnrollRequest,
                      authorization: str = Header(...),
                      db: Session = Depends(lambda: __import__('database').SessionLocal())):
    """Verify first TOTP token to activate 2FA and get backup recovery codes."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header.")
    temp_token = authorization.split(" ", 1)[1]
    email = verify_temp_token(temp_token)
    
    from database import AllowedEmail
    allowed = db.query(AllowedEmail).filter_by(email=email).first()
    if not allowed or not allowed.totp_secret:
        raise HTTPException(status_code=400, detail="2FA enrollment has not been initialized.")
        
    # Verify token
    if not totp.verify_totp_token(allowed.totp_secret, body.code):
        raise HTTPException(status_code=400, detail="Invalid verification code. Scan the QR code again.")
        
    # Activation: generate recovery codes, mark enabled
    raw_codes, hashed_codes = totp.generate_backup_codes()
    allowed.totp_enabled = 1
    allowed.backup_codes = _json.dumps(hashed_codes)
    db.commit()
    
    # Sync AllowedEmail JSON
    threading.Thread(target=_push_allowed_emails, daemon=True).start()
    
    # Log successful login to history
    from database import LoginHistory
    ip = None
    loc = "Local"
    try:
        db.add(LoginHistory(email=email, logged_at=_now_iso(), ip_address=ip, location=loc))
        db.commit()
        threading.Thread(target=_push_login, daemon=True).start()
    except Exception:
        pass
        
    return {
        "status": "enabled",
        "token": create_token(email),
        "email": email,
        "backup_codes": raw_codes
    }


@router.post("/totp/verify")
def totp_verify(body: VerifyTotpRequest,
                request: Request,
                authorization: str = Header(...),
                db: Session = Depends(lambda: __import__('database').SessionLocal())):
    """Verify 6-digit TOTP token or 8-character backup recovery code to log in."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header.")
    temp_token = authorization.split(" ", 1)[1]
    email = verify_temp_token(temp_token)
    
    from database import AllowedEmail, LoginHistory
    allowed = db.query(AllowedEmail).filter_by(email=email).first()
    if not allowed or allowed.totp_enabled != 1 or not allowed.totp_secret:
        raise HTTPException(status_code=400, detail="2FA is not enabled for this user.")
        
    code_str = body.code.strip()
    is_valid = False
    used_backup = False
    
    # Check if 6-digit TOTP code
    if len(code_str) == 6 and code_str.isdigit():
        if totp.verify_totp_token(allowed.totp_secret, code_str):
            is_valid = True
    # Otherwise check if single-use recovery code
    else:
        is_valid, updated_codes = totp.verify_and_consume_backup_code(allowed.backup_codes, code_str)
        if is_valid:
            allowed.backup_codes = updated_codes
            used_backup = True
            
    if not is_valid:
        raise HTTPException(status_code=401, detail="Invalid or expired verification code.")
        
    # Log successful login
    ip = request.client.host if request.client else None
    loc = get_location_from_ip_safe(ip)
    
    now_str = _now_iso()
    try:
        db.add(LoginHistory(email=email, logged_at=now_str, ip_address=ip, location=loc))
        db.commit()
    except Exception:
        pass
        
    # Sync logins
    threading.Thread(target=_push_login, daemon=True).start()
    
    # Sync backup code consumption
    if used_backup:
        threading.Thread(target=_push_allowed_emails, daemon=True).start()
        
    return {
        "token": create_token(email),
        "email": email
    }


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
