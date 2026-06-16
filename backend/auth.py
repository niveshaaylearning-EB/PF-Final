"""
Authentication — password-based login with OTP email verification.
JWT tokens expire at 11:59 PM IST or 24 hours from login, whichever is earlier.
"""
import os
import hashlib as _hashlib_pw
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
ADMIN_EMAILS   = {"jay.chaudhari@niveshaay.com", "nukul.madaan@niveshaay.com"}

def is_admin_email(email: str) -> bool:
    if not email:
        return False
    return email.lower().strip() in ADMIN_EMAILS

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

def create_token(email: str, first_name: str = "") -> str:
    IST     = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(IST)
    cap_24h = now_ist + timedelta(hours=24)
    eod     = now_ist.replace(hour=23, minute=59, second=59, microsecond=0)
    if eod <= now_ist:
        eod = eod + timedelta(days=1)
    exp_ist = min(cap_24h, eod)
    exp     = exp_ist.astimezone(timezone.utc).replace(tzinfo=None)
    payload: dict = {"sub": email, "exp": exp}
    if first_name:
        payload["fn"] = first_name
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

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

# ── Email OTP ─────────────────────────────────────────────────────────────────
import secrets
import time as _time
import hmac as _hmac_lib
import hashlib as _hashlib

SMTP_FROM      = os.environ.get("SMTP_FROM",      "communication@niveshaay.com")
BREVO_API_KEY  = os.environ.get("BREVO_API_KEY",  "")

# In-memory fallback so OTP verify still works even if DB write failed
# { email: {"code": str, "ts": float, "used": bool} }
_otp_mem: dict = {}

def _generate_otp() -> str:
    return str(secrets.randbelow(900000) + 100000)

# HMAC-based stateless OTP — survives container restarts and multi-worker deployments
_OTP_SECRET = os.environ.get("OTP_SECRET", "") or JWT_SECRET

def _hmac_otp(email: str, minute: int) -> str:
    """Deterministic 6-digit code for (email, UTC-minute). Same result on every worker."""
    key = _OTP_SECRET.encode()
    msg = f"{email}:{minute}".encode()
    h = _hmac_lib.new(key, msg, _hashlib.sha256).hexdigest()
    return str(int(h[-8:], 16) % 900000 + 100000)

