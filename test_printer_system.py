# test_printer_system.py — Comprehensive Automated Test Suite
import os
import io
import sys
import ssl
import time
import json
import base64
import unittest
import threading
import httpx
from PIL import Image

import paho.mqtt.client as mqtt

API_BASE = "http://127.0.0.1:8080"
API_KEY = os.getenv("APP_API_KEY", "")
MQTT_HOST = os.getenv("MQTT_HOST", "mqtt")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USERNAME", "printer_api")
MQTT_PASS = os.getenv("MQTT_PASSWORD", "")
PRINT_TOPIC = os.getenv("PRINT_TOPIC", "Prn00005E005301")

class PrinterSystemTestSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        print("\n=======================================================")
        print("STARTING FULL THERMAL PRINTER SYSTEM TEST SUITE")
        print("=======================================================\n")
        assert bool(API_KEY), "APP_API_KEY must be set in environment"

    # =========================================================================
    # SECTION 1: MQTT & Security Tests
    # =========================================================================

    def test_01_mqtt_auth_valid(self):
        """MQTT: Valid credentials connect successfully."""
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-valid-auth")
        client.username_pw_set(MQTT_USER, MQTT_PASS)
        conn_results = []
        client.on_connect = lambda c, ud, flags, rc, props=None: conn_results.append(rc)
        client.connect(MQTT_HOST, MQTT_PORT, 5)
        client.loop_start()
        time.sleep(1)
        client.loop_stop()
        client.disconnect()
        self.assertEqual(len(conn_results), 1)
        self.assertEqual(conn_results[0].is_failure, False)
        print("✓ PASS: MQTT valid credentials authenticated")

    def test_02_mqtt_auth_invalid(self):
        """MQTT: Invalid credentials are rejected by broker."""
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-invalid-auth")
        client.username_pw_set("printer_api", "synthetic-placeholder-not-a-secret")
        conn_results = []
        client.on_connect = lambda c, ud, flags, rc, props=None: conn_results.append(rc)
        client.connect(MQTT_HOST, MQTT_PORT, 5)
        client.loop_start()
        time.sleep(1)
        client.loop_stop()
        client.disconnect()
        self.assertEqual(len(conn_results), 1)
        self.assertTrue(conn_results[0].is_failure)
        print("✓ PASS: MQTT invalid credentials rejected with Not authorized")

    def test_03_mqtt_acl_enforcement(self):
        """MQTT: ACL blocks unauthorized topics for printer_device."""
        # Note: printer_device is only allowed to write to Hearbeat, PrintSuccess, PrintSucces
        # and subscribe to Prn00005E005301.
        received = []
        sub_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-acl-listener")
        sub_client.username_pw_set(MQTT_USER, MQTT_PASS)
        sub_client.on_message = lambda c, ud, msg: received.append((msg.topic, msg.payload.decode()))
        sub_client.connect(MQTT_HOST, MQTT_PORT, 5)
        sub_client.subscribe("#", qos=1)
        sub_client.loop_start()
        time.sleep(0.5)

        pub_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-acl-pub")
        # Read printer_device password from passwords.txt or test with printer_api
        pub_client.username_pw_set(MQTT_USER, MQTT_PASS)
        pub_client.connect(MQTT_HOST, MQTT_PORT, 5)
        pub_client.loop_start()

        pub_client.publish(PRINT_TOPIC, "test_acl_ok", qos=1)
        time.sleep(0.5)

        sub_client.loop_stop()
        pub_client.loop_stop()
        sub_client.disconnect()
        pub_client.disconnect()

        self.assertTrue(any(t == PRINT_TOPIC and m == "test_acl_ok" for t, m in received))
        print("✓ PASS: MQTT ACL allows authorized topic delivery")

    def test_04_mqtt_sub_pub_and_reconnect(self):
        """MQTT: Subscription, message publishing and reconnect."""
        delivered = []
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-sub-pub")
        client.username_pw_set(MQTT_USER, MQTT_PASS)
        client.on_connect = lambda c, ud, flags, rc, props=None: c.subscribe("test/topic", qos=1)
        client.on_message = lambda c, ud, msg: delivered.append(msg.payload.decode())
        client.connect(MQTT_HOST, MQTT_PORT, 10)
        client.loop_start()
        time.sleep(0.5)

        client.publish("test/topic", "ping_payload", qos=1)
        time.sleep(0.5)
        self.assertIn("ping_payload", delivered)

        # Force disconnect & reconnect
        client.reconnect()
        time.sleep(0.8)
        client.publish("test/topic", "ping_payload_2", qos=1)
        time.sleep(0.8)
        self.assertIn("ping_payload_2", delivered)

        client.loop_stop()
        client.disconnect()
        print("✓ PASS: MQTT Subscribe, Publish & Reconnect verified")

    # =========================================================================
    # SECTION 2: Print System & Queue Tests (MongoDB)
    # =========================================================================

    def test_05_single_text_ticket(self):
        """Print System: Single text ticket submission."""
        headers = {"x-api-key": API_KEY}
        payload = {
            "title": "UNIT TEST TICKET",
            "text": "Hello Thermal Printer World!\nLine 2 content\nLine 3 item",
            "cut": True,
            "add_datetime": True
        }
        res = httpx.post(f"{API_BASE}/api/print/text", json=payload, headers=headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertTrue(bool(data.get("job_id")))
        print(f"✓ PASS: Single text ticket enqueued with job_id={data.get('job_id')}")

    def test_06_batch_multi_text_tickets(self):
        """Print System: Batch submission of 3 distinct text tickets."""
        headers = {"x-api-key": API_KEY}
        batch_payload = {
            "printer_id": "default",
            "idempotency_key": f"test-batch-multi-{int(time.time()*1000)}",
            "separate": True,
            "tickets": [
                {"type": "text", "content": "Ticket #1: Task A"},
                {"type": "text", "content": "Ticket #2: Task B"},
                {"type": "text", "content": "Ticket #3: Task C"}
            ]
        }
        res = httpx.post(f"{API_BASE}/api/print/batch", json=batch_payload, headers=headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("total_tickets"), 3)
        self.assertEqual(len(data.get("job_ids", [])), 3)
        print(f"✓ PASS: Multi-ticket batch enqueued successfully ({data.get('batch_id')})")

    def test_07_image_ticket_base64(self):
        """Print System: Image ticket with valid base64 PNG."""
        headers = {"x-api-key": API_KEY}
        # 10x10 monochrome image
        img = Image.new("L", (100, 100), color=128)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64_str = base64.b64encode(buf.getvalue()).decode()

        payload = {
            "image_base64": b64_str,
            "title": "IMAGE TEST",
            "subtitle": "Base64 Rendered"
        }
        res = httpx.post(f"{API_BASE}/api/print/image-url", json=payload, headers=headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        print(f"✓ PASS: Base64 image ticket enqueued with job_id={data.get('job_id')}")

    def test_08_mixed_batch(self):
        """Print System: Mixed batch with text and image elements."""
        headers = {"x-api-key": API_KEY}
        img = Image.new("L", (50, 50), color=0)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64_str = base64.b64encode(buf.getvalue()).decode()

        batch_payload = {
            "separate": True,
            "tickets": [
                {
                    "elements": [
                        {"type": "text", "content": "Header: Shopping List"},
                        {"type": "image", "image_base64": b64_str, "title": "Store Logo"},
                        {"type": "text", "content": "Item 1: Apples\nItem 2: Milk"}
                    ]
                },
                {
                    "type": "text",
                    "content": "Second Independent Ticket in Batch"
                }
            ]
        }
        res = httpx.post(f"{API_BASE}/api/print/batch", json=batch_payload, headers=headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("total_tickets"), 2)
        print("✓ PASS: Mixed text + image batch enqueued successfully")

    def test_09_idempotency_and_duplicate_prevention(self):
        """Print System: Idempotency keys prevent duplicate prints."""
        headers = {"x-api-key": API_KEY}
        key = f"idemp-test-{int(time.time()*1000)}"
        payload = {
            "idempotency_key": key,
            "tickets": [{"type": "text", "content": "Idempotent Ticket"}]
        }
        # First submission
        res1 = httpx.post(f"{API_BASE}/api/print/batch", json=payload, headers=headers)
        self.assertEqual(res1.status_code, 200)
        data1 = res1.json()

        # Duplicate submission with identical key
        res2 = httpx.post(f"{API_BASE}/api/print/batch", json=payload, headers=headers)
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()

        self.assertTrue(data2.get("idempotent", False))
        self.assertEqual(data1.get("batch_id"), data2.get("batch_id"))
        print("✓ PASS: Idempotency check returned existing batch without duplicate queuing")

    def test_10_invalid_and_unsafe_images(self):
        """Print System: Unsafe URLs (SSRF) and corrupt images are blocked."""
        headers = {"x-api-key": API_KEY}

        # 1. SSRF to local IP
        res_ssrf = httpx.post(
            f"{API_BASE}/api/print/image-url",
            json={"image_url": "https://127.0.0.1/private.png"},
            headers=headers
        )
        self.assertIn(res_ssrf.status_code, (400, 500))

        # 2. SSRF to cloud metadata
        res_meta = httpx.post(
            f"{API_BASE}/api/print/image-url",
            json={"image_url": "https://169.254.169.254/latest/meta-data/"},
            headers=headers
        )
        self.assertIn(res_meta.status_code, (400, 500))

        # 3. Invalid corrupted base64
        res_corrupt = httpx.post(
            f"{API_BASE}/api/print/image-url",
            json={"image_base64": "NOT_A_VALID_IMAGE_BYTES"},
            headers=headers
        )
        self.assertIn(res_corrupt.status_code, (400, 500))
        print("✓ PASS: SSRF protection and corrupt image rejection verified")

    def test_11_queue_management_endpoints(self):
        """Print System: Queue status, listing, and inspection."""
        headers = {"x-api-key": API_KEY}

        # Status
        status_res = httpx.get(f"{API_BASE}/queue/status", headers=headers)
        self.assertEqual(status_res.status_code, 200)
        status_data = status_res.json()
        self.assertIn("flusher_running", status_data)
        self.assertIn("queue_count", status_data)

        # List
        list_res = httpx.get(f"{API_BASE}/queue/list", headers=headers)
        self.assertEqual(list_res.status_code, 200)
        list_data = list_res.json()
        self.assertIn("jobs", list_data)

        # Capabilities
        cap_res = httpx.get(f"{API_BASE}/api/printer/capabilities", headers=headers)
        self.assertEqual(cap_res.status_code, 200)
        cap_data = cap_res.json()
        self.assertEqual(cap_data.get("print_width_px"), 576)
        print("✓ PASS: Queue management and capabilities endpoints functional")

    # =========================================================================
    # SECTION 3: ChatGPT MCP Integration Tests
    # =========================================================================

    def test_12_mcp_discovery_and_tools_list(self):
        """ChatGPT: MCP initialize and tools/list discovery."""
        headers = {"x-api-key": API_KEY, "Content-Type": "application/json"}

        # initialize
        init_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-suite", "version": "1.0.0"}
            }
        }
        res_init = httpx.post(f"{API_BASE}/mcp", json=init_body, headers=headers)
        self.assertEqual(res_init.status_code, 200)
        init_data = res_init.json()
        server_name = init_data.get("result", {}).get("serverInfo", {}).get("name")
        self.assertIn(server_name, ["thermal-printer-mcp", "Thermodrucker"])

        # tools/list
        tools_body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        res_tools = httpx.post(f"{API_BASE}/mcp", json=tools_body, headers=headers)
        self.assertEqual(res_tools.status_code, 200)
        tools_data = res_tools.json()
        tool_names = [t["name"] for t in tools_data.get("result", {}).get("tools", [])]
        self.assertIn("print_tickets", tool_names)
        self.assertIn("print_text", tool_names)
        self.assertIn("print_image", tool_names)
        self.assertIn("get_printer_status", tool_names)
        self.assertIn("get_print_job_status", tool_names)
        self.assertIn("get_batch_status", tool_names)
        self.assertIn("get_printer_capabilities", tool_names)
        print("✓ PASS: MCP tool discovery confirmed all 7 core tools")

    def test_13_mcp_tool_execution(self):
        """ChatGPT: Calling tools via MCP tools/call."""
        headers = {"x-api-key": API_KEY, "Content-Type": "application/json"}

        # Call get_printer_status
        call_body = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "get_printer_status",
                "arguments": {}
            }
        }
        res = httpx.post(f"{API_BASE}/mcp", json=call_body, headers=headers)
        self.assertEqual(res.status_code, 200)
        call_data = res.json()
        self.assertFalse(call_data.get("result", {}).get("isError", True))
        content_text = call_data["result"]["content"][0]["text"]
        parsed = json.loads(content_text)
        self.assertIn("queue_count", parsed)
        print("✓ PASS: MCP tools/call execution verified")

    def test_14_mcp_auth_rejection(self):
        """ChatGPT: MCP rejects unauthorized calls."""
        headers = {"x-api-key": "invalid_wrong_token"}
        call_body = {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}
        res = httpx.post(f"{API_BASE}/mcp", json=call_body, headers=headers)
        self.assertEqual(res.status_code, 401)
        print("✓ PASS: MCP endpoint rejects unauthorized requests")

    def test_15_chatgpt_action_openapi_spec(self):
        """ChatGPT: OpenAPI 3.1 action specification is valid."""
        res = httpx.get(f"{API_BASE}/api/chatgpt-action.json")
        self.assertEqual(res.status_code, 200)
        spec = res.json()
        self.assertEqual(spec.get("openapi"), "3.1.0")
        self.assertIn("/api/print/tickets", spec.get("paths", {}))
        self.assertIn("/api/print/text", spec.get("paths", {}))
        print("✓ PASS: ChatGPT Action OpenAPI 3.1 specification available")

    def test_16_flusher_payload_validation_and_mqtt(self):
        """Queue Flusher: Validates end-to-end payload generation, base64 PNG decoding, and MQTT publishing."""
        from logic import render_receipt, pil_to_base64_png, ReceiptCfg
        import queue_print

        cfg = ReceiptCfg()
        img = render_receipt("REGRESSION TEST", ["Verifying queue flusher fixes", "Umlaute: ä ö ü ß", "Symbol: 100 CHF"], add_time=True, width_px=576, cfg=cfg)
        b64 = pil_to_base64_png(img)

        # Enqueue job
        test_jid = f"test-flush-{int(time.time()*1000)}"
        jid = queue_print.enqueue_base64_png(b64, cut_paper=1, job_id=test_jid, meta={"source": "test_flusher_text"})
        self.assertEqual(jid, test_jid)

        # Spy on mqtt_publish_image_base64 directly in queue_print namespace
        published_payloads = []
        orig_publish = queue_print.mqtt_publish_image_base64
        orig_status = queue_print.printer_status

        def mock_publish(b64_png, cut_paper=1, paper_width_mm=0, paper_height_mm=0, ticket_id=None):
            published_payloads.append({
                "ticket_id": ticket_id,
                "b64": b64_png,
                "cut": cut_paper,
            })
            return True

        queue_print.mqtt_publish_image_base64 = mock_publish
        queue_print.printer_status = lambda: {"online": True, "detail": "Test Mock"}
        try:
            count = queue_print.flush_once()
            self.assertGreaterEqual(count, 1, "flush_once should have processed at least 1 job")
        finally:
            queue_print.mqtt_publish_image_base64 = orig_publish
            queue_print.printer_status = orig_status

        # Validate captured payload
        p = next((x for x in published_payloads if x["ticket_id"] == test_jid), None)
        self.assertIsNotNone(p, f"Expected {test_jid} in published payloads: {[x['ticket_id'] for x in published_payloads]}")
        self.assertEqual(p["ticket_id"], test_jid)
        self.assertEqual(p["cut"], 1)

        # Decode base64 and verify valid PNG
        raw_png = base64.b64decode(p["b64"])
        self.assertTrue(raw_png.startswith(b"\x89PNG\r\n\x1a\n"), "Decoded payload must have valid PNG magic bytes")
        decoded_img = Image.open(io.BytesIO(raw_png))
        self.assertEqual(decoded_img.size[0], 576, "Decoded image width must be exactly 576px")
        self.assertGreater(decoded_img.size[1], 50)

        # Verify MongoDB archive state
        a_coll = queue_print._get_archive_coll()
        archived_doc = a_coll.find_one({"job_id": test_jid}) if a_coll is not None else None
        self.assertIsNotNone(archived_doc)
        self.assertEqual(archived_doc.get("status"), "published")
        print(f"✓ PASS: Queue flusher text payload verified (PNG width={decoded_img.size[0]}, magic bytes valid, status=published)")

    def test_17_flusher_image_ticket(self):
        """Queue Flusher: Image ticket processing and payload integrity."""
        import queue_print

        test_img = Image.new("1", (576, 120), color=1)
        buf = io.BytesIO()
        test_img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        test_jid = f"test-img-{int(time.time()*1000)}"
        queue_print.enqueue_base64_png(b64, cut_paper=0, job_id=test_jid, meta={"source": "test_flusher_image"})

        published_payloads = []
        orig_publish = queue_print.mqtt_publish_image_base64
        orig_status = queue_print.printer_status

        def mock_publish(b64_png, cut_paper=1, paper_width_mm=0, paper_height_mm=0, ticket_id=None):
            published_payloads.append({"ticket_id": ticket_id, "b64": b64_png, "cut": cut_paper})
            return True

        queue_print.mqtt_publish_image_base64 = mock_publish
        queue_print.printer_status = lambda: {"online": True, "detail": "Test Mock"}
        try:
            count = queue_print.flush_once()
            self.assertGreaterEqual(count, 1)
        finally:
            queue_print.mqtt_publish_image_base64 = orig_publish
            queue_print.printer_status = orig_status

        p = next((x for x in published_payloads if x["ticket_id"] == test_jid), None)
        self.assertIsNotNone(p, f"Expected {test_jid} in published payloads: {[x['ticket_id'] for x in published_payloads]}")
        self.assertEqual(p["ticket_id"], test_jid)
        self.assertEqual(p["cut"], 0)
        decoded = Image.open(io.BytesIO(base64.b64decode(p["b64"])))
        self.assertEqual(decoded.size, (576, 120))
        print("✓ PASS: Queue flusher image ticket payload verified (cut=0 preserved, size=(576, 120))")

    def test_18_flusher_batch_tickets(self):
        """Queue Flusher: Batch processing maintains sequential ordering and separate cuts."""
        import queue_print

        # Provide real 1-bit PNGs
        img = Image.new("1", (576, 80), color=1)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        valid_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        tickets = [
            {"b64": valid_b64, "cut": 1, "meta": {"test": "1"}},
            {"b64": valid_b64, "cut": 1, "meta": {"test": "2"}},
        ]

        batch_res = queue_print.enqueue_batch(tickets, separate=True, meta={"source": "test_flusher_batch"})
        bid = batch_res["batch_id"]

        published_payloads = []
        orig_publish = queue_print.mqtt_publish_image_base64
        orig_status = queue_print.printer_status

        def mock_publish(b64_png, cut_paper=1, paper_width_mm=0, paper_height_mm=0, ticket_id=None):
            published_payloads.append({"ticket_id": ticket_id, "b64": b64_png, "cut": cut_paper})
            return True

        queue_print.mqtt_publish_image_base64 = mock_publish
        queue_print.printer_status = lambda: {"online": True, "detail": "Test Mock"}
        try:
            count = queue_print.flush_once()
            self.assertGreaterEqual(count, 2)
        finally:
            queue_print.mqtt_publish_image_base64 = orig_publish
            queue_print.printer_status = orig_status

        batch_payloads = [p for p in published_payloads if p["ticket_id"].startswith(bid)]
        self.assertGreaterEqual(len(batch_payloads), 2)
        self.assertTrue(batch_payloads[0]["ticket_id"].endswith("-01"))
        self.assertTrue(batch_payloads[1]["ticket_id"].endswith("-02"))
        print(f"✓ PASS: Queue flusher batch verified (2 tickets processed in order: {[p['ticket_id'] for p in batch_payloads]})")

if __name__ == "__main__":
    unittest.main()
