import sys
from playwright.sync_api import sync_playwright

API_BASE = "http://192.0.2.1:8080"
API_KEY = "synthetic-placeholder-not-a-secret"

def run_tests():
    print("=" * 60)
    print("STARTING PLAYWRIGHT HEADLESS CHROMIUM BROWSER VERIFICATION")
    print("=" * 60)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            extra_http_headers={"x-api-key": API_KEY},
            viewport={"width": 390, "height": 844},  # Mobile viewport (iPhone 14)
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        )
        page = context.new_page()

        # 1. Navigate to UI
        response = page.goto(f"{API_BASE}/ui")
        assert response.status == 200, f"Expected 200, got {response.status}"
        print("✓ PASS: Successfully loaded UI page (200 OK)")

        # 2. Check security headers
        headers = response.headers
        referrer_policy = headers.get("referrer-policy")
        assert referrer_policy == "strict-origin-when-cross-origin", f"Wrong referrer-policy: {referrer_policy}"
        print(f"✓ PASS: Referrer-Policy confirmed: {referrer_policy}")

        # 3. Verify CSRF meta tag and cookie
        csrf_meta = page.locator('meta[name="csrf-token"]').get_attribute("content")
        assert csrf_meta, "Missing meta csrf-token"
        
        cookies = context.cookies()
        csrf_cookie = next((c["value"] for c in cookies if c["name"] == "printer_csrf"), None)
        assert csrf_cookie, "Missing printer_csrf cookie"
        assert csrf_cookie == csrf_meta, f"CSRF mismatch: cookie={csrf_cookie} vs meta={csrf_meta}"
        print(f"✓ PASS: Cryptographic CSRF cookie and meta tag match: {csrf_cookie[:16]}...")

        # 4. Verify form action contains CSRF token
        form_action = page.locator('#pane_tpl form').get_attribute("action")
        assert f"csrf={csrf_cookie}" in form_action, f"Form action missing csrf parameter: {form_action}"
        print(f"✓ PASS: Form action correctly signed: {form_action}")

        # 5. Fill out template form and submit via real browser click
        page.fill('input#title', 'PLAYWRIGHT VERIFIED')
        page.fill('textarea#lines', 'Automated Chromium Test\nMobile iPhone Viewport\nNo Cross-Site Rejection')
        
        # Monitor submission response
        with page.expect_response(lambda r: "/ui/print/template" in r.url and r.request.method == "POST") as resp_info:
            page.click('#pane_tpl button[type="submit"]')
        
        submit_res = resp_info.value
        assert submit_res.status in (200, 303), f"Submission failed with status {submit_res.status}: {submit_res.text()}"
        print(f"✓ PASS: Real browser form submission succeeded with status {submit_res.status} (No cross-site error!)")

        # 6. Verify page redirected cleanly to /ui#tpl
        page.wait_for_load_state("networkidle")
        assert "/ui" in page.url, f"Unexpected final page URL: {page.url}"
        print(f"✓ PASS: Final page URL is {page.url}")

        # 7. HAR Simulation: POST with Origin: null, Sec-Fetch-Site: same-origin
        har_res = context.request.post(
            f"{API_BASE}/ui/print/template?csrf={csrf_cookie}",
            headers={
                "Origin": "null",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
                "Content-Type": "application/x-www-form-urlencoded"
            },
            data=f"title=HAR_SIM&lines=OriginNullSuccess&csrf_token={csrf_cookie}"
        )
        assert har_res.status in (200, 303), f"HAR simulation failed: {har_res.status} {har_res.text()}"
        print(f"✓ PASS: HAR simulation (Origin: null, Sec-Fetch-Site: same-origin) succeeded with status {har_res.status}")

        # 8. Malicious Attack Simulation: Attacker page submits POST from cross-site
        attacker_context = browser.new_context()
        attacker_context.add_cookies([
            {"name": "printer_csrf", "value": csrf_cookie, "domain": "192.0.2.1", "path": "/"}
        ])
        attack_res = attacker_context.request.post(
            f"{API_BASE}/ui/print/template?csrf={csrf_cookie}",
            headers={
                "Origin": "https://evil-attacker.example.com",
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-Mode": "cors",
                "Content-Type": "application/x-www-form-urlencoded"
            },
            data=f"title=ATTACK&lines=ShouldBeBlocked&csrf_token={csrf_cookie}"
        )
        assert attack_res.status == 403, f"Expected 403 for cross-site attack, got {attack_res.status}"
        assert "cross-site request rejected" in attack_res.text(), f"Unexpected error message: {attack_res.text()}"
        print("✓ PASS: Malicious cross-site attack was rejected with 403 Forbidden ('cross-site request rejected')")

        # 9. Tampered CSRF Attack Simulation
        tampered_res = attacker_context.request.post(
            f"{API_BASE}/ui/print/template?csrf=tampered_token_payload",
            headers={
                "Origin": "null",
                "Sec-Fetch-Site": "same-origin",
                "Content-Type": "application/x-www-form-urlencoded"
            },
            data="title=TAMPERED&lines=ShouldBeBlocked&csrf_token=tampered_token_payload"
        )
        assert tampered_res.status in (401, 403), f"Expected 401/403 for tampered CSRF, got {tampered_res.status}"
        print(f"✓ PASS: Tampered CSRF attack was rejected with {tampered_res.status}")

        attacker_context.close()
        browser.close()

    print("=" * 60)
    print("ALL PLAYWRIGHT HEADLESS BROWSER TESTS PASSED PERFECTLY!")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
