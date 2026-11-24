# mcp_server.py — Model Context Protocol (MCP) and ChatGPT Action Router
import json
import uuid
import time
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse

from logic import check_api_key, require_mcp_auth, ReceiptCfg, pil_to_base64_png, render_receipt, log, PRINT_WIDTH_PX
from image_processor import (
    fetch_image_from_url_safe,
    load_image_from_base64,
    scale_and_center_image,
    render_ticket_elements,
    render_normalized_ticket,
    _render_badge,
)
from ticket_model import NormalizedTicket, NormalizedBlock, normalize_ticket
from queue_print import (
    enqueue_batch,
    enqueue_base64_png,
    get_job_status,
    get_batch_status as mongo_get_batch_status,
    get_queue_status,
    wait_for_job_confirmation,
    wait_for_batch_confirmation,
)

router = APIRouter(prefix="/mcp", tags=["MCP"])


# ----------------- Shared Ticket Item Schema Definition -----------------

TICKET_ITEM_PROPERTIES = {
    "content": {
        "type": "string",
        "description": "Canonical main body text for the ticket. Multiline text and emoji are supported. For a daily plan, prefer structured blocks: a short title and subtitle, headings for sections, one text block per time range, and dividers or spacers between sections. Keep lines concise for 80mm paper."
    },
    "text": {
        "type": "string",
        "description": "Alias for 'content' (for backward compatibility). If both 'content' and 'text' are provided, they must be identical."
    },
    "title": {
        "type": "string",
        "default": "",
        "description": "Optional prominent bold main title header at top of ticket."
    },
    "subtitle": {
        "type": "string",
        "default": "",
        "description": "Optional subtitle printed directly beneath the main title."
    },
    "badge": {
        "type": "string",
        "description": "Optional badge label rendered in a high-contrast thermal box (e.g. 'IMPORTANT', 'REMINDER', 'MOTIVATION', 'URGENT', 'INFO' or custom text)."
    },
    "badge_style": {
        "type": "string",
        "enum": ["inverted", "border", "bold"],
        "default": "inverted",
        "description": "Badge rendering style: 'inverted' (solid black box with white text), 'border' (black outline box), or 'bold' (bracketed text)."
    },
    "badge_alignment": {
        "type": "string",
        "enum": ["left", "center", "right"],
        "default": "center",
        "description": "Horizontal alignment of badge."
    },
    "align": {
        "type": "string",
        "enum": ["left", "center", "right"],
        "default": "left",
        "description": "Horizontal alignment of body text: 'left', 'center', or 'right'."
    },
    "font_size": {
        "type": "string",
        "enum": ["small", "normal", "large"],
        "default": "normal",
        "description": "Font size for body text: 'small' (20px), 'normal' (28px), or 'large' (36px)."
    },
    "bold": {
        "type": "boolean",
        "default": False,
        "description": "Whether to render body text in bold."
    },
    "show_timestamp": {
        "type": "boolean",
        "default": False,
        "description": "Whether to render a visible date/time stamp on paper. Defaults to false. Internal timestamps are always recorded."
    },
    "timestamp_format": {
        "type": "string",
        "default": "%d.%m.%Y %H:%M",
        "description": "Format for visible timestamp (e.g. '%d.%m.%Y %H:%M' or '%Y-%m-%d %H:%M')."
    },
    "timestamp_timezone": {
        "type": "string",
        "default": "Europe/Zurich",
        "description": "Timezone for visible timestamp (default: 'Europe/Zurich')."
    },
    "show_sender": {
        "type": "boolean",
        "default": False,
        "description": "Whether to print a visible 'From: <sender>' line. Defaults to false."
    },
    "sender": {
        "type": "string",
        "description": "Sender name if show_sender is true."
    },
    "footer": {
        "type": "string",
        "description": "Optional footer text rendered at bottom of ticket."
    },
    "cut": {
        "type": "boolean",
        "default": True,
        "description": "Whether to advance/cut paper after this ticket."
    },
    "copies": {
        "type": "integer",
        "default": 1,
        "minimum": 1,
        "maximum": 5,
        "description": "Number of physical copies to print (1 to 5)."
    },
    "image_url": {
        "type": "string",
        "description": "Optional HTTPS URL of image to download, scale, and print on this ticket."
    },
    "image_base64": {
        "type": "string",
        "description": "Optional Base64-encoded image string to print on this ticket."
    },
    "blocks": {
        "type": "array",
        "description": "Optional ordered layout blocks. Use these to make plans and checklists readable instead of sending one long paragraph: heading blocks for sections, todo blocks for each actionable task (set checked=true only when complete), text blocks for appointments or travel notes, badge blocks for priorities, dividers/spacers for grouping, and qr_code blocks for links. When todo blocks exist, the renderer automatically adds a static 'X von Y erledigt' count based on their checked state. A QR block uses qr_data (for example an HTTPS URL); QR codes are centered automatically.",
        "items": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["heading", "text", "todo", "badge", "divider", "spacer", "image", "qr_code"]},
                "text": {"type": "string"},
                "checked": {"type": "boolean", "default": False, "description": "Whether the task is already complete. Leave false for work the user still needs to do."},
                "level": {"type": "integer", "default": 1, "description": "Heading level 1 (large), 2 (medium), 3 (normal)."},
                "align": {"type": "string", "enum": ["left", "center", "right"], "default": "left"},
                "bold": {"type": "boolean", "default": False},
                "font_size": {"type": "string", "enum": ["small", "normal", "large"], "default": "normal"},
                "badge_style": {"type": "string", "enum": ["inverted", "border", "bold"], "default": "inverted"},
                "divider_style": {"type": "string", "enum": ["solid", "dashed", "dotted"], "default": "solid"},
                "height_px": {"type": "integer", "default": 20},
                "image_url": {"type": "string"},
                "image_base64": {"type": "string"},
                "qr_data": {"type": "string", "description": "Text or link encoded in this QR code; for links use a complete HTTPS URL."},
                "qr_box_size": {"type": "integer", "minimum": 2, "maximum": 10, "default": 6, "description": "QR module size in pixels. Use 4 to 6 for a compact code and 6 to 8 for easier scanning."}
            },
            "required": ["type"]
        }
    }
}


