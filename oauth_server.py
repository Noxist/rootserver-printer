# oauth_server.py — RFC-compliant OAuth 2.1 Authorization Server for MCP & ChatGPT
from __future__ import annotations
import os
import time
import json
import base64
import hashlib
import secrets
import hmac
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Request, Response, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from queue_print import _get_db
from logic import log

oauth_router = APIRouter()

ISSUER = "https://printer.nox007.tech"
AUTH_SECRET = os.getenv("APP_API_KEY", secrets.token_hex(32))

# Supported OAuth Scopes
SUPPORTED_SCOPES = ["printer:read", "printer:print", "offline_access"]
DEFAULT_SCOPES = "printer:read printer:print"

# ----------------- MongoDB Collections -----------------

def _clients_coll():
    db = _get_db()
    return db["oauth_clients"] if db is not None else None

def _codes_coll():
    db = _get_db()
    return db["oauth_codes"] if db is not None else None

def _tokens_coll():
    db = _get_db()
    return db["oauth_tokens"] if db is not None else None

# ----------------- Crypto & PKCE Helpers -----------------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")

def _b64url_decode(s: str) -> bytes:
    padding = "=" * (4 - (len(s) % 4)) if len(s) % 4 else ""
    return base64.urlsafe_b64decode(s + padding)

def verify_pkce(code_verifier: str, code_challenge: str, method: str = "S256") -> bool:
    if not code_verifier or not code_challenge:
        return False
    if method == "S256":
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        computed = _b64url_encode(digest)
        return hmac.compare_digest(computed, code_challenge)
    elif method == "plain":
        return hmac.compare_digest(code_verifier, code_challenge)
    return False

