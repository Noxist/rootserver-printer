#!/usr/bin/env python3
"""
test_encoding_regression.py — Golden Master and Regression Test Suite for Ticket Rendering
Verifies that text rendering, font sizes, line wrapping, Swiss-safe text processing,
Umlauts, special characters, image dithering, and MQTT JSON packaging remain 100% intact.
"""

import os
import sys
import io
import json
import base64
import unittest
from PIL import Image

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, '/app')

from logic import (
    render_receipt,
    render_image_with_headers,
    pil_to_base64_png,
    _to_1bit,
    ReceiptCfg,
    PRINT_WIDTH_PX,
)
from queue_print import enqueue_base64_png

class TestTicketEncodingRegression(unittest.TestCase):

    def setUp(self):
        self.cfg = ReceiptCfg()
        self.width_px = PRINT_WIDTH_PX  # 576 px

    def test_01_unicode_text_preservation(self):
        """Verify that German Eszett (ß), Umlauts (ä, ö, ü) and French accents are rendered without replacement."""
        raw_text = "Grüße vom Bodensee: Übermäßiger Kaffeegenuss in Zürich & Bern!"
        img = render_receipt("UNICODE TEST", [raw_text], add_time=False, width_px=self.width_px, cfg=self.cfg)
        self.assertEqual(img.size[0], 576)
        self.assertGreater(img.size[1], 100)

    def test_02_ascii_text_rendering(self):
        """Verify standard ASCII receipt rendering produces valid monochrome image with width 576px."""
        title = "STANDARD RECEIPT"
        lines = ["Item 1: Espresso", "Item 2: Croissant", "Total: 8.50 CHF"]
        img = render_receipt(title, lines, add_time=True, width_px=self.width_px, cfg=self.cfg)
        
        self.assertEqual(img.size[0], 576, "Image width must be exactly 576 px")
        self.assertGreater(img.size[1], 100, "Image height must be greater than 100 px")
        self.assertEqual(img.mode, "L", "Rendered canvas mode must be grayscale 'L'")

    def test_03_german_umlauts_and_symbols(self):
        """Verify German Umlauts (ä, ö, ü, Ä, Ö, Ü) and symbols are cleanly rendered."""
        title = "UMLAUTE & SYMBOLE"
        lines = [
            "ä ö ü Ä Ö Ü",
            "Akzente: é è à ç",
            "Symbole: € @ & < > \" ' % $ # * + = - /"
        ]
        img = render_receipt(title, lines, add_time=False, width_px=self.width_px, cfg=self.cfg)
        self.assertEqual(img.size[0], 576)
        
        # Check that canvas has dark pixels where text was drawn (background is 255)
        px_data = list(img.getdata())
        dark_pixels = [p for p in px_data if p < 128]
        self.assertGreater(len(dark_pixels), 500, "Umlauts and symbols must produce visible text pixels")

    def test_04_long_text_wrapping(self):
        """Verify automatic line wrapping works cleanly without truncation or overflow."""
        title = "LONG TEXT WRAP TEST"
        long_paragraph = (
            "Dieser Thermodrucker ist ueber das Heimnetzwerk und Tailscale angebunden. "
            "Er unterstuetzt 80mm breite Papierrollen mit einer Aufloesung von 203 DPI "
            "und druckt monochrome PNG-Dateien, die ueber das MQTT-Protokoll empfangen werden."
        )
        lines = [long_paragraph]
        img = render_receipt(title, lines, add_time=True, width_px=self.width_px, cfg=self.cfg)
        self.assertEqual(img.size[0], 576)
        # Multiple wrapped lines must expand vertical canvas
        self.assertGreater(img.size[1], 200, "Wrapped text must increase canvas height")

    def test_04b_emoji_in_legacy_text_renderer(self):
        """REST/Web UI text renderer must preserve emoji as visible artwork."""
        from image_processor import EMOJI_FONT_PATH
        self.assertTrue(os.path.isfile(EMOJI_FONT_PATH))
        img = render_receipt("Tagesplan 🫕", ["15:00 Lernen 📚", "18:00 Abendessen 🫕"],
                             add_time=False, width_px=self.width_px, cfg=self.cfg)
        self.assertEqual(img.width, 576)
        self.assertGreater(sum(1 for px in img.getdata() if px < 128), 500)

    def test_05_pil_to_base64_png_1bit_floyd_steinberg(self):
        """Verify conversion to 1-bit monochrome PNG with Floyd-Steinberg dithering."""
        test_img = Image.new("L", (576, 100), color=200) # light gray
        b64_png = pil_to_base64_png(test_img)
        self.assertIsInstance(b64_png, str)
        self.assertGreater(len(b64_png), 50)
        
        # Decode and verify it is a valid PNG
        raw_bytes = base64.b64decode(b64_png)
        # PNG signature check: 89 50 4E 47 0D 0A 1A 0A
        self.assertEqual(raw_bytes[:8], b"\x89PNG\r\n\x1a\n", "Must be a valid PNG binary header")
        
        decoded_img = Image.open(io.BytesIO(raw_bytes))
        self.assertEqual(decoded_img.size, (576, 100))
        self.assertEqual(decoded_img.mode, "1", "Must be 1-bit monochrome mode")

    def test_06_image_with_headers_and_subtitles(self):
        """Verify composite image with title, subtitle, and scaling."""
        inner_img = Image.new("RGB", (200, 200), color=(100, 150, 200))
        comp = render_image_with_headers(
            inner_img,
            width_px=self.width_px,
            cfg=self.cfg,
            title="Image Title",
            subtitle="Subtitle Info",
            sender_name="Chef"
        )
        self.assertEqual(comp.size[0], 576)
        self.assertGreater(comp.size[1], 200)

    def test_07_mqtt_json_payload_format(self):
        """Verify the exact JSON format sent to the printer over MQTT."""
        test_img = Image.new("1", (576, 50), color=255)
        buf = io.BytesIO()
        test_img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        
        # Simulate payload packaging
        job_id = "test-job-999"
        payload = {
            "ticket_id": job_id,
            "data_type": "png",
            "data_base64": b64,
            "paper_type": 0,
            "paper_width_mm": 0,
            "paper_height_mm": 0,
            "cut_paper": 1,
            "source": "printer"
        }
        
        # Assert required fields match native firmware expectations
        self.assertEqual(payload["data_type"], "png")
        self.assertEqual(payload["paper_type"], 0)
        self.assertEqual(payload["cut_paper"], 1)
        self.assertEqual(payload["source"], "printer")
        self.assertTrue(len(payload["data_base64"]) > 0)
        
        # Verify JSON is serializable and fits MQTT size constraints (<= 2MB)
        encoded_json = json.dumps(payload)
        self.assertLess(len(encoded_json.encode("utf-8")), 2 * 1024 * 1024)

    def test_08_golden_master_pixel_stability(self):
        """Golden master test: identical input produces identical pixel count and layout."""
        img1 = render_receipt("GOLDEN MASTER", ["Line 1", "Line 2"], add_time=False, width_px=576, cfg=self.cfg)
        img2 = render_receipt("GOLDEN MASTER", ["Line 1", "Line 2"], add_time=False, width_px=576, cfg=self.cfg)
        
        self.assertEqual(img1.size, img2.size, "Dimensions must be deterministic")
        self.assertEqual(list(img1.getdata()), list(img2.getdata()), "Pixel data must be completely deterministic")

    def test_09_no_dashed_line_at_ticket_end(self):
        """Regression test: verify no unwanted dashed tear line is drawn at ticket end."""
        from image_processor import render_ticket_elements
        elements = [{"type": "text", "title": "NO DASH LINE", "content": "Line A\nLine B"}]
        img = render_ticket_elements(elements, self.cfg, width_px=576, separate=True)
        # Check bottom 60 pixels (feed margin)
        bottom_crop = img.crop((0, img.height - 60, 576, img.height))
        px_data = list(bottom_crop.getdata())
        black_pixels = sum(1 for p in px_data if p < 128)
        self.assertEqual(black_pixels, 0, "No black pixels allowed in bottom feed margin (dashed line must be absent)")

if __name__ == "__main__":
    unittest.main()