# ----------------- MCP Tool Definitions -----------------

MCP_TOOLS = [
    {
        "name": "print_text",
        "description": (
            "Prints a single formatted ticket on the 80mm thermal receipt printer. Emoji are rendered as monochrome artwork. "
            "Supports custom title, subtitle, content, badges (e.g. MOTIVATION, IMPORTANT), alignment, "
            "font sizing, bold text, optional timestamp, optional sender, structured headings, to-do checkboxes, dividers, spacers, QR codes, and synchronous confirmation waiting. "
            "For a request like 'print my daily plan', format it with a clear title/date, section headings, one concise todo block per actionable task, text blocks for appointments or travel, and a short footer; use a QR block when a useful link is provided. Leave todo boxes unchecked unless completion is known. The renderer prints the checklist completion count automatically."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **TICKET_ITEM_PROPERTIES,
                "wait_for_result": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, waits synchronously up to timeout_seconds for printer firmware confirmation before returning."
                },
                "timeout_seconds": {
                    "type": "integer",
                    "default": 15,
                    "minimum": 2,
                    "maximum": 35,
                    "description": "Maximum seconds to wait for firmware ACK if wait_for_result is true."
                }
            }
        }
    },
    {
        "name": "print_tickets",
        "description": (
            "Prints a batch of sequential tickets (1 to 20) in a single request. "
            "Guarantees execution order and physical ticket separation. Each ticket can have its own "
            "title, subtitle, content, badge, formatting, and images."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tickets": {
                    "type": "array",
                    "description": "Ordered array of tickets to print. Each ticket supports all layout options (content, text, title, subtitle, badge, images, etc.).",
                    "items": {
                        "type": "object",
                        "properties": TICKET_ITEM_PROPERTIES
                    }
                },
                "separate": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to physically separate tickets with feed spacing past the manual tear-off bar."
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Optional unique key to prevent duplicate prints on retries."
                },
                "wait_for_result": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, waits synchronously for all tickets in batch to be processed and confirmed."
                },
                "timeout_seconds": {
                    "type": "integer",
                    "default": 25,
                    "minimum": 3,
                    "maximum": 45,
                    "description": "Maximum seconds to wait for batch confirmation."
                }
            },
            "required": ["tickets"]
        }
    },
    {
        "name": "preview_ticket",
        "description": (
            "Renders a ticket into a base64-encoded monochrome PNG preview using the exact same rendering "
            "pipeline as physical printing, WITHOUT enqueuing or printing anything. Ideal for inspecting badges, "
            "subtitles, emoji, QR codes, and layouts beforehand. For long daily plans or complex structured tickets, preview first and then print the same layout."
        ),
        "inputSchema": {
            "type": "object",
            "properties": TICKET_ITEM_PROPERTIES
        }
    },
    {
        "name": "preview_batch",
        "description": (
            "Renders a batch of tickets into preview images using the exact same rendering pipeline, "
            "without sending any print jobs to the printer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tickets": {
                    "type": "array",
                    "description": "Ordered array of tickets to preview.",
                    "items": {
                        "type": "object",
                        "properties": TICKET_ITEM_PROPERTIES
                    }
                },
                "separate": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to include tear-off feed spacing in preview height."
                }
            },
            "required": ["tickets"]
        }
    },
    {
        "name": "print_image",
        "description": "Prints an image on the thermal printer from an HTTPS URL or Base64 string, with optional title, subtitle, badge, and content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "image_url": {"type": "string", "description": "HTTPS URL of image to download and print."},
                "image_base64": {"type": "string", "description": "Base64 encoded image data."},
                "title": {"type": "string", "default": "", "description": "Optional header title."},
                "subtitle": {"type": "string", "default": "", "description": "Optional subtitle."},
                "content": {"type": "string", "description": "Optional accompanying text."},
                "badge": {"type": "string", "description": "Optional badge label."},
                "badge_style": {"type": "string", "enum": ["inverted", "border", "bold"], "default": "inverted"},
                "badge_alignment": {"type": "string", "enum": ["left", "center", "right"], "default": "center"},
                "show_timestamp": {"type": "boolean", "default": False},
                "show_sender": {"type": "boolean", "default": False},
                "sender": {"type": "string"},
                "cut": {"type": "boolean", "default": True},
                "wait_for_result": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "integer", "default": 15}
            }
        }
    },
    {
        "name": "get_printer_status",
        "description": "Checks whether the thermal printer is online, queue backlog size, archive count, and broker connectivity.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "get_print_job_status",
        "description": "Retrieves the detailed status of an individual print job by job ID or idempotency key.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "The unique job ID to inspect."}
            },
            "required": ["job_id"]
        }
    },
    {
        "name": "get_batch_status",
        "description": "Retrieves execution and queue progress of a batch of tickets, including confirmed, published, and unconfirmed counts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "batch_id": {"type": "string", "description": "The batch ID returned by print_tickets."}
            },
            "required": ["batch_id"]
        }
    },
    {
        "name": "get_printer_capabilities",
        "description": "Returns hardware and protocol capabilities of the thermal printer system.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    }
]

