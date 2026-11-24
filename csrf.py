# csrf.py — Cryptographically Signed CSRF Protection for Thermal Printer UI
import hmac
import time
import secrets
import hashlib
from typing import Optional, Any

CSRF_COOKIE_NAME = "printer_csrf"
CSRF_MAX_AGE_SECONDS = 86400  # 24 hours

def generate_csrf_token(secret: str) -> str:
    """
    Generates a cryptographically signed, timestamped CSRF token.
    Format: <random_hex>.<timestamp>.<hmac_signature>
    """
    if not secret:
        raise ValueError("Secret key must not be empty for CSRF token generation")
    nonce = secrets.token_hex(16)
    ts = str(int(time.time()))
    payload = f"{nonce}.{ts}"
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    return f"{payload}.{sig}"

def validate_csrf_token(token: str, secret: str, max_age: int = CSRF_MAX_AGE_SECONDS) -> bool:
    """
    Validates token format, timestamp freshness, and HMAC signature.
    """
    if not token or not isinstance(token, str) or not secret:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    nonce, ts_str, sig = parts
    try:
        ts = int(ts_str)
    except ValueError:
        return False

    now = int(time.time())
    if now - ts > max_age or ts > now + 300:  # Allow max 5 min future clock drift
        return False

    payload = f"{nonce}.{ts_str}"
    expected_sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    return hmac.compare_digest(sig, expected_sig)

def extract_submitted_csrf(request: Any) -> Optional[str]:
    """
    Extracts CSRF token from:
    1. Header 'X-CSRF-Token'
    2. Query parameter 'csrf' or 'csrf_token'
    3. Form field 'csrf_token' or 'csrf' if form already parsed
    """
    header_val = getattr(request, "headers", {}).get("x-csrf-token", "").strip()
    if header_val:
        return header_val
    query_params = getattr(request, "query_params", {})
    query_val = query_params.get("csrf") or query_params.get("csrf_token")
    if query_val:
        return str(query_val).strip()
    if hasattr(request, "_form") and request._form is not None:
        form_val = request._form.get("csrf_token") or request._form.get("csrf")
        if form_val:
            return str(form_val).strip()
    return None

def verify_csrf(request: Any, secret: str) -> bool:
    """
    Verifies that the submitted CSRF token matches the cookie and is cryptographically valid.
    """
    cookie_token = getattr(request, "cookies", {}).get(CSRF_COOKIE_NAME, "").strip()
    submitted_token = extract_submitted_csrf(request)

    if not cookie_token or not submitted_token:
        return False

    # 1. Verify cryptographic integrity of both
    if not validate_csrf_token(cookie_token, secret):
        return False
    if not validate_csrf_token(submitted_token, secret):
        return False

    # 2. Verify double-submit equality
    return hmac.compare_digest(cookie_token, submitted_token)
