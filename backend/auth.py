"""
Authentication: OTP-based email login restricted to @niveshaay.com.
JWT tokens are issued on successful OTP verification and must be sent
as  Authorization: Bearer <token>  on every /api/* request.
"""
import os, secrets, smtplib
from email.mime.text import MIMEText
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
ALLOWED_DOMAIN  = os.environ.get("ALLOWED_DOMAIN", "niveshaay.com")
ADMIN_EMAIL     = os.environ.get("ADMIN_EMAIL", "jay.chaudhari@niveshaay.com")
JWT_SECRET      = os.environ.get("JWT_SECRET", "nia-perf-secret-change-in-prod-32x")
JWT_ALGORITHM   = "HS256"
JWT_EXPIRE_DAYS = 7
OTP_EXPIRE_MINS = 10
MAX_OTP_ATTEMPTS = 5   # per email per OTP_EXPIRE_MINS window

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)
SMTP_USE_SSL = os.environ.get("SMTP_USE_SSL", "false").lower() == "true"  # port 465 mode

# ── Pydantic models ───────────────────────────────────────────────────────────
class OtpRequest(BaseModel):
    email: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None

class OtpVerify(BaseModel):
    email: str
    code: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None

# ── Helpers ───────────────────────────────────────────────────────────────────
import urllib.request
import json as _json

def get_location_from_ip(ip: str) -> str:
    if not ip or ip in ("127.0.0.1", "::1", "localhost"):
        return "Local/Loopback"
    
    # Private IP range check
    ip_parts = ip.split('.')
    if len(ip_parts) == 4:
        try:
            p0, p1 = int(ip_parts[0]), int(ip_parts[1])
            if p0 == 10 or (p0 == 192 and p1 == 168) or (p0 == 172 and 16 <= p1 <= 31):
                return "Private Network"
        except Exception:
            pass

    try:
        url = f"http://ip-api.com/json/{ip}"
        with urllib.request.urlopen(url, timeout=3) as response:
            data = _json.loads(response.read().decode())
            if data.get("status") == "success":
                city = data.get("city", "")
                region = data.get("regionName", "")
                country = data.get("country", "")
                parts = [p for p in (city, region, country) if p]
                return ", ".join(parts) if parts else "Unknown Location"
    except Exception as e:
        print(f"[IP Lookup] Error resolving location for {ip}: {e}")
    return "Unknown Location"

def reverse_geocode_location(lat: float, lon: float) -> str:
    if lat is None or lon is None:
        return "Unknown Location"
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=18&addressdetails=1"
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'NIA-Performance-Center/1.0 (contact@niveshaay.com)'}
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            data = _json.loads(response.read().decode())
            addr = data.get("address", {})
            if addr:
                road = addr.get("road")
                suburb = addr.get("suburb") or addr.get("neighbourhood") or addr.get("village")
                city = addr.get("city") or addr.get("town") or addr.get("county")
                state = addr.get("state")
                pincode = addr.get("postcode")
                country = addr.get("country")
                
                parts = []
                if road:
                    parts.append(road)
                if suburb:
                    parts.append(suburb)
                if city:
                    parts.append(city)
                if state:
                    parts.append(state)
                if pincode:
                    if parts and parts[-1] == state:
                        parts[-1] = f"{state} {pincode}"
                    else:
                        parts.append(pincode)
                if country:
                    parts.append(country)
                
                if parts:
                    return ", ".join(parts)
            
            display_name = data.get("display_name")
            if display_name:
                return display_name
    except Exception as e:
        print(f"[Reverse Geocode] Error for ({lat}, {lon}): {e}")
    return "Unknown Location"

def _now_iso() -> str:
    return datetime.utcnow().isoformat()

def send_otp_email(email: str, code: str):
    # Always print to console for backup/debugging/fallback
    print(f"\n{'='*50}")
    print(f"[OTP] Code generated for {email} -> {code}")
    print(f"{'='*50}\n")

    if not SMTP_USER or not SMTP_PASS or SMTP_PASS == "YOUR_EMAIL_PASSWORD_HERE":
        return

    body = (
        f"Hello,\n\n"
        f"Your NIA Performance Center login code is:\n\n"
        f"    {code}\n\n"
        f"This code is valid for {OTP_EXPIRE_MINS} minutes.\n"
        f"If you did not request this, please ignore.\n\n"
        f"— NIA Tech Team"
    )
    msg = MIMEText(body)
    msg["Subject"] = f"NIA Login Code: {code}"
    msg["From"]    = SMTP_FROM
    msg["To"]      = email

    try:
        if SMTP_USE_SSL:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=8) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(SMTP_FROM, [email], msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=8) as s:
                s.ehlo()
                s.starttls()
                s.ehlo()
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(SMTP_FROM, [email], msg.as_string())
        print(f"[OTP] Email sent successfully to {email}")
    except Exception as e:
        # Email failed — OTP still printed to console above, do NOT block login
        print(f"[OTP] Email delivery failed ({e}). Code visible in logs above.")

