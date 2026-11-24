#!/usr/bin/env python3
"""
Comprehensive End-to-End Verification of:
1. OAuth 2.1 RFC-compliant endpoints (.well-known, DCR register, authorize consent, PKCE exchange, refresh, revoke)
2. Scoped MCP access via OAuth Bearer token (printer:read, printer:print)
3. Rejection of unauthorized/revoked OAuth tokens
4. Guest Token lifecycle (enable, disable, re-enable, delete, canonical URLs)
5. Live Status & SSE stream availability
6. Rate Limiting categorization & headers
"""

import sys
import os
import json
import base64
import hashlib
import secrets
import urllib.request
import urllib.parse
import urllib.error

BASE_URL = "http://127.0.0.1:8080"
ADMIN_KEY = os.environ.get("APP_API_KEY", "synthetic-placeholder-not-a-secret")

class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

opener = urllib.request.build_opener(NoRedirectHandler)

def req(url, method="GET", headers=None, data=None, is_form=False):
    if headers is None:
        headers = {}
    
    if data is not None:
        if is_form:
            data = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif isinstance(data, (dict, list)):
            data = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif isinstance(data, str):
            data = data.encode("utf-8")
    
    req_obj = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(req_obj, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, resp.headers, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        return e.code, e.headers, body

def run_tests():
    print("=" * 60)
    print("STARTING OAUTH 2.1, GUEST LINKS & LIVE STATUS TEST SUITE")
    print("=" * 60)

    # 1. Metadata endpoints
    status, _, body = req(f"{BASE_URL}/.well-known/oauth-authorization-server")
    assert status == 200, f"oauth-authorization-server failed: {status}"
    auth_meta = json.loads(body)
    assert auth_meta["issuer"] == "https://printer.nox007.tech"
    assert "code_challenge_methods_supported" in auth_meta
    assert "S256" in auth_meta["code_challenge_methods_supported"]
    print("✓ PASS: /.well-known/oauth-authorization-server returns valid RFC 8414 metadata")

    status, _, body = req(f"{BASE_URL}/.well-known/oauth-protected-resource")
    assert status == 200, f"oauth-protected-resource failed: {status}"
    res_meta = json.loads(body)
    assert res_meta["resource"] == "https://printer.nox007.tech/mcp"
    print("✓ PASS: /.well-known/oauth-protected-resource returns RFC 9207 metadata")

    status, _, body = req(f"{BASE_URL}/.well-known/openid-configuration")
    assert status == 200, f"openid-configuration failed: {status}"
    print("✓ PASS: /.well-known/openid-configuration returns valid metadata")

    # 2. Dynamic Client Registration (RFC 7591)
    reg_payload = {
        "client_name": "ChatGPT-Verification-Client",
        "redirect_uris": ["https://chatgpt.com/aip/g-test/oauth/callback"],
        "token_endpoint_auth_method": "none"
    }
    status, _, body = req(f"{BASE_URL}/oauth/register", method="POST", data=reg_payload)
    assert status in (200, 201), f"oauth/register failed: {status} {body}"
    client_data = json.loads(body)
    client_id = client_data["client_id"]
    client_secret = client_data.get("client_secret", "")
    print(f"✓ PASS: Dynamic Client Registration registered client_id={client_id[:12]}...")

    # 3. PKCE S256 Generation & Authorization Code Flow
    code_verifier = secrets.token_urlsafe(40)
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")

    # Authorize GET renders consent screen
    auth_url = f"{BASE_URL}/oauth/authorize?response_type=code&client_id={client_id}&redirect_uri=https://chatgpt.com/aip/g-test/oauth/callback&scope=printer:read%20printer:print&state=xyz123&code_challenge={code_challenge}&code_challenge_method=S256"
    status, _, body = req(auth_url)
    assert status == 200, f"oauth/authorize failed: {status}"
    assert "ChatGPT" in body
    print("✓ PASS: GET /oauth/authorize renders interactive consent screen")

    # Consent POST approves authorization with admin_secret
    consent_data = {
        "client_id": client_id,
        "redirect_uri": "https://chatgpt.com/aip/g-test/oauth/callback",
        "state": "xyz123",
        "scope": "printer:read printer:print",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "admin_secret": ADMIN_KEY,
        "action": "allow"
    }
    status, headers, body = req(f"{BASE_URL}/oauth/authorize/consent", method="POST", data=consent_data, is_form=True)
    assert status in (302, 303), f"Expected 303 redirect, got: {status} {body}"
    redirect_target = headers.get("Location")
    assert redirect_target, "Missing Location header in consent response"
    
    parsed = urllib.parse.urlparse(redirect_target)
    params = urllib.parse.parse_qs(parsed.query)
    auth_code = params["code"][0]
    assert params["state"][0] == "xyz123"
    print(f"✓ PASS: User consent approved code={auth_code[:12]}...")

    # 4. Token Exchange (POST /oauth/token with PKCE verifier)
    token_data = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": "https://chatgpt.com/aip/g-test/oauth/callback",
        "client_id": client_id,
        "code_verifier": code_verifier
    }
    status, _, body = req(f"{BASE_URL}/oauth/token", method="POST", data=token_data, is_form=True)
    assert status == 200, f"oauth/token failed: {status} {body}"
    token_resp = json.loads(body)
    access_token = token_resp["access_token"]
    refresh_token = token_resp.get("refresh_token")
    assert token_resp["token_type"].lower() == "bearer"
    assert token_resp["scope"] == "printer:read printer:print"
    print(f"✓ PASS: Token exchange issued Bearer access_token & refresh_token")

    # 5. MCP invocation with OAuth Bearer token
    mcp_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {}
    }
    status, _, body = req(
        f"{BASE_URL}/mcp",
        method="POST",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        data=mcp_payload
    )
    assert status == 200, f"mcp tools/list failed with OAuth Bearer token: {status} {body}"
    mcp_resp = json.loads(body)
    assert "result" in mcp_resp
    assert "tools" in mcp_resp["result"]
    tools = [t["name"] for t in mcp_resp["result"]["tools"]]
    assert "get_printer_status" in tools
    print(f"✓ PASS: MCP tools/list succeeded with OAuth Bearer token ({len(tools)} tools available)")

    # Execute a tool via MCP with Bearer token
    mcp_call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "get_printer_status",
            "arguments": {}
        }
    }
    status, _, body = req(
        f"{BASE_URL}/mcp",
        method="POST",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        data=mcp_call
    )
    assert status == 200, f"mcp tools/call failed: {status} {body}"
    mcp_call_resp = json.loads(body)
    assert "result" in mcp_call_resp
    print("✓ PASS: MCP tools/call 'get_printer_status' executed successfully with OAuth token")

    # 6. Scoped Security: Block destructive endpoints with OAuth Bearer token
    status, _, body = req(
        f"{BASE_URL}/queue/clear",
        method="POST",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    assert status in (401, 403), f"Destructive /queue/clear MUST return 401/403 with OAuth token, got: {status}"
    print(f"✓ PASS: Destructive endpoint (/queue/clear) strictly rejected ({status} Unauthorized/Forbidden) for OAuth tokens")

    # 7. Token Refresh Flow
    refresh_data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id
    }
    status, _, body = req(f"{BASE_URL}/oauth/token", method="POST", data=refresh_data, is_form=True)
    assert status == 200, f"token refresh failed: {status} {body}"
    new_token_resp = json.loads(body)
    new_access_token = new_token_resp["access_token"]
    assert new_access_token != access_token
    print("✓ PASS: Token refresh successfully issued new access_token")

    # 8. Token Revocation Flow (RFC 7009)
    revoke_data = {
        "token": new_access_token,
        "token_type_hint": "access_token"
    }
    status, _, _ = req(f"{BASE_URL}/oauth/revoke", method="POST", data=revoke_data, is_form=True)
    assert status == 200, f"oauth/revoke failed: {status}"
    print("✓ PASS: Token revocation endpoint /oauth/revoke returned 200 OK")

    # Verify revoked token is now rejected
    status, _, _ = req(
        f"{BASE_URL}/mcp",
        method="POST",
        headers={"Authorization": f"Bearer {new_access_token}", "Accept": "application/json"},
        data=mcp_payload
    )
    assert status == 401, f"Revoked token MUST be rejected with 401, got {status}"
    print("✓ PASS: Revoked token is immediately rejected with 401 Unauthorized")

    # 9. Guest Links Lifecycle Verification
    # Create guest token via UI endpoint
    create_guest_payload = {
        "name": "Integration-Test-Guest",
        "quota": "3"
    }
    status, _, body = req(
        f"{BASE_URL}/ui/guests/create",
        method="POST",
        headers={"x-api-key": ADMIN_KEY},
        data=create_guest_payload,
        is_form=True
    )
    assert status == 200, f"guest create failed: {status} {body}"
    # Parse generated guest token from response HTML link
    import re
    match = re.search(r"https://printer\.nox007\.tech/guest/([a-zA-Z0-9_\-]+)", body)
    assert match, f"Could not find canonical guest link in HTML: {body[:300]}"
    guest_token = match.group(1)
    canonical_url = match.group(0)
    print(f"✓ PASS: Guest token created with canonical URL: {canonical_url}")

    # Guest can access landing page
    status, _, body = req(f"{BASE_URL}/guest/{guest_token}")
    assert status == 200, f"guest landing page failed: {status}"
    print("✓ PASS: Active guest can access guest portal")

    # Toggle disable guest
    status, _, body = req(
        f"{BASE_URL}/ui/guests/toggle",
        method="POST",
        headers={"x-api-key": ADMIN_KEY},
        data={"token": guest_token, "enabled": "0"},
        is_form=True
    )
    assert status == 200, f"toggle disable failed: {status} {body}"
    assert "disabled" in body.lower() or "deaktiviert" in body.lower()
    print("✓ PASS: Guest link successfully disabled via /ui/guests/toggle")

    # Disabled guest trying to access portal should show disabled status or 403
    status, _, body = req(f"{BASE_URL}/guest/{guest_token}")
    assert status == 403 or "deaktiviert" in body.lower() or "disabled" in body.lower(), f"Disabled guest should be blocked, got {status}"
    print("✓ PASS: Disabled guest link correctly blocked")

    # Toggle re-enable guest
    status, _, body = req(
        f"{BASE_URL}/ui/guests/toggle",
        method="POST",
        headers={"x-api-key": ADMIN_KEY},
        data={"token": guest_token, "enabled": "1"},
        is_form=True
    )
    assert status == 200, f"toggle re-enable failed: {status} {body}"
    assert "enabled" in body.lower() or "aktiviert" in body.lower()
    print("✓ PASS: Guest link successfully re-enabled")

    # Permanent delete guest
    status, _, body = req(
        f"{BASE_URL}/ui/guests/delete",
        method="POST",
        headers={"x-api-key": ADMIN_KEY},
        data={"token": guest_token},
        is_form=True
    )
    assert status == 200, f"guest delete failed: {status} {body}"
    assert "dauerhaft gelöscht" in body.lower() or "gelöscht" in body.lower()
    print("✓ PASS: Guest link permanently deleted via /ui/guests/delete")

    # Accessing deleted guest must now fail (404 or 403)
    status, _, _ = req(f"{BASE_URL}/guest/{guest_token}")
    assert status in (403, 404), f"Deleted guest must be rejected, got {status}"
    print("✓ PASS: Deleted guest link is permanently invalid")

    # 10. Live Status & SSE Availability
    status, _, body = req(f"{BASE_URL}/live-status")
    assert status == 200, f"/live-status failed: {status}"
    live_data = json.loads(body)
    assert "state" in live_data
    assert "busy" in live_data
    assert "printer_online" in live_data
    print(f"✓ PASS: /live-status returned valid state (state={live_data['state']}, busy={live_data['busy']})")

    # Check SSE endpoint connection
    req_sse = urllib.request.Request(f"{BASE_URL}/events", headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(req_sse, timeout=5) as sse_resp:
        assert sse_resp.status == 200
        content_type = sse_resp.headers.get("Content-Type", "")
        assert "text/event-stream" in content_type
        first_line = sse_resp.readline().decode("utf-8")
        assert "data:" in first_line or "event:" in first_line or first_line.startswith(":")
    print("✓ PASS: /events correctly streams text/event-stream SSE events")

    # 11. Rate Limiting Headers
    status, headers, _ = req(f"{BASE_URL}/ui/status")
    assert status == 200
    assert "X-RateLimit-Limit" in headers
    assert "X-RateLimit-Remaining" in headers
    assert headers["X-RateLimit-Category"] == "status_read"
    print(f"✓ PASS: Rate limiting headers confirmed (Limit: {headers['X-RateLimit-Limit']}, Category: status_read)")

    print("=" * 60)
    print("ALL OAUTH 2.1, GUEST LINKS & LIVE STATUS TESTS PASSED PERFECTLY!")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
