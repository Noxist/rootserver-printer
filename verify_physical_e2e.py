#!/usr/bin/env python3
"""
Controlled End-to-End Physical Verification (Max 4 tickets)
Tests MCP endpoints against live printer and monitors MQTT PrintSuccess ACKs.
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error

# Load environment
env_path = "/home/leandro/services/.env"
env_vars = {}
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip().strip("'\"")

api_key = env_vars.get("CHATGPT_API_KEY") or env_vars.get("APP_API_KEY")
mcp_url = "http://192.0.2.1:8080/mcp"

def call_mcp(tool_name: str, args: dict) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000),
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": args
        }
    }
    req = urllib.request.Request(
        mcp_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {api_key}"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        if "error" in res:
            return {"error": res["error"]}
        content = res.get("result", {}).get("content", [])
        if content and content[0].get("type") == "text":
            return json.loads(content[0]["text"])
        return res.get("result", {})

print("=======================================================")
print("STARTING CONTROLLED E2E PHYSICAL PRINTER VERIFICATION")
print("=======================================================")

# Step 0: Check printer status
status = call_mcp("get_printer_status", {})
print(f"Printer Status: online={status.get('printer_online')}, queue={status.get('queue_count')}, detail='{status.get('printer_status_detail')}'")
if not status.get("printer_online"):
    print("WARNING: Printer does not report fresh heartbeat, but proceeding with caution.")

# Test 1: Single text ticket with title and subtitle, NO timestamp, NO sender
print("\n--- Test 1/4: Single Ticket with Title and Subtitle (no timestamp) ---")
t1_res = call_mcp("print_text", {
    "title": "TEST 1: UNTERTITEL",
    "subtitle": "E2E Einzelticket",
    "content": "Verifikation 1/4: Titel + Untertitel ohne Zeitstempel und ohne Absender.",
    "show_timestamp": False,
    "show_sender": False,
    "wait_for_result": True,
    "timeout_seconds": 20
})
print("Test 1 Result:", json.dumps(t1_res, indent=2))
job1_id = t1_res.get("job_id")

# If wait_for_result returned printed or published, check job status
if job1_id:
    time.sleep(2)
    s1 = call_mcp("get_print_job_status", {"job_id": job1_id})
    print(f"Job 1 Polled Status: status={s1.get('status')}, confirmed={s1.get('printer_confirmation')}")

# Test 2: Badge ticket with MOTIVATION
print("\n--- Test 2/4: Badge Ticket with MOTIVATION ---")
t2_res = call_mcp("print_text", {
    "title": "TEST 2: BADGE",
    "subtitle": "High-Contrast Thermodruck",
    "badge": "MOTIVATION",
    "badge_style": "inverted",
    "content": "Verifikation 2/4: Thermodruck mit inversem Badge-Block.",
    "show_timestamp": False,
    "show_sender": False,
    "wait_for_result": True,
    "timeout_seconds": 20
})
print("Test 2 Result:", json.dumps(t2_res, indent=2))
job2_id = t2_res.get("job_id")

if job2_id:
    time.sleep(2)
    s2 = call_mcp("get_print_job_status", {"job_id": job2_id})
    print(f"Job 2 Polled Status: status={s2.get('status')}, confirmed={s2.get('printer_confirmation')}")

# Test 3 & 4: Batch of 2 tickets (one with ONLY 'text', one with ONLY 'content')
print("\n--- Test 3 & 4: Batch Tickets (text vs content aliasing) ---")
batch_res = call_mcp("print_tickets", {
    "tickets": [
        {
            "title": "TEST 3: NUR TEXT",
            "subtitle": "Feld-Aliasing Test",
            "text": "Verifikation 3/4: Batch-Ticket ausschliesslich mit 'text' (kein leeres Ticket!)."
        },
        {
            "title": "TEST 4: NUR CONTENT",
            "subtitle": "Kanonisches Feld",
            "content": "Verifikation 4/4: Batch-Ticket mit kanonischem Feld 'content'."
        }
    ],
    "separate": True,
    "wait_for_result": True,
    "timeout_seconds": 25
})
print("Batch Result:", json.dumps(batch_res, indent=2))
batch_id = batch_res.get("batch_id")

if batch_id:
    time.sleep(4)
    sb = call_mcp("get_batch_status", {"batch_id": batch_id})
    print(f"Batch Polled Status: status={sb.get('status')}, confirmed={sb.get('confirmed_tickets')}/{sb.get('total_tickets')}")

print("\n=======================================================")
print("E2E VERIFICATION COMPLETED (TOTAL 4 TEST TICKETS SENT)")
print("=======================================================")