def _sign_token(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64url_encode(raw)
    sig = hmac.new(AUTH_SECRET.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}"

def verify_token(token_str: str) -> Optional[Dict[str, Any]]:
    """Verifies a signed OAuth access token and checks revocation in DB."""
    if not token_str or "." not in token_str:
        return None
    try:
        payload_b64, sig_b64 = token_str.rsplit(".", 1)
        expected_sig = hmac.new(AUTH_SECRET.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
        actual_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        
        # Check expiration
        now = time.time()
        if payload.get("exp", 0) < now:
            return None
        
        # Check revocation in DB if available
        jti = payload.get("jti")
        tokens = _tokens_coll()
        if tokens is not None and jti:
            doc = tokens.find_one({"jti": jti})
            if doc and doc.get("revoked"):
                return None
            
        return payload
    except Exception as e:
        log(f"[oauth] Token verification failed: {e}")
        return None

# ----------------- Pre-Registered Well-Known Clients -----------------

# Pre-allow standard ChatGPT and local test clients
DEFAULT_CLIENT_ID = "chatgpt-mcp-client"
DEFAULT_CLIENT_SECRET = secrets.token_hex(24)

# ----------------- Metadata Endpoints -----------------

@oauth_router.get("/.well-known/oauth-authorization-server")
@oauth_router.get("/.well-known/openid-configuration")
async def oauth_authorization_server_metadata():
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/oauth/authorize",
        "token_endpoint": f"{ISSUER}/oauth/token",
        "registration_endpoint": f"{ISSUER}/oauth/register",
        "revocation_endpoint": f"{ISSUER}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": SUPPORTED_SCOPES,
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "service_documentation": "https://printer.nox007.tech/README.md",
    }

@oauth_router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource_metadata():
    """RFC 9207 / MCP Authorization Specification Protected Resource Metadata."""
    return {
        "resource": f"{ISSUER}/mcp",
        "authorization_servers": [ISSUER],
        "scopes_supported": SUPPORTED_SCOPES,
        "bearer_methods_supported": ["header"],
    }

# ----------------- Dynamic Client Registration (RFC 7591) -----------------

@oauth_router.post("/oauth/register")
async def oauth_register_client(request: Request):
    """Dynamic Client Registration used by ChatGPT / MCP clients."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "invalid_request", "error_description": "JSON body expected"})

    client_name = str(body.get("client_name") or "ChatGPT").strip()
    redirect_uris = body.get("redirect_uris", [])
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise HTTPException(status_code=400, detail={"error": "invalid_redirect_uri", "error_description": "redirect_uris list required"})

    client_id = f"client-{secrets.token_hex(12)}"
    client_secret = secrets.token_hex(24)

    client_doc = {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "grant_types": body.get("grant_types", ["authorization_code", "refresh_token"]),
        "response_types": body.get("response_types", ["code"]),
        "scope": body.get("scope", DEFAULT_SCOPES),
        "created_at": time.time(),
    }

    coll = _clients_coll()
    if coll is not None:
        coll.insert_one(client_doc)
    log(f"[oauth] Registered new client: {client_id} ({client_name})")

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "grant_types": client_doc["grant_types"],
        "response_types": client_doc["response_types"],
        "scope": client_doc["scope"],
    }

# ----------------- Authorization Endpoint -----------------

def _find_client(client_id: str) -> Optional[Dict[str, Any]]:
    if client_id in {DEFAULT_CLIENT_ID, "chatgpt", "chatgpt-mcp"}:
        return {
            "client_id": client_id,
            "client_name": "ChatGPT",
            "redirect_uris": ["https://chatgpt.com/api/aip/mcp/oauth/callback", "https://chat.openai.com/aip/oauth/callback", "https://platform.openai.com/oauth/callback", "http://localhost", "https://oauth.pstmn.io/v1/callback"],
        }
    coll = _clients_coll()
    if coll is not None:
        return coll.find_one({"client_id": client_id})
    return None

@oauth_router.get("/oauth/authorize", response_class=HTMLResponse)
async def oauth_authorize_page(
    request: Request,
    response_type: str = "code",
    client_id: str = "",
    redirect_uri: str = "",
    scope: str = DEFAULT_SCOPES,
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "S256",
):
    if response_type != "code":
        raise HTTPException(status_code=400, detail="Only response_type=code is supported")
    if not client_id:
        raise HTTPException(status_code=400, detail="Missing client_id")
    if not redirect_uri:
        raise HTTPException(status_code=400, detail="Missing redirect_uri")

    client = _find_client(client_id)
    # If client not registered in DB yet, create an ad-hoc record for standard OpenAI domains
    if not client:
        if "openai.com" in redirect_uri or "chatgpt.com" in redirect_uri or "localhost" in redirect_uri:
            client = {
                "client_id": client_id,
                "client_name": "ChatGPT / MCP Client",
                "redirect_uris": [redirect_uri],
            }
        else:
            raise HTTPException(status_code=400, detail="Unknown client_id")

    client_name = client.get("client_name", "ChatGPT")

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Thermal Printer &bull; OAuth Authorization</title>
  <style>
    :root {{
      --bg: #0b0f19; --card: #151d2f; --border: #263554;
      --text: #f0f4fc; --muted: #94a3b8; --accent: #3b82f6;
      --ok: #10b981; --err: #ef4444;
    }}
    * {{ box-sizing: border-box; margin:0; padding:0; }}
    body {{
      background: var(--bg); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px;
    }}
    .auth-card {{
      background: var(--card); border: 1px solid var(--border);
      border-radius: 20px; max-width: 440px; width: 100%; padding: 28px;
      box-shadow: 0 10px 40px rgba(0,0,0,0.5);
    }}
    .header {{ text-align: center; margin-bottom: 24px; }}
    .icon {{ font-size: 2.8rem; margin-bottom: 8px; }}
    .title {{ font-size: 1.4rem; font-weight: 700; }}
    .sub {{ color: var(--muted); font-size: 0.95rem; margin-top: 4px; }}
    .app-box {{
      background: rgba(59,130,246,0.08); border: 1px solid rgba(59,130,246,0.25);
      border-radius: 12px; padding: 14px 16px; margin-bottom: 20px;
    }}
    .scopes-list {{ margin: 16px 0; }}
    .scope-item {{
      display: flex; align-items: flex-start; gap: 10px; margin-bottom: 10px;
      font-size: 0.92rem;
    }}
    .check {{ color: var(--ok); font-weight: bold; }}
    .btn-row {{ display: flex; gap: 12px; margin-top: 24px; }}
    button {{
      flex: 1; padding: 12px; border-radius: 10px; font-size: 1rem;
      font-weight: 600; cursor: pointer; border: none; transition: all 0.15s ease;
    }}
    .btn-approve {{ background: var(--accent); color: #fff; }}
    .btn-approve:hover {{ background: #2563eb; }}
    .btn-deny {{ background: transparent; color: var(--muted); border: 1px solid var(--border); }}
    .btn-deny:hover {{ background: rgba(255,255,255,0.05); color: var(--text); }}
    .pass-input {{
      width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--border);
      background: #0d1322; color: #fff; margin-top: 8px; font-size: 0.95rem;
    }}
  </style>
</head>
<body>
  <div class="auth-card">
    <div class="header">
      <div class="icon">🖨️</div>
      <h1 class="title">Authorize Access</h1>
      <p class="sub">for your thermal printer</p>
    </div>

    <div class="app-box">
      <div style="font-size:0.85rem; color:var(--muted)">Requesting Application</div>
      <div style="font-size:1.15rem; font-weight:700; color:#fff">{client_name}</div>
      <div style="font-size:0.8rem; color:var(--muted); word-break:break-all; margin-top:4px">{redirect_uri}</div>
    </div>

    <div class="scopes-list">
      <div style="font-size:0.85rem; color:var(--muted); margin-bottom:8px">The following permissions will be granted:</div>
      <div class="scope-item">
        <span class="check">&#10003;</span>
        <div><b>Print tickets & images</b> (text receipts, vouchers, batch print)</div>
      </div>
      <div class="scope-item">
        <span class="check">&#10003;</span>
        <div><b>Read printer status</b> (online status, job status, queue)</div>
      </div>
      <div class="scope-item" style="color:var(--muted)">
        <span style="color:#ef4444">&#10007;</span>
        <div><b>Admin restricted</b> (cannot clear queue, no guest management)</div>
      </div>
    </div>

    <form method="post" action="/oauth/authorize/consent">
      <input type="hidden" name="client_id" value="{client_id}">
      <input type="hidden" name="redirect_uri" value="{redirect_uri}">
      <input type="hidden" name="scope" value="{scope}">
      <input type="hidden" name="state" value="{state}">
      <input type="hidden" name="code_challenge" value="{code_challenge}">
      <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">

      <div style="margin-top:16px;">
        <label style="font-size:0.85rem; color:var(--muted)">Admin password or API key for confirmation:</label>
        <input type="password" name="admin_secret" class="pass-input" placeholder="Enter admin secret..." required autofocus>
      </div>

      <div class="btn-row">
        <button type="button" class="btn-deny" onclick="window.location.href='{redirect_uri}?error=access_denied&state={state}'">Deny</button>
        <button type="submit" class="btn-approve">Approve</button>
      </div>
    </form>
  </div>
</body>
</html>"""
    return HTMLResponse(html)

@oauth_router.post("/oauth/authorize/consent")
async def oauth_authorize_consent(
    request: Request,
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    scope: str = Form(DEFAULT_SCOPES),
    state: str = Form(""),
    code_challenge: str = Form(""),
    code_challenge_method: str = Form("S256"),
    admin_secret: str = Form(""),
):
    # Verify administrator secret (APP_API_KEY)
    valid_key = os.getenv("APP_API_KEY", "")
    if not (valid_key and hmac.compare_digest(admin_secret.strip(), valid_key)):
        # Check if user has active authenticated admin cookie or Cloudflare Access
        raise HTTPException(status_code=401, detail="Invalid admin password / API key.")

    # Generate Authorization Code
    code = f"authcode-{secrets.token_urlsafe(32)}"
    expires_at = time.time() + 300  # 5 minutes validity

    code_doc = {
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "user": "leandro",
        "created_at": time.time(),
        "expires_at": expires_at,
        "used": False,
    }

    codes = _codes_coll()
    if codes is not None:
        codes.insert_one(code_doc)

    log(f"[oauth] Issued authorization code for client {client_id}")

    # Redirect back to client with code and state
    sep = "&" if "?" in redirect_uri else "?"
    redirect_target = f"{redirect_uri}{sep}code={code}"
    if state:
        redirect_target += f"&state={state}"

    return RedirectResponse(redirect_target, status_code=303)

# ----------------- Token Endpoint (RFC 6749 & RFC 7636) -----------------

@oauth_router.post("/oauth/token")
async def oauth_token_endpoint(
    request: Request,
    grant_type: str = Form(...),
    code: Optional[str] = Form(None),
    redirect_uri: Optional[str] = Form(None),
    client_id: Optional[str] = Form(None),
    code_verifier: Optional[str] = Form(None),
    refresh_token: Optional[str] = Form(None),
):
    now = time.time()

    # 1. Authorization Code Grant
    if grant_type == "authorization_code":
        if not code:
            return JSONResponse({"error": "invalid_request", "error_description": "Missing code"}, status_code=400)

        codes = _codes_coll()
        code_doc = codes.find_one({"code": code}) if codes is not None else None
        if not code_doc:
            return JSONResponse({"error": "invalid_grant", "error_description": "Invalid or expired authorization code"}, status_code=400)

        if code_doc.get("used"):
            return JSONResponse({"error": "invalid_grant", "error_description": "Authorization code has already been used"}, status_code=400)

        if code_doc.get("expires_at", 0) < now:
            return JSONResponse({"error": "invalid_grant", "error_description": "Authorization code expired"}, status_code=400)

        # Verify Redirect URI if stored
        expected_uri = code_doc.get("redirect_uri")
        if expected_uri and redirect_uri and expected_uri.split("?")[0] != redirect_uri.split("?")[0]:
            return JSONResponse({"error": "invalid_grant", "error_description": "redirect_uri mismatch"}, status_code=400)

        # Verify PKCE
        stored_challenge = code_doc.get("code_challenge")
        stored_method = code_doc.get("code_challenge_method", "S256")
        if stored_challenge:
            if not code_verifier or not verify_pkce(code_verifier, stored_challenge, stored_method):
                log("[oauth] PKCE verification failed")
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)

        # Mark code as used
        if codes is not None:
            codes.update_one({"code": code}, {"$set": {"used": True}})

        # Issue Access Token and Refresh Token
        jti = secrets.token_hex(16)
        scope = code_doc.get("scope", DEFAULT_SCOPES)
        user = code_doc.get("user", "leandro")
        expires_in = 30 * 86400  # 30 days access token

        token_payload = {
            "iss": ISSUER,
            "sub": user,
            "aud": f"{ISSUER}/mcp",
            "jti": jti,
            "scope": scope,
            "iat": int(now),
            "exp": int(now + expires_in),
        }
        access_token = _sign_token(token_payload)
        new_refresh_token = f"rt-{secrets.token_urlsafe(32)}"

        tokens = _tokens_coll()
        if tokens is not None:
            tokens.insert_one({
                "jti": jti,
                "client_id": code_doc.get("client_id"),
                "user": user,
                "scope": scope,
                "access_token": access_token,
                "refresh_token": new_refresh_token,
                "created_at": now,
                "expires_at": now + expires_in,
                "revoked": False,
            })

        log(f"[oauth] Issued access token for user {user} (expires in {expires_in}s)")

        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "refresh_token": new_refresh_token,
            "scope": scope,
        }

    # 2. Refresh Token Grant
    elif grant_type == "refresh_token":
        if not refresh_token:
            return JSONResponse({"error": "invalid_request", "error_description": "Missing refresh_token"}, status_code=400)

        tokens = _tokens_coll()
        token_doc = tokens.find_one({"refresh_token": refresh_token, "revoked": False}) if tokens is not None else None
        if not token_doc:
            return JSONResponse({"error": "invalid_grant", "error_description": "Invalid or revoked refresh token"}, status_code=400)

        jti = secrets.token_hex(16)
        user = token_doc.get("user", "leandro")
        scope = token_doc.get("scope", DEFAULT_SCOPES)
        expires_in = 30 * 86400

        token_payload = {
            "iss": ISSUER,
            "sub": user,
            "aud": f"{ISSUER}/mcp",
            "jti": jti,
            "scope": scope,
            "iat": int(now),
            "exp": int(now + expires_in),
        }
        new_access_token = _sign_token(token_payload)
        new_refresh_token = f"rt-{secrets.token_urlsafe(32)}"

        if tokens is not None:
            # Revoke old token record and insert new
            tokens.update_one({"_id": token_doc["_id"]}, {"$set": {"revoked": True}})
            tokens.insert_one({
                "jti": jti,
                "client_id": token_doc.get("client_id"),
                "user": user,
                "scope": scope,
                "access_token": new_access_token,
                "refresh_token": new_refresh_token,
                "created_at": now,
                "expires_at": now + expires_in,
                "revoked": False,
            })

        return {
            "access_token": new_access_token,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "refresh_token": new_refresh_token,
            "scope": scope,
        }

    else:
        return JSONResponse({"error": "unsupported_grant_type", "error_description": f"grant_type '{grant_type}' not supported"}, status_code=400)

# ----------------- Revocation Endpoint (RFC 7009) -----------------

@oauth_router.post("/oauth/revoke")
async def oauth_revoke_endpoint(
    token: str = Form(...),
    token_type_hint: Optional[str] = Form(None),
):
    tokens = _tokens_coll()
    if tokens is not None:
        tokens.update_many(
            {"$or": [{"access_token": token}, {"refresh_token": token}]},
            {"$set": {"revoked": True, "revoked_at": time.time()}}
        )
    return Response(status_code=200)