# ----------------- Execution Helpers -----------------

async def execute_mcp_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    cfg = ReceiptCfg()

    if name == "get_printer_capabilities":
        return {
            "model": "HS-C830ULWB-2199 (80mm Cloud Thermal Receipt Printer)",
            "paper_width_mm": 80,
            "resolution_dpi": 203,
            "print_width_px": PRINT_WIDTH_PX,
            "supported_image_types": ["PNG", "JPEG", "WebP", "GIF", "BMP", "Base64", "HTTPS URLs"],
            "dithering_algorithms": ["floyd", "bayer", "threshold"],
            "cutter_support": "Manual tear-off bar confirmed; cut_paper signal forwarded to firmware",
            "protocol": "MQTT JSON-wrapped monochrome Base64 PNG (No ESC/POS)",
            "queue_store": "MongoDB persistent database 'printer'",
            "batch_support": True,
            "max_tickets_per_batch": 20,
            "supported_badges": ["IMPORTANT", "REMINDER", "MOTIVATION", "URGENT", "INFORMATION", "custom text"],
            "badge_styles": ["inverted", "border", "bold"],
            "alignment_options": ["left", "center", "right"],
            "font_sizes": ["small", "normal", "large"],
            "preview_tools": ["preview_ticket", "preview_batch"],
            "status_states": ["queued", "rendering", "published", "printed", "unconfirmed", "failed"]
        }

    elif name == "get_printer_status":
        return get_queue_status()

    elif name == "get_print_job_status":
        jid = args.get("job_id", "")
        res = get_job_status(jid)
        return res if res else {"error": f"Job '{jid}' not found"}

    elif name == "get_batch_status":
        bid = args.get("batch_id", "")
        res = mongo_get_batch_status(bid)
        return res if res else {"error": f"Batch '{bid}' not found"}

    elif name == "preview_ticket":
        # Load image if specified
        loaded_img = None
        if args.get("image_url"):
            loaded_img = await fetch_image_from_url_safe(args["image_url"])
        elif args.get("image_base64"):
            loaded_img = load_image_from_base64(args["image_base64"])

        norm = normalize_ticket(args)
        if loaded_img:
            norm.image_obj = loaded_img

        img = render_normalized_ticket(norm, cfg=cfg, width_px=PRINT_WIDTH_PX, separate=norm.cut)
        b64 = pil_to_base64_png(img)
        return {
            "preview": True,
            "width_px": img.width,
            "height_px": img.height,
            "preview_base64_png": b64,
            "title": norm.title,
            "subtitle": norm.subtitle,
            "badge": norm.badge,
            "badge_style": norm.badge_style,
            "show_timestamp": norm.show_timestamp,
            "show_sender": norm.show_sender,
            "message": "Preview generated successfully without enqueuing."
        }

    elif name == "preview_batch":
        raw_tickets = args.get("tickets", [])
        if not raw_tickets:
            raise ValueError("Tickets array cannot be empty.")
        if len(raw_tickets) > 20:
            raise ValueError(f"Batch size ({len(raw_tickets)}) exceeds limit of 20 tickets per request.")

        separate = bool(args.get("separate", True))
        previews = []
        for idx, t in enumerate(raw_tickets):
            loaded_img = None
            if t.get("image_url"):
                loaded_img = await fetch_image_from_url_safe(t["image_url"])
            elif t.get("image_base64"):
                loaded_img = load_image_from_base64(t["image_base64"])

            norm = normalize_ticket(t)
            if loaded_img:
                norm.image_obj = loaded_img

            img = render_normalized_ticket(norm, cfg=cfg, width_px=PRINT_WIDTH_PX, separate=separate)
            b64 = pil_to_base64_png(img)
            previews.append({
                "ticket_index": idx,
                "width_px": img.width,
                "height_px": img.height,
                "title": norm.title,
                "subtitle": norm.subtitle,
                "badge": norm.badge,
                "preview_base64_png": b64
            })

        return {
            "preview": True,
            "total_tickets": len(previews),
            "previews": previews,
            "message": f"Generated previews for {len(previews)} tickets successfully."
        }

    elif name == "print_text":
        norm = normalize_ticket(args)
        img = render_normalized_ticket(norm, cfg=cfg, width_px=PRINT_WIDTH_PX, separate=norm.cut)
        b64 = pil_to_base64_png(img)

        # Enqueue job(s) for the requested copies
        jid = None
        for copy_idx in range(norm.copies):
            copy_meta = {"source": "chatgpt_text", "copy": copy_idx + 1, "total_copies": norm.copies}
            jid = enqueue_base64_png(b64, cut_paper=1 if norm.cut else 0, meta=copy_meta)

        if bool(args.get("wait_for_result", False)):
            wait_sec = float(args.get("timeout_seconds", 15))
            st = wait_for_job_confirmation(jid, timeout_seconds=wait_sec)
            status_val = st.get("status", "unconfirmed")
            return {
                "job_id": jid,
                "status": status_val,
                "rendered": True,
                "mqtt_published": status_val in ("published", "printed", "unconfirmed"),
                "printer_confirmation": st.get("printer_confirmation", False),
                "detail": st.get("detail", ""),
                "copies": norm.copies,
                "message": (
                    "Print confirmed by printer firmware." if status_val == "printed"
                    else "Ticket dispatched via MQTT to printer." if status_val == "published"
                    else "Ticket published to MQTT, awaiting printer acknowledgement."
                )
            }

        return {
            "job_id": jid,
            "status": "queued",
            "rendered": True,
            "mqtt_published": False,
            "printer_confirmation": False,
            "copies": norm.copies,
            "message": "Text ticket enqueued successfully in MongoDB."
        }

    elif name == "print_image":
        img_url = args.get("image_url")
        img_b64 = args.get("image_base64")
        if img_url:
            img = await fetch_image_from_url_safe(img_url)
        elif img_b64:
            img = load_image_from_base64(img_b64)
        else:
            raise ValueError("Either image_url or image_base64 must be provided.")

        norm = normalize_ticket(args)
        norm.image_obj = img
        final_img = render_normalized_ticket(norm, cfg=cfg, width_px=PRINT_WIDTH_PX, separate=norm.cut)
        b64 = pil_to_base64_png(final_img)
        jid = enqueue_base64_png(b64, cut_paper=1 if norm.cut else 0, meta={"source": "chatgpt_image"})

        if bool(args.get("wait_for_result", False)):
            wait_sec = float(args.get("timeout_seconds", 15))
            st = wait_for_job_confirmation(jid, timeout_seconds=wait_sec)
            status_val = st.get("status", "unconfirmed")
            return {
                "job_id": jid,
                "status": status_val,
                "rendered": True,
                "mqtt_published": status_val in ("published", "printed", "unconfirmed"),
                "printer_confirmation": st.get("printer_confirmation", False),
                "detail": st.get("detail", "")
            }

        return {"job_id": jid, "status": "queued", "rendered": True, "message": "Image ticket enqueued successfully."}

    elif name == "print_tickets":
        raw_tickets = args.get("tickets", [])
        if not raw_tickets:
            raise ValueError("Tickets array cannot be empty.")
        if len(raw_tickets) > 20:
            raise ValueError(f"Batch size ({len(raw_tickets)}) exceeds limit of 20 tickets per request.")

        separate = bool(args.get("separate", True))
        idempotency_key = args.get("idempotency_key")

        prepared_tickets = []
        for i, t in enumerate(raw_tickets):
            loaded_img = None
            if t.get("image_url"):
                loaded_img = await fetch_image_from_url_safe(t["image_url"])
            elif t.get("image_base64"):
                loaded_img = load_image_from_base64(t["image_base64"])

            norm = normalize_ticket(t)
            if loaded_img:
                norm.image_obj = loaded_img

            ticket_img = render_normalized_ticket(norm, cfg=cfg, width_px=PRINT_WIDTH_PX, separate=separate and norm.cut)
            b64_str = pil_to_base64_png(ticket_img)
            prepared_tickets.append({
                "b64": b64_str,
                "cut": 1 if (separate and norm.cut) else 0,
                "meta": {
                    "ticket_index": i,
                    "source": "chatgpt_batch",
                    "title": norm.title,
                    "has_badge": bool(norm.badge)
                }
            })

        batch_result = enqueue_batch(
            tickets_data=prepared_tickets,
            separate=separate,
            idempotency_key=idempotency_key,
            meta={"source": "chatgpt_batch"}
        )

        if bool(args.get("wait_for_result", False)):
            wait_sec = float(args.get("timeout_seconds", 25))
            bst = wait_for_batch_confirmation(batch_result["batch_id"], timeout_seconds=wait_sec)
            return {
                **batch_result,
                **bst,
                "rendered": True,
                "message": (
                    "All tickets confirmed printed by printer firmware." if bst.get("status") == "printed"
                    else f"Batch processing: {bst.get('confirmed_tickets', 0)}/{len(prepared_tickets)} tickets confirmed."
                )
            }

        return {
            **batch_result,
            "rendered": True,
            "printer_confirmation": False,
            "message": f"Batch of {len(prepared_tickets)} tickets enqueued successfully in MongoDB."
        }

    else:
        raise ValueError(f"Unknown MCP tool: {name}")