def create_token(email: str) -> str:
    IST = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(IST)

    # Cap 1: 24 hours from login
    cap_24h = now_ist + timedelta(hours=24)

    # Cap 2: 11:59:59 PM IST of the current day; if already past, use tomorrow
    eod = now_ist.replace(hour=23, minute=59, second=59, microsecond=0)
    if eod <= now_ist:
        eod = eod + timedelta(days=1)

    # Expire at whichever comes first
    exp_ist = min(cap_24h, eod)
    exp = exp_ist.astimezone(timezone.utc).replace(tzinfo=None)
    return jwt.encode({"sub": email, "exp": exp}, JWT_SECRET, algorithm=JWT_ALGORITHM)

def verify_token(token: str) -> str:
    """Returns email or raises HTTPException 401."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        email = payload.get("sub")
        if not email:
            raise ValueError
        return email
    except (JWTError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid or expired token. Please log in again.")

# ── FastAPI dependency ────────────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)

def get_current_user(creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> str:
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return verify_token(creds.credentials)

# ── Router ────────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/request-otp")
def request_otp(body: OtpRequest, db: Session = Depends(lambda: __import__('database').SessionLocal())):
    email = body.email.lower().strip()
    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        raise HTTPException(status_code=403, detail=f"Only @{ALLOWED_DOMAIN} email addresses are allowed.")

    from database import OtpCode, AllowedEmail
    
    # Check if the email is approved for access
    allowed = db.query(AllowedEmail).filter_by(email=email).first()
    if not allowed:
        raise HTTPException(status_code=403, detail="Your email is not approved for access.")

    cutoff = (datetime.utcnow() - timedelta(minutes=OTP_EXPIRE_MINS)).isoformat()
    recent = db.query(OtpCode).filter(
        OtpCode.email == email,
        OtpCode.created_at >= cutoff,
    ).count()
    if recent >= MAX_OTP_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many OTP requests. Please wait 10 minutes.")

    code = str(secrets.randbelow(900000) + 100000)
    db.add(OtpCode(email=email, code=code, created_at=_now_iso(), used=0))
    db.commit()

    try:
        send_otp_email(email, code)
    except Exception as e:
        print(f"[SMTP ERROR] Failed to send email to {email}: {e}")
        return {"status": "sent", "message": f"OTP generated. (Warning: Email delivery failed, check server console logs)"}

    return {"status": "sent", "message": f"OTP sent to {email}"}


@router.post("/verify-otp")
def verify_otp(body: OtpVerify, request: Request, db: Session = Depends(lambda: __import__('database').SessionLocal())):
    from database import OtpCode, LoginHistory, AllowedEmail
    email  = body.email.lower().strip()

    # Check if approved
    allowed = db.query(AllowedEmail).filter_by(email=email).first()
    if not allowed:
        raise HTTPException(status_code=403, detail="Your email is not approved for access.")

    cutoff = (datetime.utcnow() - timedelta(minutes=OTP_EXPIRE_MINS)).isoformat()

    record = db.query(OtpCode).filter(
        OtpCode.email      == email,
        OtpCode.code       == body.code.strip(),
        OtpCode.used       == 0,
        OtpCode.created_at >= cutoff,
    ).order_by(OtpCode.created_at.desc()).first()

    if not record:
        raise HTTPException(status_code=401, detail="Invalid or expired OTP code.")

    record.used = 1
    ip = request.client.host if request.client else None
    
    # Geolocation fallback if coords are not provided
    if body.latitude is not None and body.longitude is not None:
        loc = reverse_geocode_location(body.latitude, body.longitude)
        if not loc or loc == "Unknown Location":
            loc = get_location_from_ip(ip)
    else:
        loc = get_location_from_ip(ip)

    db.add(LoginHistory(email=email, logged_at=_now_iso(), ip_address=ip, location=loc))
    db.commit()

    return {"token": create_token(email), "email": email}


@router.get("/me")
def me(current_user: str = Depends(get_current_user)):
    return {"email": current_user, "is_admin": current_user == ADMIN_EMAIL}


@router.post("/direct-login")
def direct_login(body: OtpRequest, request: Request, db: Session = Depends(lambda: __import__('database').SessionLocal())):
    """Login with email only — restricted to pre-approved @niveshaay.com addresses."""
    from database import LoginHistory, AllowedEmail
    email = body.email.lower().strip()
    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        raise HTTPException(status_code=403, detail=f"Only @{ALLOWED_DOMAIN} email addresses are allowed.")

    # Admin email always allowed — never locked out
    if email != ADMIN_EMAIL:
        allowed = db.query(AllowedEmail).filter_by(email=email).first()
        if not allowed:
            raise HTTPException(status_code=403, detail="Your email is not approved for access. Please request access from the login page.")

    ip = request.client.host if request.client else None
    try:
        loc = get_location_from_ip(ip)
    except Exception:
        loc = "Unknown"

    try:
        db.add(LoginHistory(email=email, logged_at=_now_iso(), ip_address=ip, location=loc))
        db.commit()
    except Exception:
        pass

    return {"token": create_token(email), "email": email}


@router.post("/logout")
def logout(request: Request, db: Session = Depends(lambda: __import__('database').SessionLocal())):
    from database import AuditLog
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        try:
            email = verify_token(auth_header.split(" ", 1)[1])
            ip = request.client.host if request.client else None
            loc = get_location_from_ip(ip)
            db.add(AuditLog(
                user_email=email,
                event_type="logout",
                details=None,
                created_at=_now_iso(),
                ip_address=ip,
                location=loc,
            ))
            db.commit()
        except Exception:
            pass
    return {"status": "logged_out"}
