# image_processor.py — SSRF-safe image fetching, tone mapping, dithering, and multi-element rendering
import io
import os
import ssl
import time
import base64
import socket
import ipaddress
import urllib.parse
import unicodedata
from typing import Optional, List, Dict, Any, Union

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageOps, UnidentifiedImageError

from logic import (
    ReceiptCfg,
    _safe_font,
    _text_length,
    _wrap,
    _line_height,
    _x_for_align,
    _apply_tone,
    _to_1bit,
    pil_to_base64_png,
    cfg_get,
    log,
    PRINT_WIDTH_PX,
)

MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_IMAGE_PIXELS = 20_000_000          # 20 Megapixels
DOWNLOAD_TIMEOUT_SECONDS = 8.0
EMOJI_FONT_PATH = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"


def _is_emoji_codepoint(char: str) -> bool:
    code = ord(char)
    return (0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27FF
            or 0x2300 <= code <= 0x23FF or 0xFE0F == code)


def _text_clusters(text: str):
    """Yield grapheme-like clusters, keeping emoji modifiers, flags and ZWJ sequences together."""
    cluster = ""
    join_next = False
    regional_count = 0
    for char in text:
        code = ord(char)
        is_regional = 0x1F1E6 <= code <= 0x1F1FF
        extends = (unicodedata.combining(char) != 0 or 0xFE00 <= code <= 0xFE0F
                   or 0x1F3FB <= code <= 0x1F3FF or code == 0x20E3)
        if not cluster:
            cluster = char
        elif extends or join_next or code == 0x200D or (is_regional and regional_count == 1):
            cluster += char
        else:
            yield cluster
            cluster = char
            regional_count = 0
        if is_regional:
            regional_count = (regional_count + 1) % 2
        elif not extends and code != 0x200D:
            regional_count = 0
        join_next = code == 0x200D
    if cluster:
        yield cluster


def _emoji_font(size: int):
    try:
        # Noto Color Emoji is a bitmap font with a fixed 109px strike.
        return _safe_font(EMOJI_FONT_PATH, 109)
    except Exception:
        return None


def _cluster_width(cluster: str, font) -> int:
    if not any(_is_emoji_codepoint(c) for c in cluster):
        return _text_length(cluster, font)
    emoji = _emoji_font(12)
    if emoji is None:
        return _text_length(cluster, font)
    box = emoji.getbbox(cluster)
    return max(1, round((box[2] - box[0]) * font.size / 109))


def _receipt_text_width(text: str, font) -> int:
    return sum(_cluster_width(cluster, font) for cluster in _text_clusters(text))


def _wrap_receipt_text(text: str, font, max_px: int) -> List[str]:
    """Wrap text using the same mixed-font measurements used by the renderer."""
    lines = []
    for raw in text.splitlines() or [text]:
        words = raw.split()
        if not words:
            lines.append("")
            continue
        line = ""
        for word in words:
            candidate = f"{line} {word}" if line else word
            if _receipt_text_width(candidate, font) <= max_px:
                line = candidate
            else:
                if line:
                    lines.append(line)
                line = ""
                part = ""
                for cluster in _text_clusters(word):
                    candidate = part + cluster
                    if part and _receipt_text_width(candidate, font) > max_px:
                        lines.append(part)
                        part = cluster
                    else:
                        part = candidate
                line = part
        if line:
            lines.append(line)
    return lines


def _draw_receipt_text(image, draw, xy, text: str, font, fill=0):
    """Draw ordinary text with a monochrome raster emoji fallback where available."""
    x, y = xy
    for cluster in _text_clusters(text):
        if any(_is_emoji_codepoint(c) for c in cluster):
            emoji_font = _emoji_font(12)
            if emoji_font is not None:
                box = emoji_font.getbbox(cluster)
                w, h = max(1, box[2] - box[0]), max(1, box[3] - box[1])
                layer = Image.new("RGBA", (w + 4, h + 4), (255, 255, 255, 0))
                ImageDraw.Draw(layer).text((2 - box[0], 2 - box[1]), cluster, font=emoji_font, embedded_color=True)
                alpha = layer.getchannel("A")
                opaque = Image.new("RGB", layer.size, (255, 255, 255))
                opaque.paste(layer.convert("RGB"), mask=alpha)
                mono = ImageOps.grayscale(opaque)
                # Bright emoji colors become too pale on thermal paper; boost contrast before reducing size.
                mono = ImageEnhance.Contrast(mono).enhance(2.4)
                target_h = max(1, round(font.size))
                target_w = max(1, round(layer.width * target_h / layer.height))
                mono = mono.resize((target_w, target_h), Image.Resampling.LANCZOS)
                if fill == 255:
                    mono = ImageOps.invert(mono)
                image.paste(mono, (round(x), round(y)))
                x += _cluster_width(cluster, font)
                continue
        draw.text((round(x), round(y)), cluster, fill=fill, font=font)
        x += _text_length(cluster, font)