# ----------------- MCP JSON-RPC Handler -----------------

@router.post("")
@router.post("/")
async def mcp_jsonrpc_endpoint(request: Request):
    """Handles MCP protocol requests via JSON-RPC 2.0."""
    if not require_mcp_auth(request):
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32000, "message": "Unauthorized: Valid Cloudflare Access assertion or API key required"}},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="OAuth", error="invalid_token", error_description="Missing or invalid access token", resource_metadata="https://printer.nox007.tech/.well-known/cloudflare-access-protected-resource/mcp"'}
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None})

    method = body.get("method")
    req_id = body.get("id")
    params = body.get("params", {})

    if method == "initialize":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": False}
                },
                "serverInfo": {
                    "name": "thermal-printer-mcp",
                    "version": "2.0.0"
                }
            }
        })

    elif method == "tools/list":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": MCP_TOOLS
            }
        })

    elif method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        try:
            result_data = await execute_mcp_tool(tool_name, arguments)
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result_data, indent=2, ensure_ascii=False)
                        }
                    ],
                    "isError": False
                }
            })
        except Exception as exc:
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error executing {tool_name}: {exc}"}],
                    "isError": True
                }
            })

    elif method == "ping":
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {}})

    else:
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}
        })


# ----------------- Official Python MCP SDK MCPServer -----------------
try:
    from mcp.server import MCPServer
    from mcp.server.transport_security import TransportSecuritySettings

    mcp_app_server = MCPServer("Thermodrucker", version="2.0.0")

    @mcp_app_server.tool()
    async def print_tickets(
        tickets: List[Dict[str, Any]],
        separate: bool = True,
        idempotency_key: Optional[str] = None,
        wait_for_result: bool = False,
        timeout_seconds: int = 25,
    ) -> str:
        """Prints multiple tickets in a single batch request (1 to 20 tickets). Supports text, content, badges, formatting, and images with guaranteed order and physical ticket separation."""
        res = await execute_mcp_tool("print_tickets", {
            "tickets": tickets,
            "separate": separate,
            "idempotency_key": idempotency_key,
            "wait_for_result": wait_for_result,
            "timeout_seconds": timeout_seconds,
        })
        return json.dumps(res, ensure_ascii=False)

    @mcp_app_server.tool()
    async def print_text(
        content: Optional[str] = None,
        text: Optional[str] = None,
        title: str = "",
        subtitle: str = "",
        badge: Optional[str] = None,
        badge_style: str = "inverted",
        badge_alignment: str = "center",
        align: str = "left",
        bold: bool = False,
        font_size: str = "normal",
        show_timestamp: bool = False,
        timestamp_format: Optional[str] = None,
        timestamp_timezone: str = "Europe/Zurich",
        show_sender: bool = False,
        sender: Optional[str] = None,
        footer: Optional[str] = None,
        blocks: Optional[List[Dict[str, Any]]] = None,
        cut: bool = True,
        copies: int = 1,
        wait_for_result: bool = False,
        timeout_seconds: int = 15,
    ) -> str:
        """Prints a single text ticket on the thermal receipt printer with rich layout options, badges, custom fonts, and optional synchronous wait."""
        args = {
            "content": content, "text": text, "title": title, "subtitle": subtitle,
            "badge": badge, "badge_style": badge_style, "badge_alignment": badge_alignment,
            "align": align, "bold": bold, "font_size": font_size,
            "show_timestamp": show_timestamp, "timestamp_format": timestamp_format,
            "timestamp_timezone": timestamp_timezone, "show_sender": show_sender,
            "sender": sender, "footer": footer, "blocks": blocks, "cut": cut,
            "copies": copies, "wait_for_result": wait_for_result, "timeout_seconds": timeout_seconds,
        }
        res = await execute_mcp_tool("print_text", args)
        return json.dumps(res, ensure_ascii=False)

    @mcp_app_server.tool()
    async def preview_ticket(
        content: Optional[str] = None,
        text: Optional[str] = None,
        title: str = "",
        subtitle: str = "",
        badge: Optional[str] = None,
        badge_style: str = "inverted",
        badge_alignment: str = "center",
        align: str = "left",
        bold: bool = False,
        font_size: str = "normal",
        show_timestamp: bool = False,
        timestamp_format: Optional[str] = None,
        timestamp_timezone: str = "Europe/Zurich",
        show_sender: bool = False,
        sender: Optional[str] = None,
        footer: Optional[str] = None,
        blocks: Optional[List[Dict[str, Any]]] = None,
        cut: bool = True,
        copies: int = 1,
        image_url: Optional[str] = None,
        image_base64: Optional[str] = None,
    ) -> str:
        """Renders a single ticket into a base64-encoded monochrome PNG preview without queuing or printing anything. Ideal for inspecting badges, subtitles, and layouts beforehand."""
        args = {
            "content": content, "text": text, "title": title, "subtitle": subtitle,
            "badge": badge, "badge_style": badge_style, "badge_alignment": badge_alignment,
            "align": align, "bold": bold, "font_size": font_size,
            "show_timestamp": show_timestamp, "timestamp_format": timestamp_format,
            "timestamp_timezone": timestamp_timezone, "show_sender": show_sender,
            "sender": sender, "footer": footer, "blocks": blocks, "cut": cut,
            "copies": copies, "image_url": image_url, "image_base64": image_base64,
        }
        res = await execute_mcp_tool("preview_ticket", args)
        return json.dumps(res, ensure_ascii=False)

    @mcp_app_server.tool()
    async def preview_batch(
        tickets: List[Dict[str, Any]],
        separate: bool = True,
    ) -> str:
        """Renders a batch of tickets into preview images without sending any print jobs to the printer."""
        res = await execute_mcp_tool("preview_batch", {"tickets": tickets, "separate": separate})
        return json.dumps(res, ensure_ascii=False)

    @mcp_app_server.tool()
    async def print_image(
        image_url: Optional[str] = None,
        image_base64: Optional[str] = None,
        title: str = "",
        subtitle: str = "",
        content: Optional[str] = None,
        badge: Optional[str] = None,
        badge_style: str = "inverted",
        badge_alignment: str = "center",
        show_timestamp: bool = False,
        show_sender: bool = False,
        sender: Optional[str] = None,
        cut: bool = True,
        wait_for_result: bool = False,
        timeout_seconds: int = 15,
    ) -> str:
        """Prints an image on the thermal printer from an HTTPS URL or Base64 string with optional title, subtitle, badge, and content."""
        args = {
            "image_url": image_url, "image_base64": image_base64, "title": title,
            "subtitle": subtitle, "content": content, "badge": badge,
            "badge_style": badge_style, "badge_alignment": badge_alignment,
            "show_timestamp": show_timestamp, "show_sender": show_sender,
            "sender": sender, "cut": cut, "wait_for_result": wait_for_result,
            "timeout_seconds": timeout_seconds,
        }
        res = await execute_mcp_tool("print_image", args)
        return json.dumps(res, ensure_ascii=False)

    @mcp_app_server.tool()
    async def get_printer_status() -> str:
        """Checks whether the thermal printer is online, queue backlog size, and broker connectivity."""
        res = await execute_mcp_tool("get_printer_status", {})
        return json.dumps(res, ensure_ascii=False)

    @mcp_app_server.tool()
    async def get_print_job_status(job_id: str) -> str:
        """Retrieves the status of an individual print job by job ID."""
        res = await execute_mcp_tool("get_print_job_status", {"job_id": job_id})
        return json.dumps(res, ensure_ascii=False)

    @mcp_app_server.tool()
    async def get_batch_status(batch_id: str) -> str:
        """Retrieves the execution and queue progress of a batch of tickets."""
        res = await execute_mcp_tool("get_batch_status", {"batch_id": batch_id})
        return json.dumps(res, ensure_ascii=False)

    @mcp_app_server.tool()
    async def get_printer_capabilities() -> str:
        """Returns hardware and protocol capabilities of the thermal printer system."""
        res = await execute_mcp_tool("get_printer_capabilities", {})
        return json.dumps(res, ensure_ascii=False)

    mcp_streamable_app = mcp_app_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
except ImportError:
    mcp_app_server = None
    mcp_streamable_app = None

