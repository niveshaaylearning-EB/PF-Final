import io
import secrets
import string
import hashlib
import json
import pyotp
import qrcode
import qrcode.image.svg

def generate_totp_secret() -> str:
    """Generate a random Base32 secret for TOTP (Google Authenticator)."""
    return pyotp.random_base32()

def generate_totp_qr_svg(email: str, secret: str) -> str:
    """
    Generate an SVG QR code for TOTP provisioning.
    Returns the SVG content as a string.
    """
    # Create the provisioning URI
    uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=email.strip().lower(),
        issuer_name="NIA Performance Center"
    )
    
    # Generate SVG QR code using path-based factory (most compatible with browsers)
    factory = qrcode.image.svg.SvgPathImage
    img = qrcode.make(uri, image_factory=factory, box_size=10, border=2)

    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")

    # Add explicit dimensions so the browser renders it correctly
    if 'width=' not in svg:
        svg = svg.replace('<svg ', '<svg width="200" height="200" ', 1)

    return svg

def verify_totp_token(secret: str, token: str) -> bool:
    """
    Verify a 6-digit TOTP token against the secret.
    Allows for clock drift of +/- 1 time step (30 seconds).
    """
    if not secret or not token:
        return False
    
    # Clean token (only digits, strip whitespaces)
    clean_token = "".join(c for c in str(token) if c.isdigit())
    if len(clean_token) != 6:
        return False
        
    totp = pyotp.TOTP(secret)
    # valid_window=1 allows verification of codes from 30 seconds ago/ahead
    return totp.verify(clean_token, valid_window=1)

def generate_backup_codes(count: int = 8) -> tuple[list[str], list[str]]:
    """
    Generate random single-use backup recovery codes.
    Returns:
      - raw_codes: List of strings (e.g. ['ABCD-1234', ...]) to show to the user.
      - hashed_codes: List of hex-encoded SHA-256 hashes to store in the database.
    """
    raw_codes = []
    hashed_codes = []
    chars = string.ascii_uppercase + string.digits
    for _ in range(count):
        # Format: XXXX-XXXX
        part1 = "".join(secrets.choice(chars) for _ in range(4))
        part2 = "".join(secrets.choice(chars) for _ in range(4))
        code = f"{part1}-{part2}"
        raw_codes.append(code)
        
        # Normalize and hash with SHA-256
        normalized = code.upper().replace("-", "").strip()
        hashed = hashlib.sha256(normalized.encode()).hexdigest()
        hashed_codes.append(hashed)
        
    return raw_codes, hashed_codes

def verify_and_consume_backup_code(stored_codes_json: str | None, submitted_code: str) -> tuple[bool, str | None]:
    """
    Check if a submitted backup recovery code is valid and unused.
    If valid, consumes the code and returns (True, updated_codes_json).
    Otherwise returns (False, None).
    """
    if not stored_codes_json or not submitted_code:
        return False, None
        
    try:
        hashes_list = json.loads(stored_codes_json)
        if not isinstance(hashes_list, list):
            return False, None
    except Exception:
        return False, None
        
    # Normalize and hash the submitted code
    normalized = submitted_code.upper().replace("-", "").strip()
    submitted_hash = hashlib.sha256(normalized.encode()).hexdigest()
    
    if submitted_hash in hashes_list:
        # Code is valid! Consume it by removing it from the list
        hashes_list.remove(submitted_hash)
        return True, json.dumps(hashes_list)
        
    return False, None