def _receipt_x_for_align(text: str, font, width_px: int, align: str, left: int, right: int) -> int:
    text_w = _receipt_text_width(text, font)
    if align == "center":
        return max(left, (width_px - text_w) // 2)
    if align == "right":
        return max(left, width_px - right - text_w)
    return left

# ----------------- SSRF & IP Validation -----------------

def is_ip_private_or_restricted(ip_str: str) -> bool:
    """Checks if an IP address belongs to private, loopback, link-local, or multicast ranges."""
    try:
        ip = ipaddress.ip_address(ip_str)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    except ValueError:
        return True


def validate_safe_url(url: str) -> tuple[bool, str, list[str]]:
    """
    Validates that a URL is HTTPS and resolves strictly to public, non-private IP addresses.
    Returns (is_safe, error_msg, resolved_ips).
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https":
        return False, "Only HTTPS URLs are permitted for image fetching.", []

    hostname = parsed.hostname
    if not hostname:
        return False, "Invalid URL: missing hostname.", []

    # Disallow known cloud metadata hostnames directly
    if hostname.lower() in ("metadata.google.internal", "instance-data", "169.254.169.254"):
        return False, "Access to cloud metadata endpoints is prohibited.", []

    try:
        addr_info = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
        resolved_ips = list({info[4][0] for info in addr_info})
    except socket.gaierror as e:
        return False, f"DNS resolution failed for hostname '{hostname}': {e}", []

    if not resolved_ips:
        return False, f"No IP addresses resolved for hostname '{hostname}'.", []

    for ip in resolved_ips:
        if is_ip_private_or_restricted(ip):
            return False, f"Access to private/restricted IP address ({ip}) is blocked.", []

    return True, "", resolved_ips


async def fetch_image_from_url_safe(url: str) -> Image.Image:
    """
    Downloads an image securely:
    - Enforces HTTPS
    - Validates against SSRF (private IPs & metadata)
    - Limits redirects (max 3, each re-checked)
    - Limits response size (streaming up to 10MB)
    - Enforces timeout
    """
    current_url = url
    for _ in range(3):
        is_safe, err_msg, _ = validate_safe_url(current_url)
        if not is_safe:
            raise ValueError(f"SSRF Protection: {err_msg}")

        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False) as client:
            try:
                response = await client.get(current_url, headers={"User-Agent": "ThermalPrinter/2.0 (SSRF-Safe)"})
            except Exception as exc:
                raise ValueError(f"HTTP request to '{current_url}' failed: {exc}")

            if response.status_code in (301, 302, 303, 307, 308):
                redirect_target = response.headers.get("location")
                if not redirect_target:
                    raise ValueError("Redirect received without Location header.")
                current_url = urllib.parse.urljoin(current_url, redirect_target)
                continue

            if response.status_code != 200:
                raise ValueError(f"HTTP error {response.status_code} fetching image.")

            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
                raise ValueError(f"Image too large ({content_length} bytes, limit is {MAX_DOWNLOAD_BYTES} bytes).")

            body = response.content
            if len(body) > MAX_DOWNLOAD_BYTES:
                raise ValueError(f"Downloaded image exceeded size limit ({len(body)} bytes).")

            if not body:
                raise ValueError("Downloaded image is empty.")

            try:
                img = Image.open(io.BytesIO(body))
                w, h = img.size
                if w < 1 or h < 1 or w * h > MAX_IMAGE_PIXELS:
                    raise ValueError(f"Image dimensions ({w}x{h}) exceed safety limit.")
                img.load()
                return img
            except UnidentifiedImageError as e:
                raise ValueError(f"Downloaded file is not a valid image: {e}")
            except Exception as e:
                raise ValueError(f"Failed to decode downloaded image: {e}")

    raise ValueError("Too many redirects while fetching image.")


def load_image_from_base64(b64_string: str) -> Image.Image:
    """Decodes a base64 string (with or without data URI prefix) into a PIL Image."""
    if "," in b64_string and ";base64," in b64_string:
        b64_string = b64_string.split(";base64,", 1)[1]

    try:
        raw_bytes = base64.b64decode(b64_string.strip())
        if len(raw_bytes) > MAX_DOWNLOAD_BYTES:
            raise ValueError(f"Base64 image exceeds {MAX_DOWNLOAD_BYTES} bytes.")
        img = Image.open(io.BytesIO(raw_bytes))
        w, h = img.size
        if w < 1 or h < 1 or w * h > MAX_IMAGE_PIXELS:
            raise ValueError(f"Image dimensions ({w}x{h}) exceed safety limit.")
        img.load()
        return img
    except Exception as e:
        raise ValueError(f"Invalid base64 image data: {e}")


# ----------------- Image Scaling & Conditioning -----------------

def scale_and_center_image(img: Image.Image, target_width: int = PRINT_WIDTH_PX) -> Image.Image:
    """Converts image to grayscale, scales to target_width while preserving aspect ratio."""
    if img.mode != "L":
        img = img.convert("L")

    w, h = img.size
    if w <= 0 or h <= 0:
        raise ValueError("Invalid image dimensions.")

    if w != target_width:
        scale = target_width / float(w)
        new_h = max(1, int(h * scale))
        img = img.resize((target_width, new_h), Image.LANCZOS)

    return img


from zoneinfo import ZoneInfo
from datetime import datetime
from ticket_model import NormalizedTicket, NormalizedBlock, normalize_ticket

# ----------------- Badge & Ticket Layout Rendering -----------------

def _render_badge(
    text: str,
    style: str = "inverted",
    align: str = "center",
    width_px: int = PRINT_WIDTH_PX,
    margin_left: int = 18,
    margin_right: int = 18,
) -> Optional[Image.Image]:
    badge_text = text.strip()
    if not badge_text:
        return None
    font = _safe_font("DejaVuSans-Bold.ttf", 22)
    bbox = font.getbbox(badge_text)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    pad_x = 16
    pad_y = 6
    badge_w = text_w + (pad_x * 2)
    badge_h = text_h + (pad_y * 2) + 2

    block_h = badge_h + 12
    img = Image.new("L", (width_px, block_h), color=255)
    draw = ImageDraw.Draw(img)

    if align == "center":
        bx = (width_px - badge_w) // 2
    elif align == "right":
        bx = width_px - margin_right - badge_w
    else:
        bx = margin_left
    by = 4

    if style == "border":
        draw.rounded_rectangle((bx, by, bx + badge_w, by + badge_h), radius=6, outline=0, width=3)
        tx = bx + pad_x - bbox[0]
        ty = by + pad_y - bbox[1]
        draw.text((tx, ty), badge_text, fill=0, font=font)
    elif style == "bold":
        bold_font = _safe_font("DejaVuSans-Bold.ttf", 24)
        b_text = f"◆ {badge_text} ◆"
        b_bbox = bold_font.getbbox(b_text)
        bw = b_bbox[2] - b_bbox[0]
        if align == "center":
            tx = (width_px - bw) // 2
        elif align == "right":
            tx = width_px - margin_right - bw
        else:
            tx = margin_left
        draw.text((tx, by + 4), b_text, fill=0, font=bold_font)
    else:  # "inverted"
        draw.rounded_rectangle((bx, by, bx + badge_w, by + badge_h), radius=6, fill=0)
        tx = bx + pad_x - bbox[0]
        ty = by + pad_y - bbox[1]
        draw.text((tx, ty), badge_text, fill=255, font=font)

    return img


def _todo_progress_label(ticket: NormalizedTicket) -> Optional[str]:
    todo_items = [block for block in (ticket.blocks or []) if block.type == "todo"]
    if not todo_items:
        return None
    completed = sum(1 for item in todo_items if item.checked)
    return f"{completed} von {len(todo_items)} erledigt"


def render_normalized_ticket(
    ticket: NormalizedTicket,
    cfg: Optional[ReceiptCfg] = None,
    width_px: int = PRINT_WIDTH_PX,
    separate: bool = True,
) -> Image.Image:
    """
    Renders a NormalizedTicket into a clean 576px monochrome receipt.
    Unified rendering logic shared across MCP, REST API, Web UI, and Guest links.
    """
    if cfg is None:
        cfg = ReceiptCfg()

    margin_left = cfg.margin_left
    margin_right = cfg.margin_right
    max_w = width_px - margin_left - margin_right

    # Select body font
    if ticket.font_size == "small":
        body_size = 20
    elif ticket.font_size == "large":
        body_size = 36
    else:
        body_size = 28
    font_body_name = "DejaVuSans-Bold.ttf" if ticket.bold else "DejaVuSans.ttf"
    font_body = _safe_font(font_body_name, body_size)

    font_title = _safe_font("DejaVuSans-Bold.ttf", 36)
    font_sub = _safe_font("DejaVuSans.ttf", 22)
    font_meta = _safe_font("DejaVuSans.ttf", 20)

    lh_title = _line_height(font_title, 1.15)
    lh_sub = _line_height(font_sub, 1.15)
    lh_body = _line_height(font_body, 1.15)
    lh_meta = _line_height(font_meta, 1.15)

    sections: List[Image.Image] = []

    # 1. Badge (if present)
    if ticket.badge:
        b_img = _render_badge(
            ticket.badge,
            style=ticket.badge_style,
            align=ticket.badge_alignment,
            width_px=width_px,
            margin_left=margin_left,
            margin_right=margin_right
        )
        if b_img:
            sections.append(b_img)

    # 2. Title & Subtitle block
    title_lines = _wrap_receipt_text(ticket.title, font_title, max_w) if ticket.title else []
    sub_lines = _wrap_receipt_text(ticket.subtitle, font_sub, max_w) if ticket.subtitle else []

    header_height = 0
    if title_lines:
        header_height += len(title_lines) * lh_title + 6
    if sub_lines:
        header_height += len(sub_lines) * lh_sub + 6

    if header_height > 0:
        h_img = Image.new("L", (width_px, header_height), color=255)
        h_draw = ImageDraw.Draw(h_img)
        hy = 0
        for t_line in title_lines:
            tx = _receipt_x_for_align(t_line, font_title, width_px, ticket.align or cfg.align_title, margin_left, margin_right)
            _draw_receipt_text(h_img, h_draw, (tx, hy), t_line, font_title)
            hy += lh_title
        if title_lines and sub_lines:
            hy += 4
        for s_line in sub_lines:
            sx = _receipt_x_for_align(s_line, font_sub, width_px, ticket.align or "left", margin_left, margin_right)
            _draw_receipt_text(h_img, h_draw, (sx, hy), s_line, font_sub)
            hy += lh_sub
        sections.append(h_img)

    todo_progress = _todo_progress_label(ticket)
    if todo_progress:
        progress_img = _render_badge(
            todo_progress, style="border", align="left", width_px=width_px,
            margin_left=margin_left, margin_right=margin_right,
        )
        if progress_img:
            sections.append(progress_img)

    # 3. Meta lines (Sender, Timestamp) - ONLY rendered when explicitly enabled
    meta_lines = []
    if ticket.show_sender and ticket.sender:
        meta_lines.append(f"From: {ticket.sender}")
    if ticket.show_timestamp:
        try:
            tz = ZoneInfo(ticket.timestamp_timezone)
        except Exception:
            tz = ZoneInfo("Europe/Zurich")
        meta_lines.append(datetime.now(tz).strftime(ticket.timestamp_format))

    if meta_lines:
        m_img = Image.new("L", (width_px, len(meta_lines) * lh_meta + 8), color=255)
        m_draw = ImageDraw.Draw(m_img)
        my = 2
        for m_line in meta_lines:
            mx = _receipt_x_for_align(m_line, font_meta, width_px, ticket.align or "left", margin_left, margin_right)
            _draw_receipt_text(m_img, m_draw, (mx, my), m_line, font_meta)
            my += lh_meta
        sections.append(m_img)

    # 4. Optional Image (if top-level image provided)
    if ticket.image_obj:
        scaled_img = scale_and_center_image(ticket.image_obj, width_px)
        sections.append(scaled_img)

    # 5. Content or Structured Blocks
    if ticket.blocks:
        for blk in ticket.blocks:
            if blk.type == "heading":
                h_size = 36 if blk.level == 1 else (28 if blk.level == 2 else 22)
                h_font = _safe_font("DejaVuSans-Bold.ttf", h_size)
                h_lh = _line_height(h_font, 1.15)
                lines = _wrap_receipt_text(blk.text, h_font, max_w)
                b_img = Image.new("L", (width_px, len(lines) * h_lh + 8), color=255)
                b_draw = ImageDraw.Draw(b_img)
                by = 2
                for ln in lines:
                    bx = _receipt_x_for_align(ln, h_font, width_px, blk.align or "left", margin_left, margin_right)
                    _draw_receipt_text(b_img, b_draw, (bx, by), ln, h_font)
                    by += h_lh
                sections.append(b_img)

            elif blk.type == "todo" and blk.text.strip():
                f_size = 20 if blk.font_size == "small" else (36 if blk.font_size == "large" else 28)
                todo_font = _safe_font("DejaVuSans-Bold.ttf" if blk.bold else "DejaVuSans.ttf", f_size)
                todo_lh = _line_height(todo_font, 1.15)
                box_size = 22
                text_x = margin_left + box_size + 12
                lines = _wrap_receipt_text(blk.text.strip(), todo_font, max(1, max_w - box_size - 12))
                block_height = max(len(lines) * todo_lh, box_size) + 10
                todo_img = Image.new("L", (width_px, block_height), color=255)
                todo_draw = ImageDraw.Draw(todo_img)
                box_y = (block_height - box_size) // 2
                todo_draw.rectangle(
                    (margin_left, box_y, margin_left + box_size, box_y + box_size),
                    outline=0, width=3,
                )
                if blk.checked:
                    todo_draw.line(
                        [(margin_left + 4, box_y + 11), (margin_left + 9, box_y + 17),
                         (margin_left + 19, box_y + 5)],
                        fill=0, width=3,
                    )
                text_y = (block_height - len(lines) * todo_lh) // 2
                for line in lines:
                    _draw_receipt_text(todo_img, todo_draw, (text_x, text_y), line, todo_font)
                    text_y += todo_lh
                sections.append(todo_img)

            elif blk.type == "text":
                f_size = 20 if blk.font_size == "small" else (36 if blk.font_size == "large" else 28)
                t_font_name = "DejaVuSans-Bold.ttf" if blk.bold else "DejaVuSans.ttf"
                t_font = _safe_font(t_font_name, f_size)
                t_lh = _line_height(t_font, 1.15)
                wrapped = []
                for raw_line in blk.text.splitlines():
                    if not raw_line.strip():
                        wrapped.append("")
                    else:
                        wrapped.extend(_wrap_receipt_text(raw_line.strip(), t_font, max_w))
                if wrapped:
                    b_img = Image.new("L", (width_px, len(wrapped) * t_lh + 6), color=255)
                    b_draw = ImageDraw.Draw(b_img)
                    by = 2
                    for ln in wrapped:
                        bx = _receipt_x_for_align(ln, t_font, width_px, blk.align or "left", margin_left, margin_right)
                        _draw_receipt_text(b_img, b_draw, (bx, by), ln, t_font)
                        by += t_lh
                    sections.append(b_img)

            elif blk.type == "badge":
                b_img = _render_badge(blk.text, style=blk.badge_style, align=blk.align, width_px=width_px, margin_left=margin_left, margin_right=margin_right)
                if b_img:
                    sections.append(b_img)

            elif blk.type == "divider":
                div_h = 10
                d_img = Image.new("L", (width_px, div_h), color=255)
                d_draw = ImageDraw.Draw(d_img)
                dy = div_h // 2
                if blk.divider_style == "dashed":
                    for dash_x in range(margin_left, width_px - margin_right, 14):
                        d_draw.line([(dash_x, dy), (min(dash_x + 8, width_px - margin_right), dy)], fill=0, width=2)
                elif blk.divider_style == "dotted":
                    for dot_x in range(margin_left, width_px - margin_right, 8):
                        d_draw.point((dot_x, dy), fill=0)
                else:  # solid
                    d_draw.line([(margin_left, dy), (width_px - margin_right, dy)], fill=0, width=2)
                sections.append(d_img)

            elif blk.type == "spacer":
                sp_h = max(4, blk.height_px)
                sections.append(Image.new("L", (width_px, sp_h), color=255))

            elif blk.type == "qr_code" and (blk.qr_data or blk.text):
                try:
                    import qrcode
                    qr = qrcode.QRCode(box_size=blk.qr_box_size or 5, border=2)
                    qr.add_data(blk.qr_data or blk.text)
                    qr.make(fit=True)
                    qr_img = qr.make_image(fill_color="black", back_color="white").convert("L")
                    qr_wrapper = Image.new("L", (width_px, qr_img.height + 16), color=255)
                    q_x = (width_px - qr_img.width) // 2
                    qr_wrapper.paste(qr_img, (q_x, 8))
                    sections.append(qr_wrapper)
                except Exception as e:
                    log(f"QR code render error: {e}")

            elif blk.type == "image" and blk.image_obj:
                sections.append(scale_and_center_image(blk.image_obj, width_px))

    elif ticket.content:
        wrapped_body = []
        for raw_line in ticket.content.splitlines():
            txt = raw_line.strip()
            if not txt:
                wrapped_body.append("")
            else:
                wrapped_body.extend(_wrap_receipt_text(txt, font_body, max_w))
        if wrapped_body:
            c_img = Image.new("L", (width_px, len(wrapped_body) * lh_body + 10), color=255)
            c_draw = ImageDraw.Draw(c_img)
            cy = 4
            for ln in wrapped_body:
                cx = _receipt_x_for_align(ln, font_body, width_px, ticket.align or "left", margin_left, margin_right)
                _draw_receipt_text(c_img, c_draw, (cx, cy), ln, font_body)
                cy += lh_body
            sections.append(c_img)

    # 6. Footer (if present)
    if ticket.footer:
        f_lines = _wrap_receipt_text(ticket.footer, font_meta, max_w)
        if f_lines:
            f_img = Image.new("L", (width_px, len(f_lines) * lh_meta + 12), color=255)
            f_draw = ImageDraw.Draw(f_img)
            fy = 6
            for ln in f_lines:
                fx = _receipt_x_for_align(ln, font_meta, width_px, "center", margin_left, margin_right)
                _draw_receipt_text(f_img, f_draw, (fx, fy), ln, font_meta)
                fy += lh_meta
            sections.append(f_img)

    # 7. Assemble all sections
    if not sections:
        return Image.new("L", (width_px, 120), color=255)

    content_height = sum(s.height for s in sections)
    top_pad = cfg.margin_top
    bottom_pad = cfg.margin_bottom
    tear_pad = 70 if separate else 0
    total_height = max(120, top_pad + content_height + bottom_pad + tear_pad)

    final_img = Image.new("L", (width_px, total_height), color=255)
    cur_y = top_pad
    for sec in sections:
        final_img.paste(sec, (0, cur_y))
        cur_y += sec.height

    return final_img


# ----------------- Backward-Compatible Multi-Element Rendering -----------------

def render_ticket_elements(
    elements: List[Dict[str, Any]],
    cfg: Optional[ReceiptCfg] = None,
    width_px: int = PRINT_WIDTH_PX,
    separate: bool = True,
) -> Image.Image:
    """
    Renders an ordered list of elements (text and images) into a continuous receipt.
    Maintains backward compatibility for legacy callers while using the enhanced
    normalization and rendering pipeline.
    """
    if cfg is None:
        cfg = ReceiptCfg()

    if not elements:
        return Image.new("L", (width_px, 120), color=255)

    # If single element, normalize and render directly
    if len(elements) == 1:
        el = elements[0]
        # Map el to NormalizedTicket
        norm = normalize_ticket(el, default_sender=el.get("sender_name"))
        return render_normalized_ticket(norm, cfg=cfg, width_px=width_px, separate=separate)

    # If multiple elements, render each and stack vertically
    rendered_blocks: List[Image.Image] = []
    for el in elements:
        try:
            norm = normalize_ticket(el, default_sender=el.get("sender_name"))
            block_img = render_normalized_ticket(norm, cfg=cfg, width_px=width_px, separate=False)
            rendered_blocks.append(block_img)
        except Exception as e:
            log(f"Error rendering element block: {e}")

    if not rendered_blocks:
        return Image.new("L", (width_px, 120), color=255)

    total_height = sum(b.height for b in rendered_blocks)
    extra_bottom = 70 if separate else 0
    ticket_img = Image.new("L", (width_px, total_height + extra_bottom), color=255)
    y_offset = 0
    for block in rendered_blocks:
        ticket_img.paste(block, (0, y_offset))
        y_offset += block.height

    return ticket_img

