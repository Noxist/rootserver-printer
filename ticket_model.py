"""
ticket_model.py — Unified Ticket Data Model & Normalization Pipeline

Ensures all print pathways (ChatGPT MCP, REST API, Web UI, Guest Links)
use the exact same schema, validation, normalization, and defaults.
"""

from __future__ import annotations
import dataclasses
from typing import Any, Dict, List, Optional


@dataclasses.dataclass
class NormalizedBlock:
    type: str  # "heading", "text", "todo", "badge", "divider", "spacer", "image", "qr_code"
    text: str = ""
    level: int = 1  # 1 (large), 2 (medium), 3 (normal)
    align: str = "left"  # "left", "center", "right"
    bold: bool = False
    font_size: str = "normal"  # "small", "normal", "large"
    badge_style: str = "inverted"  # "inverted", "border", "bold"
    divider_style: str = "solid"  # "solid", "dashed", "dotted"
    height_px: int = 20
    image_url: Optional[str] = None
    image_base64: Optional[str] = None
    image_obj: Optional[Any] = None
    qr_data: Optional[str] = None
    qr_box_size: int = 6
    checked: bool = False


@dataclasses.dataclass
class NormalizedTicket:
    title: str = ""
    subtitle: str = ""
    content: str = ""
    sender: Optional[str] = None
    show_sender: bool = False
    show_timestamp: bool = False
    timestamp_format: str = "%d.%m.%Y %H:%M"
    timestamp_timezone: str = "Europe/Zurich"
    badge: Optional[str] = None
    badge_style: str = "inverted"  # "inverted", "border", "bold"
    badge_alignment: str = "center"  # "left", "center", "right"
    align: str = "left"  # "left", "center", "right"
    font_size: str = "normal"  # "small", "normal", "large"
    bold: bool = False
    footer: Optional[str] = None
    cut: bool = True
    copies: int = 1
    image_url: Optional[str] = None
    image_base64: Optional[str] = None
    image_obj: Optional[Any] = None
    blocks: Optional[List[NormalizedBlock]] = None
    ticket_type: str = "text"  # "text", "image", "mixed"
    meta: Dict[str, Any] = dataclasses.field(default_factory=dict)