def _otp_for_email(email: str) -> str:
    """OTP valid for the current UTC minute (+ window on verify)."""
    return _hmac_otp(email, int(_time.time() // 60))

def _verify_hmac_otp(email: str, code: str, window_minutes: int = 15) -> bool:
    """Returns True if code matches any minute in [now-window, now]."""
    minute = int(_time.time() // 60)
    for offset in range(window_minutes + 1):
        if _hmac_lib.compare_digest(_hmac_otp(email, minute - offset), code):
            return True
    return False

def send_email_otp(to_email: str, code: str):
    """Send OTP via Brevo transactional email API (HTTPS port 443 — works on all clouds)."""
    print(f"[EMAIL-OTP] Code for {to_email}: {code}")   # always log as fallback
    if not BREVO_API_KEY:
        raise RuntimeError("BREVO_API_KEY not configured")
    import requests as _req
    resp = _req.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "api-key":      BREVO_API_KEY,
            "Content-Type": "application/json",
            "Accept":       "application/json",
        },
        json={
            "sender":      {"email": SMTP_FROM, "name": "NIA Performance Center"},
            "to":          [{"email": to_email}],
            "subject":     f"NIA Login Code: {code}",
            "textContent": (
                f"Hello,\n\n"
                f"Your NIA Performance Center login code is:\n\n"
                f"    {code}\n\n"
                f"This code expires in 10 minutes.\n"
                f"If you didn't request this, ignore this email.\n\n"
                f"— NIA Tech Team"
            ),
        },
        timeout=10,
    )
    resp.raise_for_status()
    print(f"[EMAIL-OTP] Email sent via Brevo API to {to_email}")


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
    from database import AllowedEmail, LoginHistory
    email = body.email.lower().strip()

    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        raise HTTPException(status_code=403, detail=f"Only @{ALLOWED_DOMAIN} email addresses are allowed.")

    # Auto-seed allowed email if missing (any @niveshaay.com is allowed now)
    allowed = db.query(AllowedEmail).filter_by(email=email).first()
    if not allowed:
        from datetime import datetime
        allowed = AllowedEmail(email=email, added_by="system", added_at=datetime.utcnow().isoformat())
        db.add(allowed)
        db.commit()
        # Sync to disk & GitHub in background
        threading.Thread(target=_push_allowed_emails, daemon=True).start()

    # Add LoginHistory
    ip = request.client.host if request.client else None
    loc = get_location_from_ip_safe(ip)
    try:
        db.add(LoginHistory(email=email, logged_at=_now_iso(), ip_address=ip, location=loc))
        db.commit()
        # Sync login history in background
        threading.Thread(target=_push_login, daemon=True).start()
    except Exception:
        pass

    return {"token": create_token(email), "email": email}


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
    return {"email": current_user, "is_admin": is_admin_email(current_user)}


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


# ── Email OTP Endpoints ────────────────────────────────────────────────────────

class EmailOtpRequest(BaseModel):
    email: str

class EmailOtpVerify(BaseModel):
    email: str
    code:  str

class RegisterRequest(BaseModel):
    first_name: str
    last_name:  str
    email:      str
    password:   str

class RegisterVerifyRequest(BaseModel):
    first_name: str
    last_name:  str
    email:      str
    password:   str
    code:       str

class PasswordLoginRequest(BaseModel):
    email:    str
    password: str
    latitude:  Optional[float] = None
    longitude: Optional[float] = None

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    email:        str
    code:         str
    new_password: str


@router.post("/request-email-otp")
def request_email_otp(body: EmailOtpRequest,
                       db: Session = Depends(lambda: __import__('database').SessionLocal())):
    """Send a 6-digit OTP to the user's email. Public endpoint — no token needed."""
    try:
        from database import AllowedEmail
        email = body.email.lower().strip()

        if not email.endswith(f"@{ALLOWED_DOMAIN}"):
            raise HTTPException(400, detail=f"Only @{ALLOWED_DOMAIN} emails allowed.")

        # Admin always allowed; others must be pre-approved
        if email != ADMIN_EMAIL:
            try:
                allowed = db.query(AllowedEmail).filter_by(email=email).first()
            except Exception:
                allowed = None
            if not allowed:
                raise HTTPException(403, detail="Your email is not approved. Request access from the login page.")

        # Use HMAC-based OTP: same code on any worker, no shared state needed
        code = _otp_for_email(email)

        # Also store in memory (backup for same-worker verify)
        _otp_mem[email] = {"code": code, "ts": _time.time(), "used": False}

        # Store in DB (works across workers); wrapped so a DB hiccup never returns 500
        try:
            from database import OtpCode
            db.query(OtpCode).filter(OtpCode.email == email).delete()
            db.add(OtpCode(email=email, code=code, created_at=datetime.utcnow().isoformat(), used=0))
            db.commit()
        except Exception as db_err:
            print(f"[EMAIL-OTP] DB store failed for {email}: {db_err} — using memory fallback")
            try:
                db.rollback()
            except Exception:
                pass
    except HTTPException:
        raise
    except Exception as outer_err:
        # Last resort: still generate and send a code so user is never stuck
        print(f"[EMAIL-OTP] Outer error for {getattr(body, 'email', '?')}: {outer_err}")
        try:
            email = body.email.lower().strip()
            code  = _otp_for_email(email)
            _otp_mem[email] = {"code": code, "ts": _time.time(), "used": False}
        except Exception:
            return {"status": "error", "message": "Service starting up. Please try again in 30 seconds."}

    # Run SMTP in a thread with a hard timeout so a slow/wrong server
    # never blocks the endpoint for more than 12 seconds.
    import concurrent.futures as _cf
    email_sent = False
    email_error = ""
    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
            _pool.submit(send_email_otp, email, code).result(timeout=12)
        email_sent = True
    except _cf.TimeoutError:
        email_error = "SMTP timed out"
        print(f"[EMAIL-OTP] Timed out for {email}")
    except Exception as e:
        email_error = str(e)
        print(f"[EMAIL-OTP] Failed for {email}: {e}")

    if email_sent:
        return {"status": "sent", "message": f"OTP sent to {email}. Check your inbox."}
    else:
        # Email failed — return code directly (admin fallback) + show hint
        return {
            "status": "sent",
            "message": f"Email delivery failed. Your OTP code is: {code}",
            "code": code,   # shown on screen so user can still log in
            "error": email_error,
        }


@router.post("/verify-email-otp")
def verify_email_otp(body: EmailOtpVerify, request: Request,
                      db: Session = Depends(lambda: __import__('database').SessionLocal())):
    """Verify the email OTP and return a JWT token."""
    from database import LoginHistory, OtpCode
    email = body.email.lower().strip()
    code  = body.code.strip()

    cutoff = (datetime.utcnow() - timedelta(minutes=10)).isoformat()
    record = db.query(OtpCode).filter(
        OtpCode.email == email,
        OtpCode.code  == code,
        OtpCode.used  == 0,
        OtpCode.created_at >= cutoff,
    ).first()

    # Fallback 1: in-memory (same worker, covers DB-write failures)
    mem_hit = False
    if not record:
        mem = _otp_mem.get(email)
        if (mem and mem["code"] == code and not mem["used"]
                and (_time.time() - mem["ts"]) < 600):
            mem_hit = True

    # Fallback 2: HMAC stateless check (works across workers and container restarts)
    hmac_hit = False
    if not record and not mem_hit:
        hmac_hit = _verify_hmac_otp(email, code)

    if not record and not mem_hit and not hmac_hit:
        raise HTTPException(401, detail="Invalid or expired OTP. Please request a new one.")

    # Mark as used in DB and memory to prevent replay
    if record:
        record.used = 1
        try:
            db.commit()
        except Exception:
            pass
    if email in _otp_mem:
        _otp_mem[email]["used"] = True
    # For HMAC hits: store a "used" marker so same code can't replay on this worker
    if hmac_hit:
        _otp_mem[email] = {"code": code, "ts": _time.time(), "used": True}

    ip  = request.client.host if request.client else None
    loc = get_location_from_ip_safe(ip)
    try:
        db.add(LoginHistory(email=email, logged_at=_now_iso(), ip_address=ip, location=loc))
        db.commit()
    except Exception:
        pass

    # Push login history in background
    threading.Thread(target=_push_login, daemon=True).start()

    return {"token": create_token(email), "email": email}


@router.get("/test-smtp")
def test_smtp():
    """Test Brevo API email — returns success or detailed error. Admin debug only."""
    import requests as _req
    to = ADMIN_EMAIL
    try:
        resp = _req.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            json={
                "sender":      {"email": SMTP_FROM, "name": "NIA Performance Center"},
                "to":          [{"email": to}],
                "subject":     "NIA Email Test",
                "textContent": f"Brevo API test from {SMTP_FROM}",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return {"ok": True, "msg": f"Test email sent to {to} via Brevo API", "status": resp.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e), "api_key_set": bool(BREVO_API_KEY)}


# ── Password helpers ──────────────────────────────────────────────────────────

import re as _re

def _hash_password(password: str) -> str:
    """PBKDF2-SHA256 with random salt. Returns 'salt_hex:key_hex'."""
    import os as _os
    salt = _os.urandom(32)
    key  = _hashlib_pw.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + ":" + key.hex()

def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, key_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        key  = _hashlib_pw.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
        return key.hex() == key_hex
    except Exception:
        return False

def _validate_password(password: str) -> str | None:
    """Returns an error message string if invalid, None if valid."""
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if not _re.search(r"[A-Z]", password):
        return "Password must contain at least one uppercase letter."
    if not _re.search(r"[0-9]", password):
        return "Password must contain at least one number."
    if not _re.search(r"[^A-Za-z0-9]", password):
        return "Password must contain at least one special character."
    return None


# ── Registration ──────────────────────────────────────────────────────────────

@router.post("/register")
def register(body: RegisterRequest,
             db: Session = Depends(lambda: __import__('database').SessionLocal())):
    """Step 1: validate inputs and send OTP to verify email ownership."""
    from database import AllowedEmail
    email = body.email.lower().strip()

    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        raise HTTPException(400, detail=f"Only @{ALLOWED_DOMAIN} email addresses are allowed.")

    if not body.first_name.strip() or not body.last_name.strip():
        raise HTTPException(400, detail="First name and last name are required.")

    err = _validate_password(body.password)
    if err:
        raise HTTPException(400, detail=err)

    existing = db.query(AllowedEmail).filter_by(email=email).first()
    if existing and existing.password_hash:
        raise HTTPException(409, detail="An account with this email already exists. Please log in.")

    # Send verification OTP
    code = _otp_for_email(email)
    _otp_mem[email] = {"code": code, "ts": _time.time(), "used": False}
    try:
        from database import OtpCode
        db.query(OtpCode).filter(OtpCode.email == email).delete()
        db.add(OtpCode(email=email, code=code, created_at=datetime.utcnow().isoformat(), used=0))
        db.commit()
    except Exception:
        try: db.rollback()
        except: pass

    import concurrent.futures as _cf
    email_sent = False
    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
            _pool.submit(send_email_otp, email, code).result(timeout=12)
        email_sent = True
    except Exception as e:
        print(f"[REGISTER] Email failed for {email}: {e}")

    if email_sent:
        return {"status": "otp_sent", "message": f"Verification code sent to {email}."}
    else:
        return {"status": "otp_sent", "message": f"Email delivery failed. Your code: {code}", "code": code}


@router.post("/register/complete")
def register_complete(body: RegisterVerifyRequest,
                      db: Session = Depends(lambda: __import__('database').SessionLocal())):
    """Step 2: verify OTP and create the account."""
    from database import AllowedEmail, OtpCode
    email = body.email.lower().strip()
    code  = body.code.strip()

    err = _validate_password(body.password)
    if err:
        raise HTTPException(400, detail=err)

    # Verify OTP (same three-tier check)
    cutoff = (datetime.utcnow() - timedelta(minutes=10)).isoformat()
    record = db.query(OtpCode).filter(
        OtpCode.email == email, OtpCode.code == code,
        OtpCode.used == 0, OtpCode.created_at >= cutoff,
    ).first()
    mem_hit  = False
    hmac_hit = False
    if not record:
        mem = _otp_mem.get(email)
        if mem and mem["code"] == code and not mem["used"] and (_time.time() - mem["ts"]) < 600:
            mem_hit = True
    if not record and not mem_hit:
        hmac_hit = _verify_hmac_otp(email, code)
    if not record and not mem_hit and not hmac_hit:
        raise HTTPException(401, detail="Invalid or expired code. Please request a new one.")

    # Mark OTP used
    if record:
        record.used = 1
        try: db.commit()
        except: pass
    if email in _otp_mem:
        _otp_mem[email]["used"] = True

    # Create or update account
    existing = db.query(AllowedEmail).filter_by(email=email).first()
    pw_hash  = _hash_password(body.password)
    fn = body.first_name.strip()
    ln = body.last_name.strip()
    if existing:
        existing.first_name    = fn
        existing.last_name     = ln
        existing.password_hash = pw_hash
    else:
        db.add(AllowedEmail(
            email=email, added_by="self-registered",
            added_at=datetime.utcnow().isoformat(),
            first_name=fn, last_name=ln, password_hash=pw_hash,
        ))
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail="Could not save account. Please try again.")

    threading.Thread(target=_push_allowed_emails, daemon=True).start()
    return {"status": "registered", "token": create_token(email, fn), "email": email,
            "first_name": fn, "last_name": ln}


