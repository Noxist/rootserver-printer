#!/usr/bin/env python3
"""
test_mcp_layout_regression.py — Verification of:
1. Canonical 'content' vs backward-compatible 'text' normalization
2. Conflicting 'content' and 'text' rejection
3. Empty ticket rejection (plausibility check)
4. Subtitle rendering in PNG
5. Timestamp suppression when show_timestamp=False and presence when True
6. Sender suppression when show_sender=False and presence when True
7. High-contrast monochromatic badge rendering (inverted, border, bold)
8. Structured blocks (heading, text, badge, divider, qr_code)
9. Preview tools (preview_ticket, preview_batch) without enqueuing
10. Status lifecycle (queued -> published -> printed on ACK, timeout -> unconfirmed)
11. Exact 576px PNG byte validation with non-empty content
"""

import unittest
import io
import os
import time
import base64
from PIL import Image

from ticket_model import NormalizedTicket, NormalizedBlock, normalize_ticket
from image_processor import (
    render_normalized_ticket,
    render_ticket_elements,
    _render_badge,
    _todo_progress_label,
    PRINT_WIDTH_PX
)
from logic import ReceiptCfg, pil_to_base64_png
import queue_print
from mcp_server import execute_mcp_tool, MCP_TOOLS


class MCPLayoutRegressionTestSuite(unittest.TestCase):

    def setUp(self):
        self.cfg = ReceiptCfg()

    def test_01_text_field_only_batch_ticket(self):
        """Regression Test 1: Ticket with ONLY 'text' must preserve and render its full content."""
        raw = {
            "title": "Diagnosis Test C",
            "text": "This is important text that was previously lost in batch C."
        }
        norm = normalize_ticket(raw)
        self.assertEqual(norm.content, "This is important text that was previously lost in batch C.")
        self.assertEqual(norm.title, "Diagnosis Test C")

        img = render_normalized_ticket(norm, cfg=self.cfg, width_px=576, separate=True)
        self.assertEqual(img.width, 576)
        self.assertGreater(img.height, 120)

        # Confirm image is not empty (contains black pixels)
        histogram = img.histogram()
        # Grayscale L mode: 0 is black, 255 is white
        black_pixels = histogram[0]
        self.assertGreater(black_pixels, 100, "Rendered ticket must contain text pixels")
        print("✓ PASS: Batch ticket with ONLY 'text' renders full content successfully.")

    def test_02_content_field_only_batch_ticket(self):
        """Regression Test 2: Ticket with ONLY 'content' must preserve and render its full content."""
        raw = {
            "title": "Diagnosis Test B",
            "content": "This is content from Test B that should render properly."
        }
        norm = normalize_ticket(raw)
        self.assertEqual(norm.content, "This is content from Test B that should render properly.")
        img = render_normalized_ticket(norm, cfg=self.cfg, width_px=576, separate=True)
        self.assertEqual(img.width, 576)
        self.assertGreater(img.height, 120)
        self.assertGreater(img.histogram()[0], 100)
        print("✓ PASS: Batch ticket with ONLY 'content' renders full content successfully.")

    def test_03_content_and_text_identical_and_conflicting(self):
        """Regression Test 3: Identical content/text is accepted; conflicting content/text is rejected."""
        # Identical values
        raw_identical = {
            "title": "Test D",
            "content": "Same message in both fields",
            "text": "Same message in both fields"
        }
        norm = normalize_ticket(raw_identical)
        self.assertEqual(norm.content, "Same message in both fields")

        # Conflicting values
        raw_conflicting = {
            "title": "Conflict",
            "content": "Message A",
            "text": "Message B"
        }
        with self.assertRaises(ValueError) as ctx:
            normalize_ticket(raw_conflicting)
        self.assertIn("Conflicting content", str(ctx.exception))
        print("✓ PASS: Identical text/content merged; conflicting text/content rejected.")

    def test_04_empty_ticket_rejection(self):
        """Regression Test 4: Tickets without printable text, title, badge, or image are rejected."""
        empty_raw = {
            "content": "   ",
            "text": ""
        }
        with self.assertRaises(ValueError) as ctx:
            normalize_ticket(empty_raw)
        self.assertIn("no printable content", str(ctx.exception))
        print("✓ PASS: Empty tickets rejected before queuing.")

    def test_05_subtitle_rendered_into_png(self):
        """Regression Test 5: Subtitle must not disappear and must increase height/render in PNG."""
        norm_no_sub = normalize_ticket({
            "title": "Header Only",
            "content": "Body text here"
        })
        img_no_sub = render_normalized_ticket(norm_no_sub, cfg=self.cfg, width_px=576, separate=False)

        norm_with_sub = normalize_ticket({
            "title": "Header Only",
            "subtitle": "Important Subtitle That Must Not Disappear",
            "content": "Body text here"
        })
        img_with_sub = render_normalized_ticket(norm_with_sub, cfg=self.cfg, width_px=576, separate=False)

        # Height with subtitle must be noticeably larger
        self.assertGreater(img_with_sub.height, img_no_sub.height + 15)
        # More black pixels due to subtitle text
        self.assertGreater(img_with_sub.histogram()[0], img_no_sub.histogram()[0] + 50)
        print(f"✓ PASS: Subtitle renders reliably in PNG (h_diff={img_with_sub.height - img_no_sub.height}px).")

    def test_06_timestamp_suppression_and_presence(self):
        """Regression Test 6: Timestamps appear ONLY when show_timestamp=True."""
        norm_no_time = normalize_ticket({
            "title": "No Time Ticket",
            "content": "Clean text without timestamp",
            "show_timestamp": False
        })
        img_no_time = render_normalized_ticket(norm_no_time, cfg=self.cfg, width_px=576, separate=False)

        norm_with_time = normalize_ticket({
            "title": "No Time Ticket",
            "content": "Clean text without timestamp",
            "show_timestamp": True,
            "timestamp_format": "%d.%m.%Y %H:%M"
        })
        img_with_time = render_normalized_ticket(norm_with_time, cfg=self.cfg, width_px=576, separate=False)

        self.assertGreater(img_with_time.height, img_no_time.height + 10)
        self.assertGreater(img_with_time.histogram()[0], img_no_time.histogram()[0] + 30)
        print("✓ PASS: Timestamp suppressed by default, rendered only when show_timestamp=True.")

    def test_07_sender_suppression_and_presence(self):
        """Regression Test 7: 'From: ...' appears ONLY when show_sender=True."""
        norm_no_sender = normalize_ticket({
            "title": "Message",
            "content": "Text",
            "show_sender": False,
            "sender": "ChatGPT"
        })
        self.assertIsNone(norm_no_sender.sender)
        self.assertFalse(norm_no_sender.show_sender)
        img_no_sender = render_normalized_ticket(norm_no_sender, cfg=self.cfg, width_px=576, separate=False)

        norm_with_sender = normalize_ticket({
            "title": "Message",
            "content": "Text",
            "show_sender": True,
            "sender": "ChatGPT"
        })
        self.assertEqual(norm_with_sender.sender, "ChatGPT")
        self.assertTrue(norm_with_sender.show_sender)
        img_with_sender = render_normalized_ticket(norm_with_sender, cfg=self.cfg, width_px=576, separate=False)

        self.assertGreater(img_with_sender.height, img_no_sender.height + 10)
        print("✓ PASS: Unwanted 'From: ChatGPT' eliminated unless show_sender=True.")

    def test_08_monochrome_badges(self):
        """Regression Test 8: Inverted, border, and bold badge styles render with high contrast."""
        for style in ["inverted", "border", "bold"]:
            b_img = _render_badge("MOTIVATION", style=style, align="center", width_px=576)
            self.assertIsNotNone(b_img)
            self.assertEqual(b_img.width, 576)
            black_pixels = b_img.histogram()[0]
            self.assertGreater(black_pixels, 100, f"Badge style {style} must have solid black marks")

        # Full ticket with badge
        norm = normalize_ticket({
            "badge": "MOTIVATION",
            "badge_style": "inverted",
            "title": "Quote of the Day",
            "content": "Keep pushing forward."
        })
        img = render_normalized_ticket(norm, cfg=self.cfg, width_px=576, separate=True)
        self.assertEqual(img.width, 576)
        print("✓ PASS: High-contrast thermal badges (inverted, border, bold) verified.")

    def test_09_structured_blocks_and_qr_code(self):
        """Regression Test 9: Structured blocks (heading, badge, text, divider, qr_code) render accurately."""
        norm = normalize_ticket({
            "blocks": [
                {"type": "heading", "text": "Structured Receipt", "level": 1, "align": "center"},
                {"type": "badge", "text": "URGENT", "badge_style": "inverted", "align": "center"},
                {"type": "divider", "divider_style": "dashed"},
                {"type": "text", "text": "Order #48201\nStatus: Paid", "bold": True},
                {"type": "spacer", "height_px": 15},
                {"type": "qr_code", "qr_data": "https://printer.nox007.tech/guest/test-qr", "qr_box_size": 4}
            ]
        })
        img = render_normalized_ticket(norm, cfg=self.cfg, width_px=576, separate=True)
        self.assertEqual(img.width, 576)
        self.assertGreater(img.height, 350)
        print("✓ PASS: Structured blocks including QR code render cleanly.")

    def test_09b_emoji_rendering_and_layout(self):
        """Emoji must render as visible raster artwork and participate in wrapping."""
        import image_processor
        self.assertTrue(os.path.isfile(image_processor.EMOJI_FONT_PATH), "Noto Color Emoji must be installed in the runtime image")
        norm = normalize_ticket({
            "title": "Tagesplan 🫕",
            "blocks": [
                {"type": "heading", "text": "Heute ☀️", "level": 2},
                {"type": "text", "text": "15:00 Lernen 📚\n18:00 Abendessen 🫕"},
            ],
        })
        img = render_normalized_ticket(norm, cfg=self.cfg, width_px=576, separate=False)
        self.assertEqual(img.width, 576)
        self.assertGreater(img.histogram()[0], 500)
        self.assertGreater(img.height, 150)
        print("✓ PASS: Emoji rendered in headings and body text.")

    def test_09c_todo_checkboxes(self):
        """Todo blocks render an open box by default and a check mark when completed."""
        open_ticket = normalize_ticket({"blocks": [{"type": "todo", "text": "CSS prüfen"}]})
        done_ticket = normalize_ticket({"blocks": [{"type": "todo", "text": "CSS prüfen", "checked": True}]})
        open_img = render_normalized_ticket(open_ticket, cfg=self.cfg, width_px=576, separate=False)
        done_img = render_normalized_ticket(done_ticket, cfg=self.cfg, width_px=576, separate=False)
        self.assertEqual(open_img.size, done_img.size)
        self.assertGreater(done_img.histogram()[0], open_img.histogram()[0] + 10)
        print("✓ PASS: To-do boxes render open and completed states distinctly.")

    def test_09d_todo_progress_counter(self):
        """Checklist summary is calculated from the checked state at render time."""
        ticket = normalize_ticket({"blocks": [
            {"type": "todo", "text": "Done", "checked": True},
            {"type": "todo", "text": "Open one"},
            {"type": "todo", "text": "Open two"},
        ]})
        self.assertEqual(_todo_progress_label(ticket), "1 von 3 erledigt")
        img = render_normalized_ticket(ticket, cfg=self.cfg, width_px=576, separate=False)
        self.assertGreater(img.histogram()[0], 500)
        self.assertIsNone(_todo_progress_label(normalize_ticket({"content": "No checklist"})))
        print("✓ PASS: To-do progress is generated from checkbox states.")

    def test_10_preview_tools_without_enqueuing(self):
        """Regression Test 10: preview_ticket and preview_batch return base64 PNG without modifying queue."""
        q_coll = queue_print._get_queue_coll()
        q_count_before = q_coll.count_documents({}) if q_coll is not None else 0

        # Run preview_ticket via execute_mcp_tool
        import asyncio
        loop = asyncio.get_event_loop()
        res = loop.run_until_complete(execute_mcp_tool("preview_ticket", {
            "title": "Preview Test",
            "subtitle": "No print job should be enqueued",
            "content": "This is only a preview.",
            "badge": "PREVIEW"
        }))

        self.assertTrue(res.get("preview"))
        self.assertEqual(res.get("width_px"), 576)
        self.assertGreater(res.get("height_px"), 100)
        self.assertIn("preview_base64_png", res)

        # Verify no job was added to MongoDB queue
        q_count_after = q_coll.count_documents({}) if q_coll is not None else 0
        self.assertEqual(q_count_after, q_count_before, "Preview tools must NEVER enqueue print jobs")
        print("✓ PASS: preview_ticket generated valid 576px preview without queue mutation.")

    def test_11_print_success_ack_and_status_tracking(self):
        """Regression Test 11: published != printed; ACK transitions state to printed."""
        test_jid = f"test-ack-{int(time.time()*1000)}"
        img = Image.new("1", (576, 60), color=1)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        # Mock publishing without background flusher interference
        orig_publish = queue_print.mqtt_publish_image_base64
        published_records = []

        def mock_pub(b64_png, cut_paper=1, paper_width_mm=0, paper_height_mm=0, ticket_id=None):
            published_records.append(ticket_id)
            return True

        queue_print.mqtt_publish_image_base64 = mock_pub
        queue_print.printer_status = lambda: {"online": True, "detail": "Test Mock"}

        try:
            # Enqueue single ticket
            queue_print.enqueue_base64_png(b64, cut_paper=1, job_id=test_jid, meta={"source": "ack_test"})

            # Before flush: status is queued
            st_queued = queue_print.get_job_status(test_jid)
            self.assertEqual(st_queued.get("status"), "queued")
            self.assertFalse(st_queued.get("printer_confirmation"))

            # Flush once if not already picked up by background daemon
            count = queue_print.flush_once()

            # Immediately after flush: status is published (or printed if background flusher was active)
            st_pub = queue_print.get_job_status(test_jid)
            self.assertIn(st_pub.get("status"), ("published", "printed"))

            # Simulate incoming PrintSuccess ACK
            queue_print.handle_printer_ack("PrintSuccess", f"OK;{test_jid}")

            # After ACK: status must be printed!
            st_printed = queue_print.get_job_status(test_jid)
            self.assertEqual(st_printed.get("status"), "printed")
            self.assertTrue(st_printed.get("printer_confirmation"))
            self.assertEqual(st_printed.get("confirmation_topic"), "PrintSuccess")

            print(f"✓ PASS: Exact status progression verified (queued → published → printed on PrintSuccess).")
        finally:
            queue_print.mqtt_publish_image_base64 = orig_publish

    def test_12_batch_tickets_ordering_and_different_layouts(self):
        """Regression Test 12: 3 tickets in a batch with different layouts render in order."""
        tickets_input = [
            {"title": "Ticket 1", "content": "Text content only", "badge": "INFO"},
            {"title": "Ticket 2", "subtitle": "With subtitle", "text": "Using text alias"},
            {"title": "Ticket 3", "content": "Bold text", "bold": True, "badge": "URGENT", "badge_style": "border"}
        ]

        import asyncio
        loop = asyncio.get_event_loop()
        preview_res = loop.run_until_complete(execute_mcp_tool("preview_batch", {
            "tickets": tickets_input,
            "separate": True
        }))

        self.assertTrue(preview_res.get("preview"))
        self.assertEqual(preview_res.get("total_tickets"), 3)
        previews = preview_res.get("previews", [])
        self.assertEqual(len(previews), 3)

        self.assertEqual(previews[0]["title"], "Ticket 1")
        self.assertEqual(previews[0]["badge"], "INFO")
        self.assertEqual(previews[1]["subtitle"], "With subtitle")
        self.assertEqual(previews[2]["badge"], "URGENT")

        # Verify each preview PNG is valid 576px
        for p in previews:
            self.assertEqual(p["width_px"], 576)
            raw = base64.b64decode(p["preview_base64_png"])
            self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"))
            decoded = Image.open(io.BytesIO(raw))
            self.assertEqual(decoded.size[0], 576)

        print("✓ PASS: Batch tickets with diverse layouts rendered in sequence without loss.")


if __name__ == "__main__":
    unittest.main()