def normalize_ticket(raw: Dict[str, Any], default_sender: Optional[str] = None) -> NormalizedTicket:
    """
    Normalizes a raw ticket dictionary into a canonical NormalizedTicket.

    Enforces rules:
    1. If only 'content', use 'content'.
    2. If only 'text', use 'text'.
    3. If both provided and identical, use it once.
    4. If both provided and different, raises ValueError.
    5. If ticket has no printable content (no text, no image, no blocks), raises ValueError.
    6. show_sender defaults to False.
    7. show_timestamp defaults to False.
    8. Copies safely clamped between 1 and 5.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"Ticket must be a JSON object, got {type(raw).__name__}")

    # 1. Resolve content vs text
    has_content = "content" in raw and raw["content"] is not None
    has_text = "text" in raw and raw["text"] is not None

    if has_content and has_text:
        content_val = str(raw["content"]).strip()
        text_val = str(raw["text"]).strip()
        if content_val != text_val:
            raise ValueError(
                "Conflicting content: both 'content' and 'text' were provided with different values. "
                "Please provide only 'content'."
            )
        resolved_content = content_val
    elif has_content:
        resolved_content = str(raw["content"])
    elif has_text:
        resolved_content = str(raw["text"])
    else:
        resolved_content = ""

    # Check for image sources
    image_url = raw.get("image_url")
    if image_url and not str(image_url).strip():
        image_url = None
    image_base64 = raw.get("image_base64")
    if image_base64 and not str(image_base64).strip():
        image_base64 = None
    image_obj = raw.get("image_obj")

    # Check for structured blocks
    raw_blocks = raw.get("blocks")
    normalized_blocks: Optional[List[NormalizedBlock]] = None
    if isinstance(raw_blocks, list) and raw_blocks:
        normalized_blocks = []
        for b in raw_blocks:
            if isinstance(b, dict):
                b_type = str(b.get("type", "text")).lower()
                normalized_blocks.append(NormalizedBlock(
                    type=b_type,
                    text=str(b.get("text", "") or b.get("content", "")),
                    level=int(b.get("level", 1)),
                    align=str(b.get("align", "left")).lower(),
                    bold=bool(b.get("bold", False)),
                    font_size=str(b.get("font_size", "normal")).lower(),
                    badge_style=str(b.get("badge_style", "inverted")).lower(),
                    divider_style=str(b.get("divider_style", "solid")).lower(),
                    height_px=int(b.get("height_px", 20)),
                    image_url=b.get("image_url"),
                    image_base64=b.get("image_base64"),
                    image_obj=b.get("image_obj"),
                    qr_data=b.get("qr_data") or b.get("data"),
                    qr_box_size=int(b.get("qr_box_size", 6)),
                    checked=bool(b.get("checked", False)),
                ))

    # Plausibility check: Must have at least printable text, an image, or non-empty blocks
    has_printable_text = bool(resolved_content.strip())
    has_image = bool(image_url or image_base64 or image_obj)
    has_blocks = bool(normalized_blocks and any(b.text.strip() or b.image_url or b.image_base64 or b.qr_data for b in normalized_blocks))
    has_title = bool(str(raw.get("title", "")).strip())
    has_badge = bool(str(raw.get("badge", "")).strip())

    if not (has_printable_text or has_image or has_blocks or has_title or has_badge):
        raise ValueError("Ticket has no printable content. Provide 'content' or 'text', a title, badge, or an image.")

    # Determine ticket type
    if has_image and has_printable_text:
        ticket_type = "mixed"
    elif has_image:
        ticket_type = "image"
    else:
        ticket_type = "text"

    # Title & Subtitle
    title = str(raw.get("title", "") or "").strip()
    subtitle = str(raw.get("subtitle", "") or "").strip()

    # Badge handling
    badge_raw = raw.get("badge")
    badge = str(badge_raw).strip() if badge_raw else None
    badge_style = str(raw.get("badge_style", "inverted")).lower()
    if badge_style not in ("inverted", "border", "bold"):
        badge_style = "inverted"
    badge_alignment = str(raw.get("badge_alignment", "center")).lower()
    if badge_alignment not in ("left", "center", "right"):
        badge_alignment = "center"

    # Sender handling (Default show_sender is False)
    show_sender = bool(raw.get("show_sender", False))
    sender = raw.get("sender")
    if not sender and show_sender and default_sender:
        sender = default_sender
    if not show_sender:
        sender = None

    # Timestamp handling (Default show_timestamp is False)
    # Check show_timestamp, and legacy aliases add_datetime / add_time
    show_timestamp = bool(raw.get("show_timestamp", False))
    if not show_timestamp and "add_datetime" in raw:
        show_timestamp = bool(raw.get("add_datetime"))
    if not show_timestamp and "add_time" in raw:
        show_timestamp = bool(raw.get("add_time"))

    timestamp_format = str(raw.get("timestamp_format", "%d.%m.%Y %H:%M"))
    timestamp_timezone = str(raw.get("timestamp_timezone", "Europe/Zurich"))

    # Layout formatting
    align = str(raw.get("align", "left")).lower()
    if align not in ("left", "center", "right"):
        align = "left"

    font_size = str(raw.get("font_size", "normal")).lower()
    if font_size not in ("small", "normal", "large"):
        font_size = "normal"

    bold = bool(raw.get("bold", False))
    footer = str(raw["footer"]).strip() if raw.get("footer") else None

    # Cut & Copies
    cut = bool(raw.get("cut", True))
    try:
        copies = int(raw.get("copies", 1))
    except (ValueError, TypeError):
        copies = 1
    copies = max(1, min(copies, 5))

    meta = raw.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}

    return NormalizedTicket(
        title=title,
        subtitle=subtitle,
        content=resolved_content,
        sender=sender,
        show_sender=show_sender,
        show_timestamp=show_timestamp,
        timestamp_format=timestamp_format,
        timestamp_timezone=timestamp_timezone,
        badge=badge,
        badge_style=badge_style,
        badge_alignment=badge_alignment,
        align=align,
        font_size=font_size,
        bold=bold,
        footer=footer,
        cut=cut,
        copies=copies,
        image_url=image_url,
        image_base64=image_base64,
        image_obj=image_obj,
        blocks=normalized_blocks,
        ticket_type=ticket_type,
        meta=meta,
    )