# ── Password login ────────────────────────────────────────────────────────────

@router.post("/login")
def password_login(body: PasswordLoginRequest, request: Request,
                   db: Session = Depends(lambda: __import__('database').SessionLocal())):
    """Login with email + password."""
    from database import AllowedEmail, LoginHistory
    email = body.email.lower().strip()

    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        raise HTTPException(400, detail=f"Only @{ALLOWED_DOMAIN} email addresses are allowed.")

    user = db.query(AllowedEmail).filter_by(email=email).first()
    if not user or not user.password_hash:
        raise HTTPException(401, detail="No account found. Please register first.")

    if not _verify_password(body.password, user.password_hash):
        raise HTTPException(401, detail="Incorrect password.")

    ip  = request.client.host if request.client else None
    loc = get_location_from_ip_safe(ip)
    try:
        db.add(LoginHistory(email=email, logged_at=_now_iso(), ip_address=ip, location=loc))
        db.commit()
    except Exception:
        pass
    threading.Thread(target=_push_login, daemon=True).start()

    fn = user.first_name or ""
    return {"token": create_token(email, fn), "email": email,
            "first_name": fn, "last_name": user.last_name or ""}


# ── Forgot / reset password ───────────────────────────────────────────────────

@router.post("/forgot-password")
def forgot_password(body: ForgotPasswordRequest,
                    db: Session = Depends(lambda: __import__('database').SessionLocal())):
    """Send OTP to reset password. Works for any registered @niveshaay.com email."""
    from database import AllowedEmail
    email = body.email.lower().strip()

    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        raise HTTPException(400, detail=f"Only @{ALLOWED_DOMAIN} email addresses are allowed.")

    user = db.query(AllowedEmail).filter_by(email=email).first()
    if not user:
        # Don't reveal if email exists; still return success to prevent enumeration
        return {"status": "otp_sent", "message": f"If that email is registered, a code has been sent."}

    code = _otp_for_email(email)
    _otp_mem[email] = {"code": code, "ts": _time.time(), "used": False}
    try:
        from database import OtpCode
        db.query(OtpCode).filter(OtpCode.email == email).delete()
        db.add(OtpCode(email=email, code=code, created_at=datetime.utcnow().isoformat(), used=0))
        db.commit()
    except Exception:
        try: db.rollback()
        except: pass

    import concurrent.futures as _cf
    email_sent = False
    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
            _pool.submit(send_email_otp, email, code).result(timeout=12)
        email_sent = True
    except Exception as e:
        print(f"[FORGOT-PW] Email failed for {email}: {e}")

    if email_sent:
        return {"status": "otp_sent", "message": f"Password reset code sent to {email}."}
    else:
        return {"status": "otp_sent", "message": f"Email delivery failed. Your code: {code}", "code": code}


@router.post("/reset-password")
def reset_password(body: ResetPasswordRequest,
                   db: Session = Depends(lambda: __import__('database').SessionLocal())):
    """Verify OTP and set new password."""
    from database import AllowedEmail, OtpCode
    email = body.email.lower().strip()
    code  = body.code.strip()

    err = _validate_password(body.new_password)
    if err:
        raise HTTPException(400, detail=err)

    # Verify OTP
    cutoff = (datetime.utcnow() - timedelta(minutes=10)).isoformat()
    record = db.query(OtpCode).filter(
        OtpCode.email == email, OtpCode.code == code,
        OtpCode.used == 0, OtpCode.created_at >= cutoff,
    ).first()
    mem_hit  = False
    hmac_hit = False
    if not record:
        mem = _otp_mem.get(email)
        if mem and mem["code"] == code and not mem["used"] and (_time.time() - mem["ts"]) < 600:
            mem_hit = True
    if not record and not mem_hit:
        hmac_hit = _verify_hmac_otp(email, code)
    if not record and not mem_hit and not hmac_hit:
        raise HTTPException(401, detail="Invalid or expired code. Please request a new one.")

    if record:
        record.used = 1
        try: db.commit()
        except: pass
    if email in _otp_mem:
        _otp_mem[email]["used"] = True

    user = db.query(AllowedEmail).filter_by(email=email).first()
    if not user:
        raise HTTPException(404, detail="Account not found.")

    user.password_hash = _hash_password(body.new_password)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(500, detail="Could not update password. Please try again.")

    threading.Thread(target=_push_allowed_emails, daemon=True).start()
    return {"status": "password_reset", "message": "Password updated successfully. You can now log in."}
