# main.py — Thermal Printer Service with MongoDB Queue, Batch Print, SSRF-safe Images, and ChatGPT MCP
import os
import io
import time
import json
import threading
import ipaddress
from collections import deque
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any, Union
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Depends
from fastapi.responses import (
    HTMLResponse, RedirectResponse, PlainTextResponse, 
    FileResponse, JSONResponse
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image, UnidentifiedImageError

# --- Local Modules ---
from queue_print import (
    start_background_flusher, stop_background_flusher, 
    enqueue_base64_png, enqueue_batch, migrate_files_to_mongo,
    get_queue_list, get_queue_status, clear_queue as mongo_clear_queue,
    reset_queue as mongo_reset_queue, get_job_status, get_batch_status,
    get_history
)
from image_processor import (
    fetch_image_from_url_safe,
    load_image_from_base64,
    scale_and_center_image,
    render_ticket_elements,
    render_normalized_ticket,
    _render_badge,
)
from ticket_model import NormalizedTicket, normalize_ticket
import hmac
from csrf import (
    CSRF_COOKIE_NAME, generate_csrf_token, validate_csrf_token,
    verify_csrf, extract_submitted_csrf
)
from mcp_server import router as mcp_router, execute_mcp_tool, mcp_streamable_app
from routes_sources import router as sources_router
from logic import (
    log, now_str, pil_to_base64_png, render_receipt, render_image_with_headers,
    ReceiptCfg, check_api_key, require_ui_auth, require_mcp_auth,
    cfg_get, SETTINGS, SET_KEYS, _save_settings, _reload_settings_if_changed,
    GUESTS, guest_consume_or_error, _guest_check_len_ok, GUEST_MAX_CHARS,
    printer_status, PRINT_WIDTH_PX, APP_API_KEY, CHATGPT_API_KEY
)
from ui_html import html_page, HTML_UI, settings_html_form, guest_ui_html
from image_archive import (
    save_original_image, list_archived_images, get_image_file_path,
    delete_archived_image, MAX_ARCHIVED_IMAGES, list_recovered_images
)

CSRF_SECRET = APP_API_KEY or "thermal-printer-csrf-secret"

def _get_or_create_csrf(request: Request) -> str:
    tok = request.cookies.get(CSRF_COOKIE_NAME, "")
    if tok and validate_csrf_token(tok, CSRF_SECRET):
        return tok
    return generate_csrf_token(CSRF_SECRET)

def _is_secure_request(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"


# --- Lifecycle & Startup ------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    log("[main] Starting Thermal Printer Service …")
    
    # 1. Migrate legacy queue files to MongoDB if any exist
    try:
        migrate_files_to_mongo()
    except Exception as e:
        log(f"[main] Migration warning: {e}")

    # 2. Start persistent background flusher
    try:
        stop_background_flusher() 
    except Exception: 
        pass
    
    start_background_flusher()
    log("[main] Print queue background flusher active.")
    
    if mcp_streamable_app is not None:
        async with mcp_streamable_app.router.lifespan_context(mcp_streamable_app):
            yield
    else:
        yield
    
    stop_background_flusher()
    log("[main] Printer app shutdown complete.")

app = FastAPI(
    title="Thermal Printer API",
    description="Secure thermal receipt printer API with persistent MongoDB queue, batch printing, and ChatGPT MCP integration.",
    version="2.0.0",
    lifespan=lifespan
)

# Rate Limiter Configuration
_RATE_WINDOW_SECONDS = 60
_RATE_BUCKET_LIMIT = 8192
_rate_buckets: dict[tuple[str, str], deque[float]] = {}
_rate_lock = threading.Lock()

RATE_LIMITS = {
    "exempt": None,
    "status_read": 1200,      # 1200 / min = 20 req/s (status polling, queue counts, SSE)
    "ui_page": 300,           # 300 / min = 5 page reloads per second
    "write_print": 60,        # 60 / min = 1 print per second
    "admin_action": 30,       # 30 / min = admin settings/token operations
}

def _get_rate_tier(request: Request) -> str:
    path = request.url.path
    method = request.method

    # 1. Exempt: static assets, favicon, health check
    if path in {"/favicon.ico", "/_health", "/robots.txt"} or path.startswith("/static/"):
        return "exempt"

    # 2. Status & monitoring reads
    if method in {"GET", "HEAD"}:
        if path in {
            "/ui/status", "/ui/live-status", "/ui/events",
            "/live-status", "/events", "/api/live-status", "/api/events",
            "/queue/status", "/queue/list", "/status",
            "/mcp/capabilities", "/.well-known/ai-plugin.json",
            "/.well-known/mcp.json", "/chatgpt-action.json"
        }:

            return "status_read"
        if path.startswith(("/ui", "/guest")):
            return "ui_page"
        return "status_read"

    # 3. State-changing operations
    if path.startswith(("/ui/guests", "/queue/clear", "/queue/reset", "/ui/settings")):
        return "admin_action"

    if path.startswith(("/print", "/api/print", "/guest/", "/ui/print", "/mcp")):
        return "write_print"

    return "write_print"

_INTERNAL_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("192.168.0.0/16"),
]

def _rate_identity(request: Request) -> str:
    # 1. CF-Connecting-IP
    cf_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_ip:
        try:
            return str(ipaddress.ip_address(cf_ip))
        except ValueError:
            pass

    # 2. X-Forwarded-For: find leftmost non-internal IP
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        for p in parts:
            try:
                ip = ipaddress.ip_address(p)
                if not any(ip in net for net in _INTERNAL_NETS):
                    return str(ip)
            except ValueError:
                continue
        if parts:
            try:
                return str(ipaddress.ip_address(parts[0]))
            except ValueError:
                pass

    # 3. Request client host
    raw_host = request.client.host if request.client else ""
    if raw_host:
        try:
            ip = ipaddress.ip_address(raw_host)
            # If client host is an internal proxy (like Caddy), check if we have a cookie discriminator
            if any(ip in net for net in _INTERNAL_NETS):
                csrf_cookie = request.cookies.get("printer_csrf", "")
                if csrf_cookie:
                    return f"sess_{csrf_cookie[:16]}"
            return str(ip)
        except ValueError:
            pass

    csrf_cookie = request.cookies.get("printer_csrf", "")
    if csrf_cookie:
        return f"sess_{csrf_cookie[:16]}"

    return "unknown"

def _rate_limited(request: Request) -> tuple[bool, int, int, int]:
    tier = _get_rate_tier(request)
    limit = RATE_LIMITS.get(tier)
    if limit is None:
        return False, 0, 0, 0

    identity = _rate_identity(request)
    key = (identity, tier)
    now = time.monotonic()
    cutoff = now - _RATE_WINDOW_SECONDS

    with _rate_lock:
        if key not in _rate_buckets and len(_rate_buckets) >= _RATE_BUCKET_LIMIT:
            for old_key in list(_rate_buckets):
                old_bucket = _rate_buckets[old_key]
                while old_bucket and old_bucket[0] <= cutoff:
                    old_bucket.popleft()
                if not old_bucket:
                    _rate_buckets.pop(old_key, None)
            if len(_rate_buckets) >= _RATE_BUCKET_LIMIT:
                key = ("overflow", tier)
        bucket = _rate_buckets.setdefault(key, deque())
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        current_count = len(bucket)
        if current_count >= limit:
            oldest = bucket[0] if bucket else cutoff
            retry_after = max(1, int(oldest + _RATE_WINDOW_SECONDS - now))
            return True, retry_after, limit, 0

        bucket.append(now)
        remaining = max(0, limit - (current_count + 1))
        return False, 0, limit, remaining

ALLOWED_UI_HOSTS = {
    "printer.nox007.tech",
    "nox007.tech",
    "localhost",
    "127.0.0.1",
    "printer-api",
}

@app.middleware("http")
async def browser_security(request: Request, call_next):
    is_limited, retry_after, r_limit, r_remaining = _rate_limited(request)
    if is_limited:
        return PlainTextResponse(
            "rate limit exceeded",
            status_code=429,
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(r_limit),
                "X-RateLimit-Remaining": "0",
            },
        )

    # 1. Scoped Authentication for /mcp endpoint (Cloudflare Access Managed OAuth & API Keys)
    if request.url.path in {"/mcp", "/mcp/"}:
        if request.method != "OPTIONS":
            if not require_mcp_auth(request):
                return JSONResponse(
                    {"jsonrpc": "2.0", "error": {"code": -32000, "message": "Unauthorized: Valid Cloudflare Access assertion or API key required"}},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="OAuth", error="invalid_token", error_description="Missing or invalid access token", resource_metadata="https://printer.nox007.tech/.well-known/cloudflare-access-protected-resource/mcp"'}
                )

    # 1.1 Strict Authentication for All Image Endpoints (Cloudflare Access or API Key)
    if request.url.path.startswith(("/images/", "/ui/images/")):
        if not require_ui_auth(request):
            if request.url.path.startswith("/ui/"):
                return _cloudflare_access_required()
            else:
                return RedirectResponse(f"/ui{request.url.path}", status_code=307)

    # 2. Browser State-Changing Request Security & CSRF Protection
    is_state_changing = request.method not in {"GET", "HEAD", "OPTIONS"}
    is_protected_path = request.url.path.startswith(("/ui/", "/guest/", "/queue/clear", "/queue/reset"))

    if is_state_changing and is_protected_path:
        # Check if caller is an authorized machine API client
        has_api_key = False
        api_candidate = request.headers.get("x-api-key", "").strip()
        if not api_candidate:
            auth_h = request.headers.get("authorization", "").strip()
            if auth_h.lower().startswith("bearer "):
                api_candidate = auth_h[7:].strip()
        if api_candidate and ((APP_API_KEY and hmac.compare_digest(api_candidate, APP_API_KEY)) or
                              (CHATGPT_API_KEY and hmac.compare_digest(api_candidate, CHATGPT_API_KEY))):
            has_api_key = True

        if not has_api_key:
            raw_host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
            req_host = raw_host.split(",")[0].strip().split(":")[0].casefold()

            origin = request.headers.get("origin", "").strip()
            referer = request.headers.get("referer", "").strip()
            sec_fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()

            # Always reject explicit cross-site submissions
            if sec_fetch_site == "cross-site":
                return PlainTextResponse("cross-site request rejected", status_code=403)

            # Case A: Origin is present and NOT literal "null"
            if origin and origin != "null":
                origin_host = urlsplit(origin).netloc.split(":")[0].casefold()
                if not origin_host or (origin_host not in ALLOWED_UI_HOSTS and origin_host != req_host):
                    return PlainTextResponse("cross-site request rejected", status_code=403)
                if referer:
                    referer_host = urlsplit(referer).netloc.split(":")[0].casefold()
                    if not referer_host or (referer_host not in ALLOWED_UI_HOSTS and referer_host != req_host):
                        return PlainTextResponse("cross-site request rejected", status_code=403)
                if not verify_csrf(request, CSRF_SECRET):
                    return PlainTextResponse("csrf token invalid or missing", status_code=403)

            # Case B: Origin is "null" or omitted (e.g. privacy mode, mobile form navigation)
            else:
                if sec_fetch_site not in {"same-origin", "none", ""}:
                    return PlainTextResponse("cross-site request rejected", status_code=403)
                if referer:
                    referer_host = urlsplit(referer).netloc.split(":")[0].casefold()
                    if not referer_host or (referer_host not in ALLOWED_UI_HOSTS and referer_host != req_host):
                        return PlainTextResponse("cross-site request rejected", status_code=403)

                # Validate authenticated session
                if request.url.path.startswith("/guest/"):
                    parts = request.url.path.split("/")
                    guest_tok = parts[2] if len(parts) > 2 else ""
                    if not GUESTS.validate(guest_tok):
                        return PlainTextResponse("guest session invalid", status_code=403)
                else:
                    if not require_ui_auth(request):
                        return PlainTextResponse("authentication required", status_code=401)

                # Validate cryptographically signed CSRF token
                if not verify_csrf(request, CSRF_SECRET):
                    return PlainTextResponse("csrf token invalid or missing", status_code=403)

    response = await call_next(request)
    if r_limit > 0:
        response.headers["X-RateLimit-Limit"] = str(r_limit)
        response.headers["X-RateLimit-Remaining"] = str(r_remaining)
        response.headers["X-RateLimit-Category"] = _get_rate_tier(request)

    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; img-src 'self' data:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; object-src 'none'")
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response

# Routers
from live_status import live_router

app.include_router(live_router)
app.include_router(sources_router)
if mcp_streamable_app is not None:
    for route in mcp_streamable_app.routes:
        app.routes.append(route)
else:
    app.include_router(mcp_router)



# Static files
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

MAX_IMAGE_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000

async def _read_safe_image(file: UploadFile) -> Image.Image:
    content = bytearray()
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        if len(content) + len(chunk) > MAX_IMAGE_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="image upload is too large")
        content.extend(chunk)
    if not content:
        raise HTTPException(status_code=400, detail="empty image upload")

    try:
        raw_bytes = bytes(content)
        image = Image.open(io.BytesIO(raw_bytes))
        width, height = image.size
        if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
            raise HTTPException(status_code=413, detail="image dimensions are too large")
        image.load()
        image._raw_bytes = raw_bytes
        image._orig_filename = getattr(file, "filename", None) or "upload.png"
        return image
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=415, detail="invalid image upload") from exc

def _print_text_content(title: str, lines: list[str], add_dt: bool, 
                        cut: bool, source: str, sender: str = None, subtitle: str = "") -> str:
    cfg = ReceiptCfg()
    norm = normalize_ticket({
        "title": title,
        "subtitle": subtitle,
        "content": "\n".join(lines),
        "show_timestamp": add_dt,
        "show_sender": bool(sender),
        "sender": sender,
        "cut": cut
    })
    img = render_normalized_ticket(norm, cfg=cfg, width_px=PRINT_WIDTH_PX, separate=cut)
    b64 = pil_to_base64_png(img)
    return enqueue_base64_png(b64, cut_paper=(1 if cut else 0), meta={"source": source})

def _print_image_content(img_obj: Image.Image, title: str, subtitle: str, 
                         cut: bool, source: str, sender: str = None) -> str:
    raw = getattr(img_obj, "_raw_bytes", None)
    fname = getattr(img_obj, "_orig_filename", None) or title or "image.png"
    try:
        save_original_image(
            raw if raw else img_obj,
            filename_hint=fname,
            source=source,
            sender=sender
        )
    except Exception as e:
        print(f"[archive] Warning: Failed to save original image: {e}", flush=True)

    cfg = ReceiptCfg()
    norm = normalize_ticket({
        "title": title or "",
        "subtitle": subtitle or "",
        "image_obj": img_obj,
        "show_sender": bool(sender),
        "sender": sender,
        "cut": cut
    })
    norm.image_obj = img_obj
    img = render_normalized_ticket(norm, cfg=cfg, width_px=PRINT_WIDTH_PX, separate=cut)
    b64 = pil_to_base64_png(img)
    return enqueue_base64_png(b64, cut_paper=(1 if cut else 0), meta={"source": source})

def _cloudflare_access_required() -> PlainTextResponse:
    return PlainTextResponse("Cloudflare Access required.", status_code=403)

# --- Pydantic Request Models --------------------------------------------------

class PrintPayload(BaseModel):
    title: str = ""
    lines: list[str] = []
    cut: bool = True
    add_datetime: bool = True

class RawPayload(BaseModel):
    text: Optional[str] = None
    content: Optional[str] = None
    title: Optional[str] = ""
    subtitle: Optional[str] = ""
    badge: Optional[str] = None
    badge_style: Optional[str] = "inverted"
    badge_alignment: Optional[str] = "center"
    align: Optional[str] = "left"
    font_size: Optional[str] = "normal"
    bold: Optional[bool] = False
    show_timestamp: Optional[bool] = False
    timestamp_format: Optional[str] = None
    timestamp_timezone: Optional[str] = "Europe/Zurich"
    show_sender: Optional[bool] = False
    sender: Optional[str] = None
    footer: Optional[str] = None
    blocks: Optional[List[Dict[str, Any]]] = None
    add_datetime: bool = False
    cut: bool = True
    copies: int = Field(default=1, ge=1, le=5)

class ImageUrlPayload(BaseModel):
    image_url: Optional[str] = None
    image_base64: Optional[str] = None
    title: Optional[str] = ""
    subtitle: Optional[str] = ""

class TicketElement(BaseModel):
    type: str = Field(default="text", description="'text' or 'image'")
    content: Optional[str] = Field(default=None, description="Text body")
    title: Optional[str] = None
    subtitle: Optional[str] = None
    image_url: Optional[str] = None
    image_base64: Optional[str] = None
    add_datetime: Optional[bool] = False

class TicketItem(BaseModel):
    type: Optional[str] = "text"
    content: Optional[str] = None
    text: Optional[str] = None
    title: Optional[str] = None
    subtitle: Optional[str] = None
    badge: Optional[str] = None
    badge_style: Optional[str] = "inverted"
    badge_alignment: Optional[str] = "center"
    align: Optional[str] = "left"
    font_size: Optional[str] = "normal"
    bold: Optional[bool] = False
    show_timestamp: Optional[bool] = False
    show_sender: Optional[bool] = False
    sender: Optional[str] = None
    footer: Optional[str] = None
    image_url: Optional[str] = None
    image_base64: Optional[str] = None
    elements: Optional[List[TicketElement]] = None
    cut: Optional[bool] = True
    copies: Optional[int] = 1
    blocks: Optional[List[Dict[str, Any]]] = None

    class Config:
        extra = "allow"

class BatchPrintPayload(BaseModel):
    printer_id: Optional[str] = "default"
    idempotency_key: Optional[str] = None
    separate: Optional[bool] = True
    tickets: List[TicketItem]

# --- Routes: Health & Status --------------------------------------------------

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    path = os.path.join(static_dir, "favicon.ico")
    if os.path.exists(path):
        return FileResponse(path)
    return FileResponse(os.path.join(static_dir, "favicon.png"))

@app.get("/_health", response_class=PlainTextResponse)
def health():
    return "OK"

@app.get("/")
def root_redirect():
    return RedirectResponse("/ui", status_code=303)

@app.get("/ui/status")
def ui_status():
    data = printer_status()
    return JSONResponse(
        {
            "online": data.get("online", False),
            "checked_at": data.get("checked_at"),
            "method": data.get("method"),
            "detail": data.get("detail"),
        }
    )

@app.get("/api/status")
def api_status(request: Request):
    check_api_key(request)
    return get_queue_status()

@app.get("/api/printer/capabilities")
def api_capabilities(request: Request):
    check_api_key(request)
    return {
        "model": "Generic Thermal Cloud Receipt Printer",
        "paper_width_mm": 80,
        "resolution_dpi": 203,
        "print_width_px": PRINT_WIDTH_PX,
        "supported_image_types": ["PNG", "JPEG", "WebP", "GIF", "BMP", "Base64", "HTTPS URLs"],
        "dithering_algorithms": ["floyd", "bayer", "threshold"],
        "cutter_support": "Manual tear-off bar confirmed; cut_paper signal forwarded to firmware",
        "protocol": "MQTT JSON-wrapped monochrome Base64 PNG (No ESC/POS)",
        "queue_store": "MongoDB persistent database 'printer'",
        "batch_support": True,
        "max_tickets_per_batch": 20
    }

# --- Routes: API (Printing) ---------------------------------------------------

@app.post("/print")
async def print_job(p: PrintPayload, request: Request):
    check_api_key(request)
    if (not p.title.strip()) and (not any(line.strip() for line in p.lines)):
        return {"ok": False, "msg": "Empty print job ignored."}
    jid = _print_text_content(p.title, p.lines, p.add_datetime, p.cut, "api")
    return {"ok": True, "job_id": jid}

@app.post("/api/print/template")
async def api_print_template(p: PrintPayload, request: Request):
    check_api_key(request)
    if (not p.title.strip() or p.title.strip().lower() == "tasks") and not any(line.strip() for line in p.lines):
        return {"ok": False, "msg": "Empty or default template ignored."}
    jid = _print_text_content(p.title, p.lines, p.add_datetime, p.cut, "api")
    return {"ok": True, "job_id": jid}

@app.post("/api/print/raw")
@app.post("/api/print/text")
async def api_print_raw(p: RawPayload, request: Request):
    check_api_key(request)
    payload = p.model_dump(exclude_none=True)
    try:
        norm = normalize_ticket(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    img = render_normalized_ticket(norm, cfg=ReceiptCfg(), width_px=PRINT_WIDTH_PX, separate=norm.cut)
    b64 = pil_to_base64_png(img)
    job_ids = [
        enqueue_base64_png(b64, cut_paper=(1 if norm.cut else 0), meta={"source": "api", "copy": idx + 1})
        for idx in range(norm.copies)
    ]
    return {"ok": True, "job_id": job_ids[-1], "job_ids": job_ids, "copies": norm.copies}

@app.post("/api/print/image")
async def api_print_image(
    request: Request,
    file: UploadFile = File(...),
    img_title: str | None = Form(None),
    img_subtitle: str | None = Form(None),
):
    check_api_key(request)
    src = await _read_safe_image(file)
    jid = _print_image_content(src, img_title, img_subtitle, True, "api")
    return {"ok": True, "job_id": jid}

@app.post("/api/print/image-url")
async def api_print_image_url(p: ImageUrlPayload, request: Request):
    check_api_key(request)
    if p.image_url:
        img = await fetch_image_from_url_safe(p.image_url)
    elif p.image_base64:
        img = load_image_from_base64(p.image_base64)
    else:
        raise HTTPException(status_code=400, detail="Must provide image_url or image_base64")
    
    try:
        save_original_image(img, filename_hint=p.title or "api_image", source="api_url")
    except Exception as e:
        print(f"[archive] Warning: Error archiving URL image: {e}", flush=True)

    cfg = ReceiptCfg()
    elements = [{"type": "image", "image_obj": img, "title": p.title, "subtitle": p.subtitle}]
    final_img = render_ticket_elements(elements, cfg, width_px=PRINT_WIDTH_PX, separate=True)
    b64 = pil_to_base64_png(final_img)
    jid = enqueue_base64_png(b64, cut_paper=1, meta={"source": "api_image_url"})
    return {"ok": True, "job_id": jid}

@app.post("/api/print/batch")
@app.post("/api/print/tickets")
async def api_print_batch(payload: BatchPrintPayload, request: Request):
    check_api_key(request)
    if not payload.tickets:
        raise HTTPException(status_code=400, detail="Tickets list cannot be empty.")
    if len(payload.tickets) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 tickets allowed per batch request.")

    cfg = ReceiptCfg()
    prepared_tickets = []

    for i, t in enumerate(payload.tickets):
        t_data = t.dict() if hasattr(t, "dict") else dict(t)
        loaded_img = None
        if t_data.get("image_url"):
            loaded_img = await fetch_image_from_url_safe(t_data["image_url"])
        elif t_data.get("image_base64"):
            loaded_img = load_image_from_base64(t_data["image_base64"])

        try:
            norm = normalize_ticket(t_data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Ticket {i+1} validation error: {exc}")

        if loaded_img:
            norm.image_obj = loaded_img

        ticket_img = render_normalized_ticket(norm, cfg=cfg, width_px=PRINT_WIDTH_PX, separate=bool(payload.separate) and norm.cut)
        b64_str = pil_to_base64_png(ticket_img)
        prepared_tickets.append({
            "b64": b64_str,
            "cut": 1 if (payload.separate and norm.cut) else 0,
            "meta": {
                "ticket_index": i,
                "source": "api_batch",
                "title": norm.title,
                "has_badge": bool(norm.badge)
            }
        })

    result = enqueue_batch(
        tickets_data=prepared_tickets,
        separate=bool(payload.separate),
        idempotency_key=payload.idempotency_key,
        meta={"source": "api_batch"}
    )
    return {"ok": True, **result}

# --- Routes: Queue Management (MongoDB-backed) --------------------------------

@app.get("/queue/list")
async def list_queue(request: Request, limit: int = 50, skip: int = 0):
    jobs = get_queue_list(limit=limit, skip=skip)
    return JSONResponse({"count": len(jobs), "jobs": jobs})

@app.get("/queue/status")
async def queue_status(request: Request):
    return JSONResponse(get_queue_status())

@app.post("/queue/clear")
async def clear_queue(request: Request):
    if not require_ui_auth(request):
        check_api_key(request)
    deleted = mongo_clear_queue()
    accept = request.headers.get("accept", "")
    content_type = request.headers.get("content-type", "")
    if "text/html" in accept or "application/x-www-form-urlencoded" in content_type:
        return RedirectResponse("/ui#queue", status_code=303)
    return JSONResponse({"status": "cleared", "deleted_count": deleted})

@app.post("/queue/reset")
async def queue_reset(request: Request):
    check_api_key(request)
    res = mongo_reset_queue()
    return JSONResponse(res)

@app.get("/queue/history")
async def queue_history(request: Request, limit: int = 50, skip: int = 0):
    check_api_key(request)
    records = get_history(limit=limit, skip=skip)
    return JSONResponse({"count": len(records), "records": records})

@app.get("/queue/job/{job_id}")
async def job_detail(job_id: str, request: Request):
    check_api_key(request)
    doc = get_job_status(job_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Job not found")
    return JSONResponse(doc)

@app.get("/queue/batch/{batch_id}")
async def batch_detail(batch_id: str, request: Request):
    check_api_key(request)
    res = get_batch_status(batch_id)
    if not res:
        raise HTTPException(status_code=404, detail="Batch not found")
    return JSONResponse(res)

# --- Routes: UI HTML ----------------------------------------------------------

@app.get("/ui", response_class=HTMLResponse)
def ui_index(request: Request):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    csrf_tok = _get_or_create_csrf(request)
    return html_page("Receipt Printer", HTML_UI, show_logout=True, csrf_token=csrf_tok, secure=_is_secure_request(request))

@app.post("/ui/print/template")
async def ui_print_template(
    request: Request,
    title: str = Form(""),
    lines: str = Form(""),
    text: str | None = Form(None),
    add_dt: bool = Form(False),
):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    body = (lines or text or "")
    clean_lines = [ln.rstrip() for ln in body.splitlines()]
    _print_text_content(title.strip(), clean_lines, add_dt, True, "ui")
    return RedirectResponse("/ui#tpl", status_code=303)

@app.post("/ui/print/raw")
async def ui_print_raw(
    request: Request,
    text: str = Form(""),
    add_dt: bool = Form(False),
):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    lines = (text or "").splitlines()
    _print_text_content("", lines, add_dt, True, "ui")
    return RedirectResponse("/ui#raw", status_code=303)

@app.post("/ui/print/image")
async def ui_print_image(
    request: Request,
    file: UploadFile = File(...),
    img_title: str | None = Form(None),
    img_subtitle: str | None = Form(None),
):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    try:
        src = await _read_safe_image(file)
        _print_image_content(src, img_title, img_subtitle, True, "ui")
        return RedirectResponse("/ui#img", status_code=303)
    except Exception as exc:
        log("ui_print_image error:", type(exc).__name__)
        return html_page("Receipt Printer", "<div class='card'>Image processing failed.</div>")

@app.post("/ui/print/batch")
async def ui_print_batch(
    request: Request,
    tickets_text: str = Form(""),
    separate: bool = Form(True),
    add_dt: bool = Form(True),
):
    if not require_ui_auth(request):
        return _cloudflare_access_required()

    chunks = [c.strip() for c in tickets_text.split("---") if c.strip()]
    if not chunks:
        return RedirectResponse("/ui#batch", status_code=303)

    cfg = ReceiptCfg()
    prepared = []
    for idx, chunk in enumerate(chunks):
        lines = chunk.splitlines()
        first_line = lines[0] if lines else "Ticket"
        body_lines = lines[1:] if len(lines) > 1 else []
        norm = normalize_ticket({
            "title": first_line,
            "content": "\n".join(body_lines),
            "show_timestamp": add_dt,
            "show_sender": False,
            "cut": separate
        })
        ticket_img = render_normalized_ticket(norm, cfg=cfg, width_px=PRINT_WIDTH_PX, separate=separate)
        b64 = pil_to_base64_png(ticket_img)
        prepared.append({
            "b64": b64,
            "cut": 1 if separate else 0,
            "meta": {"ticket_index": idx, "source": "ui_batch", "title": norm.title}
        })

    enqueue_batch(prepared, separate=separate, meta={"source": "ui_batch"})
    return RedirectResponse("/ui#queue", status_code=303)

# --- Routes: Guest Links ------------------------------------------------------

@app.get("/guest/{token}", response_class=HTMLResponse)
def guest_ui(token: str, request: Request):
    info = GUESTS.validate(token)
    if not info:
        return html_page("Guest", "<div class='card'>Invalid or disabled link.</div>", show_logout=False, status_code=403, show_nav=False, header_url="#")
    
    csrf_tok = _get_or_create_csrf(request)
    is_unlimited = bool(info.get("unlimited") or info.get("quota_per_day", 5) <= 0)
    today = time.strftime("%Y-%m-%d")
    used_today = int(info.get("used", {}).get(today, 0)) if isinstance(info.get("used"), dict) else 0

    if is_unlimited:
        guest_banner = (
            f"<div class='card' style='display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; border:1px solid #f59e0b; background:linear-gradient(135deg, rgba(245,158,11,0.12), rgba(217,119,6,0.06));'>"
            f"<div>Guest: <b>{info['name']}</b> · printed today: <b>{used_today}</b></div>"
            f"<div style='display:inline-flex; align-items:center; gap:6px; padding:4px 14px; border-radius:999px; background:rgba(245,158,11,0.22); border:1px solid #f59e0b; color:#fbbf24; font-weight:800; font-size:1rem; box-shadow:0 0 10px rgba(245,158,11,0.3)'>"
            f"<span>left today:</span> <span style='font-family:system-ui,-apple-system,sans-serif; font-size:1.35rem; font-weight:800; line-height:1;'>&infin;</span>"
            f"</div>"
            f"</div>"
        )
    else:
        remaining = GUESTS.remaining_today(token)
        guest_banner = f"<div class='card'>Guest: <b>{info['name']}</b> · left today: <b>{remaining}</b></div>"

    content = (
        guest_banner
        + guest_ui_html()
    )
    content = content.replace('/ui/print/template', f'/guest/{token}/print/template')
    content = content.replace('/ui/print/raw', f'/guest/{token}/print/raw')
    content = content.replace('/ui/print/image', f'/guest/{token}/print/image')

    if request.headers.get("X-Partial") == "true":
        return HTMLResponse(content.replace("{csrf}", csrf_tok))
    return html_page("Guest print", content, show_logout=False, csrf_token=csrf_tok, secure=_is_secure_request(request), show_nav=False, header_url=f"/guest/{token}")

@app.post("/guest/{token}/print/template")
async def guest_print_template(
    token: str,
    title: str = Form(""),
    lines: str = Form(""),
    text: str | None = Form(None),
    add_dt: bool = Form(False),
):
    body = (lines or text or "")
    txt_full = (title or "").strip() + "\n" + body
    if add_dt: txt_full += f"\n{now_str('%Y-%m-%d %H:%M')}"
    ok, msg = _guest_check_len_ok(len(txt_full))
    if not ok: return html_page("Guest print", msg, show_logout=False, show_nav=False, header_url=f"/guest/{token}")

    tok = guest_consume_or_error(token)
    if not tok: return html_page("Guest print", "<div class='card'>Limit reached or invalid link.</div>", show_logout=False, show_nav=False, header_url=f"/guest/{token}")

    _print_text_content(
        title.strip(), 
        [ln.rstrip() for ln in body.splitlines()], 
        add_dt, True, "guest", sender=tok["name"]
    )
    return RedirectResponse(f"/guest/{token}#tpl", status_code=303)

@app.post("/guest/{token}/print/raw")
async def guest_print_raw(
    token: str,
    text: str = Form(""),
    add_dt: bool = Form(False),
):
    full = (text or "")
    if add_dt: full += f"\n{now_str('%Y-%m-%d %H:%M')}"
    ok, msg = _guest_check_len_ok(len(full))
    if not ok: return html_page("Guest print", msg, show_logout=False, show_nav=False, header_url=f"/guest/{token}")

    tok = guest_consume_or_error(token)
    if not tok: return html_page("Guest print", "<div class='card'>Limit reached or invalid link.</div>", show_logout=False, show_nav=False, header_url=f"/guest/{token}")

    lines = full.splitlines()
    _print_text_content("", lines, False, True, "guest", sender=tok["name"])
    return RedirectResponse(f"/guest/{token}#raw", status_code=303)

@app.post("/guest/{token}/print/image")
async def guest_print_image(
    token: str,
    file: UploadFile = File(...),
    img_title: str | None = Form(None),
    img_subtitle: str | None = Form(None),
):
    tok = guest_consume_or_error(token)
    if not tok: return html_page("Guest print", "<div class='card'>Limit reached or invalid link.</div>", show_logout=False, show_nav=False, header_url=f"/guest/{token}")
    src = await _read_safe_image(file)
    _print_image_content(src, img_title, img_subtitle, True, "guest", sender=tok["name"])
    return RedirectResponse(f"/guest/{token}#img", status_code=303)

# --- Routes: Settings & Admin -------------------------------------------------

@app.get("/ui/settings", response_class=HTMLResponse)
def ui_settings(request: Request):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    csrf_tok = _get_or_create_csrf(request)
    content = "<h3 class='title'>Settings</h3>" + settings_html_form()
    if request.headers.get("X-Partial") == "true":
        return HTMLResponse(content.replace("{csrf}", csrf_tok))
    return html_page("Settings", content, show_logout=True, csrf_token=csrf_tok, secure=_is_secure_request(request))

@app.post("/ui/settings/save", response_class=HTMLResponse)
async def ui_settings_save(request: Request):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    form = await request.form()
    for key, default, typ, _ in SET_KEYS:
        if typ == "checkbox":
            SETTINGS[key] = True if form.get(key) else False
        else:
            val = form.get(key)
            if val is None:
                SETTINGS[key] = default
            else:
                if typ == "number":
                    try: SETTINGS[key] = int(val) if "." not in val else float(val)
                    except: SETTINGS[key] = default
                else:
                    SETTINGS[key] = val
    _save_settings(SETTINGS)
    _reload_settings_if_changed()
    return RedirectResponse("/ui/settings", status_code=303)

@app.post("/ui/settings/test", response_class=HTMLResponse)
def ui_settings_test(request: Request):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    csrf_tok = _get_or_create_csrf(request)
    sample_lines = ["Read - 10 Min", "Drink water", "Plan – 10 Min", "Exercise – 20 Min"]
    _print_text_content("TEST", sample_lines, True, True, "ui")
    return html_page("Settings", "<div class='card'>Test ticket enqueued.</div>", show_logout=True, csrf_token=csrf_tok, secure=_is_secure_request(request))

CANONICAL_PUBLIC_ORIGIN = "https://printer.nox007.tech"

def _base_url(request: Request) -> str:
    return CANONICAL_PUBLIC_ORIGIN

def _guest_link(token: str, request: Optional[Request] = None) -> str:
    return f"{CANONICAL_PUBLIC_ORIGIN}/guest/{token}"

def _render_guests_admin(request: Request, csrf: str = "") -> str:
    rows = []
    for token, info in GUESTS.list():
        link = _guest_link(token, request)
        rid = f"lnk_{token[:8]}"
        name = info.get("name", "Guest")
        status_key = info.get("status_key", "active")
        status_label = info.get("status_label", "Active")
        quota = info.get("quota_per_day", 5)
        rem = info.get("remaining_today", quota)
        used = info.get("used_today", 0)
        is_unlimited = bool(info.get("unlimited") or quota <= 0)

        # Status badge styling
        if is_unlimited:
            badge_html = '''<span style="display:inline-flex; align-items:center; gap:5px; padding:4px 12px; border-radius:999px; background:linear-gradient(135deg, rgba(245,158,11,0.22), rgba(217,119,6,0.12)); border:1px solid #f59e0b; color:#fbbf24; font-weight:800; font-size:0.85rem; box-shadow:0 0 10px rgba(245,158,11,0.3)">
              <span style="font-family:system-ui,-apple-system,sans-serif; font-size:1.25rem; font-weight:800; line-height:1;">&infin;</span>
              <span>Unlimited</span>
            </span>'''
            toggle_btn = f'''
              <form method="post" action="/ui/guests/toggle?csrf={csrf}" style="display:inline">
                <input type="hidden" name="csrf_token" value="{csrf}">
                <input type="hidden" name="token" value="{token}">
                <input type="hidden" name="enabled" value="0">
                <button type="submit" class="secondary" style="padding:8px 12px; min-height:42px; font-size:0.85rem">Disable</button>
              </form>
            '''
        elif status_key == "active":
            badge_html = '<span style="display:inline-block; padding:4px 10px; border-radius:999px; background:rgba(16,185,129,0.15); color:#10b981; font-weight:700; font-size:0.85rem">Active</span>'
            toggle_btn = f'''
              <form method="post" action="/ui/guests/toggle?csrf={csrf}" style="display:inline">
                <input type="hidden" name="csrf_token" value="{csrf}">
                <input type="hidden" name="token" value="{token}">
                <input type="hidden" name="enabled" value="0">
                <button type="submit" class="secondary" style="padding:8px 12px; min-height:42px; font-size:0.85rem">Disable</button>
              </form>
            '''
        elif status_key == "disabled":
            badge_html = '<span style="display:inline-block; padding:4px 10px; border-radius:999px; background:rgba(234,179,8,0.15); color:#eab308; font-weight:700; font-size:0.85rem">Disabled</span>'
            toggle_btn = f'''
              <form method="post" action="/ui/guests/toggle?csrf={csrf}" style="display:inline">
                <input type="hidden" name="csrf_token" value="{csrf}">
                <input type="hidden" name="token" value="{token}">
                <input type="hidden" name="enabled" value="1">
                <button type="submit" style="background:#10b981; padding:8px 12px; min-height:42px; font-size:0.85rem">Enable</button>
              </form>
            '''
        elif status_key == "quota_reached":
            badge_html = '<span style="display:inline-block; padding:4px 10px; border-radius:999px; background:rgba(148,163,184,0.15); color:#94a3b8; font-weight:700; font-size:0.85rem">Quota Full</span>'
            toggle_btn = f'''
              <form method="post" action="/ui/guests/toggle?csrf={csrf}" style="display:inline">
                <input type="hidden" name="csrf_token" value="{csrf}">
                <input type="hidden" name="token" value="{token}">
                <input type="hidden" name="enabled" value="0">
                <button type="submit" class="secondary" style="padding:8px 12px; min-height:42px; font-size:0.85rem">Disable</button>
              </form>
            '''
        else: # revoked
            badge_html = '<span style="display:inline-block; padding:4px 10px; border-radius:999px; background:rgba(239,68,68,0.15); color:#ef4444; font-weight:700; font-size:0.85rem">Revoked</span>'
            toggle_btn = '<span style="color:var(--muted); font-size:0.85rem; padding:8px 12px; display:inline-block">-</span>'

        delete_btn = f'''
          <form method="post" action="/ui/guests/delete?csrf={csrf}" style="display:inline" onsubmit="return confirm('Do you really want to permanently delete the guest access for {name}?');">
            <input type="hidden" name="csrf_token" value="{csrf}">
            <input type="hidden" name="token" value="{token}">
            <button type="submit" style="background:#ef4444; color:#fff; padding:8px 12px; min-height:42px; font-size:0.85rem">Delete</button>
          </form>
        '''

        if is_unlimited:
            unlimited_btn = '<button type="submit" class="secondary" style="padding:4px 8px; min-height:28px; font-size:0.75rem; border-color:#eab308; color:#f59e0b">Set to limit</button>'
            usage_info = f'<span style="display:inline-flex; align-items:center; gap:4px; color:#f59e0b; font-weight:700"><span style="font-family:system-ui,-apple-system,sans-serif; font-size:1.15rem; font-weight:800; line-height:1;">&infin;</span> Unlimited ({used} today)</span>'
            quota_val = 50
        else:
            unlimited_btn = '<button type="submit" style="background:linear-gradient(135deg,#f59e0b,#d97706); color:#000; font-weight:800; padding:4px 8px; min-height:28px; font-size:0.75rem; border-radius:6px; border:none; display:inline-flex; align-items:center; gap:4px; cursor:pointer"><span style="font-family:system-ui,-apple-system,sans-serif; font-size:1rem; font-weight:800; line-height:1">&infin;</span> Unlimited</button>'
            usage_info = f'<span>{rem} / {quota} left <span style="color:var(--muted); font-size:0.78rem">({used} today)</span></span>'
            quota_val = quota

        row = f"""
        <tr style="border-bottom:1px solid var(--line);">
          <td style="padding:10px 8px; font-weight:600;">{name}</td>
          <td style="padding:10px 8px;">{badge_html}</td>
          <td style="padding:10px 8px; font-size:0.9rem;">
            <div style="display:flex; flex-direction:column; gap:6px;">
              <form method="post" action="/ui/guests/quota?csrf={csrf}" style="display:inline-flex; align-items:center; gap:4px; flex-wrap:nowrap">
                <input type="hidden" name="csrf_token" value="{csrf}">
                <input type="hidden" name="token" value="{token}">
                <button type="submit" name="delta" value="-5" title="5 less" class="secondary" style="padding:3px 6px; min-height:28px; font-weight:bold; font-size:0.75rem">-5</button>
                <button type="submit" name="delta" value="-1" title="1 less" class="secondary" style="padding:3px 6px; min-height:28px; font-weight:bold; font-size:0.75rem">-1</button>
                <input type="number" name="quota" value="{quota_val}" min="1" max="1000" style="width:58px; text-align:center; padding:4px 6px; border-radius:6px; border:1px solid #263043; background:#0d1117; color:#dfe7ff; font-size:0.85rem; font-weight:600">
                <button type="submit" name="delta" value="+1" title="1 more" class="secondary" style="padding:3px 6px; min-height:28px; font-weight:bold; font-size:0.75rem">+1</button>
                <button type="submit" name="delta" value="+5" title="5 more" class="secondary" style="padding:3px 6px; min-height:28px; font-weight:bold; font-size:0.75rem">+5</button>
                <button type="submit" class="secondary" style="padding:3px 8px; min-height:28px; font-size:0.75rem">OK</button>
              </form>
              <div style="display:inline-flex; align-items:center; gap:8px">
                <form method="post" action="/ui/guests/unlimited?csrf={csrf}" style="display:inline">
                  <input type="hidden" name="csrf_token" value="{csrf}">
                  <input type="hidden" name="token" value="{token}">
                  <input type="hidden" name="unlimited" value="{0 if is_unlimited else 1}">
                  {unlimited_btn}
                </form>
                <span style="font-size:0.8rem">{usage_info}</span>
              </div>
            </div>
          </td>
          <td style="padding:10px 8px;">
            <div class="row" style="gap:6px; align-items:center; flex-wrap:nowrap">
              <input id="{rid}" type="text" value="{link}" readonly
                     style="width:24ch; max-width:100%; background:#0d1117; border:1px solid #263043; color:#dfe7ff; padding:8px 10px; border-radius:8px; font-family:monospace; font-size:0.85rem;">
              <button type="button" class="secondary" onclick="copyToClipboard('{rid}')" style="padding:8px 12px; min-height:42px; font-size:0.85rem;">Copy</button>
            </div>
          </td>
          <td style="padding:10px 8px;">
            <div class="row" style="gap:8px; align-items:center; flex-wrap:nowrap">
              {toggle_btn}
              {delete_btn}
            </div>
          </td>
        </tr>
        """
        rows.append(row)

    copy_js = """
    <script>
    function copyToClipboard(id){
      const el=document.getElementById(id); if(!el) return;
      el.select(); el.setSelectionRange(0, 99999);
      navigator.clipboard.writeText(el.value).then(()=>{
        const b=el.nextElementSibling; 
        if(b){ b.textContent="Copied"; setTimeout(()=>b.textContent="Copy",1200); }
      });
    }
    </script>
    """
    table = f"""
    <section class="card">
      <h3 class="title">Guest Management</h3>
      <div style="overflow:auto">
      <table style="width:100%; border-collapse:collapse; min-width:600px">
        <thead>
          <tr style="border-bottom:2px solid var(--line);">
            <th style="text-align:left; padding:10px 8px;">Name</th>
            <th style="text-align:left; padding:10px 8px;">Status</th>
            <th style="text-align:left; padding:10px 8px;">Quota (Daily Limit)</th>
            <th style="text-align:left; padding:10px 8px;">Link</th>
            <th style="text-align:left; padding:10px 8px;">Actions</th>
          </tr>
        </thead>
        <tbody>
          {("".join(rows) if rows else '<tr><td colspan="5" style="padding:14px; text-align:center; color:var(--muted)">No guest tokens created yet.</td></tr>')}
        </tbody>
      </table>
      </div>
    </section>
    """
    form = f"""
    <section class='card' style='margin-top:14px'>
      <h3 class="title">Create New Guest Link</h3>
      <form method='post' action='/ui/guests/create?csrf={csrf}' class='row' style='gap:12px; flex-wrap:wrap; align-items:center'>
        <input type="hidden" name="csrf_token" value="{csrf}">
        <input type='text' name='name' placeholder='Guest name (e.g. Yaralie)' style='min-width:200px; max-width:320px; padding:10px;'>
        
        <div style="display:inline-flex; align-items:center; gap:6px">
          <label for="new_quota" style="font-size:0.9rem">Prints/Day:</label>
          <input id="new_quota" type='number' name='quota' value='50' min='1' step='1' style='width:80px; padding:10px;'>
        </div>

        <label style="display:inline-flex; align-items:center; gap:8px; cursor:pointer; padding:8px 14px; border-radius:8px; background:rgba(245,158,11,0.15); border:1px solid #f59e0b; color:#fbbf24; font-weight:700">
          <input type="checkbox" name="unlimited" value="1" id="chk_unlimited" onchange="document.getElementById('new_quota').disabled = this.checked">
          <span style="font-family:system-ui,-apple-system,sans-serif; font-size:1.35rem; font-weight:800; line-height:1;">&infin;</span>
          <span>Unlimited Prints (Golden Infinity)</span>
        </label>

        <button type='submit' style='min-height:44px;'>Create Link</button>
      </form>
    </section>
    """
    return copy_js + table + form

@app.get("/ui/guests", response_class=HTMLResponse)
def ui_guests(request: Request):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    csrf_tok = _get_or_create_csrf(request)
    content = _render_guests_admin(request, csrf=csrf_tok)
    if request.headers.get("X-Partial") == "true":
        return HTMLResponse(content)
    return html_page("Guest", content, show_logout=True, csrf_token=csrf_tok, secure=_is_secure_request(request))

@app.post("/ui/guests/create", response_class=HTMLResponse)
async def ui_guests_create(request: Request):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    csrf_tok = _get_or_create_csrf(request)
    form = await request.form()
    name = (form.get("name") or "Guest").strip()
    unlimited = bool(form.get("unlimited"))
    try:
        quota = int((form.get("quota") or 50))
    except ValueError:
        quota = 50
    token = GUESTS.create(name, quota_per_day=0 if unlimited else quota, unlimited=unlimited)
    link = _guest_link(token, request)
    unl_badge = " (&infin; Unlimited)" if unlimited else f" ({quota} prints/day)"
    msg = f"<div class='card' style='background:rgba(16,185,129,0.1); border-color:#10b981;'>New guest link created for <b>{name}</b>{unl_badge}: <a href='{link}' target='_blank' style='color:#10b981; font-weight:bold;'>{link}</a></div>"
    return html_page("Guest", msg + _render_guests_admin(request, csrf=csrf_tok), show_logout=True, csrf_token=csrf_tok, secure=_is_secure_request(request))

@app.post("/ui/guests/toggle", response_class=HTMLResponse)
async def ui_guests_toggle(request: Request):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    csrf_tok = _get_or_create_csrf(request)
    form = await request.form()
    token = (form.get("token") or "").strip()
    enabled = bool(int(form.get("enabled", 1)))
    ok = GUESTS.set_enabled(token, enabled)
    status_str = "enabled" if enabled else "disabled"
    msg = f"<div class='card'>Guest link was successfully {status_str}.</div>" if ok else "<div class='card' style='color:#ef4444'>Status could not be updated.</div>"
    return html_page("Guest", msg + _render_guests_admin(request, csrf=csrf_tok), show_logout=True, csrf_token=csrf_tok, secure=_is_secure_request(request))

@app.post("/ui/guests/quota", response_class=HTMLResponse)
async def ui_guests_quota(request: Request):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    csrf_tok = _get_or_create_csrf(request)
    form = await request.form()
    token = (form.get("token") or "").strip()
    delta_str = (form.get("delta") or "").strip()

    if delta_str:
        try:
            delta = int(delta_str)
        except ValueError:
            delta = 0
        current_quota = 50
        for tok_id, tok_info in GUESTS.list():
            if tok_id == token:
                current_quota = tok_info.get("quota_per_day") or 50
                break
        new_quota = max(1, current_quota + delta)
        ok = GUESTS.set_quota(token, new_quota, unlimited=False)
        msg = f"<div class='card'>Daily quota adjusted by {delta:+d} to {new_quota} prints.</div>" if ok else "<div class='card' style='color:#ef4444'>Token not found.</div>"
    else:
        try:
            quota = int(form.get("quota") or 50)
        except ValueError:
            quota = 50
        ok = GUESTS.set_quota(token, quota, unlimited=(quota <= 0))
        msg = f"<div class='card'>Daily quota successfully set to {quota} prints.</div>" if ok else "<div class='card' style='color:#ef4444'>Token not found.</div>"
    return html_page("Guest", msg + _render_guests_admin(request, csrf=csrf_tok), show_logout=True, csrf_token=csrf_tok, secure=_is_secure_request(request))

@app.post("/ui/guests/unlimited", response_class=HTMLResponse)
async def ui_guests_unlimited(request: Request):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    csrf_tok = _get_or_create_csrf(request)
    form = await request.form()
    token = (form.get("token") or "").strip()
    
    if "unlimited" in form:
        unlimited_flag = bool(int(form.get("unlimited")))
    else:
        current = GUESTS.validate(token)
        is_unl = bool(current.get("unlimited") or current.get("quota_per_day", 5) <= 0) if current else False
        unlimited_flag = not is_unl

    if unlimited_flag:
        ok = GUESTS.set_quota(token, quota_per_day=0, unlimited=True)
        msg = "<div class='card' style='border-color:#f59e0b; background:rgba(245,158,11,0.15); color:#fbbf24; font-weight:700'><span style='font-family:system-ui,-apple-system,sans-serif; font-size:1.2rem; font-weight:800; line-height:1;'>&infin;</span> Guest access set to UNLIMITED prints (Golden Infinity)!</div>" if ok else "<div class='card' style='color:#ef4444'>Token not found.</div>"
    else:
        ok = GUESTS.set_quota(token, quota_per_day=50, unlimited=False)
        msg = "<div class='card'>Guest access reset to limit (50 prints/day).</div>" if ok else "<div class='card' style='color:#ef4444'>Token not found.</div>"

    return html_page("Guest", msg + _render_guests_admin(request, csrf=csrf_tok), show_logout=True, csrf_token=csrf_tok, secure=_is_secure_request(request))

@app.post("/ui/guests/delete", response_class=HTMLResponse)
async def ui_guests_delete(request: Request):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    csrf_tok = _get_or_create_csrf(request)
    form = await request.form()
    token = (form.get("token") or "").strip()
    ok = GUESTS.delete(token)
    msg = "<div class='card'>Guest access was permanently deleted.</div>" if ok else "<div class='card' style='color:#ef4444'>Token not found.</div>"
    return html_page("Guest", msg + _render_guests_admin(request, csrf=csrf_tok), show_logout=True, csrf_token=csrf_tok, secure=_is_secure_request(request))

# --- Routes: Original & Recovered Image Archive -------------------------

def _render_images_admin(request: Request, csrf: str = "") -> str:
    images = list_archived_images(limit=250)
    count = len(images)
    
    cards = []
    for img in images:
        fname = img.get("filename", "")
        orig_name = img.get("original_name") or fname
        dims = f"{img.get('width', 0)}×{img.get('height', 0)}"
        fmt = img.get("format", "IMG")
        sz = img.get("size_human", "")
        sender = img.get("sender") or "Unknown"
        created = img.get("created_iso") or ""
        src_tag = img.get("source") or "upload"
        img_url = f"/ui/images/original/{fname}"

        card = f"""
        <div class="card" style="padding:10px; display:flex; flex-direction:column; justify-content:space-between; background:#0d1117; border:1px solid #263043;">
          <a href="{img_url}" target="_blank" style="display:block; overflow:hidden; border-radius:6px; background:#000;">
            <img src="{img_url}" alt="{orig_name}" style="width:100%; height:180px; object-fit:cover; display:block; border-radius:6px;" loading="lazy">
          </a>
          <div style="margin-top:10px; font-size:0.85rem;">
            <div style="font-weight:700; color:#dfe7ff; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="{orig_name}">
              {orig_name}
            </div>
            <div style="display:flex; justify-content:space-between; color:var(--muted); font-size:0.75rem; margin-top:4px;">
              <span>{dims} · {fmt}</span>
              <span>{sz}</span>
            </div>
            <div style="display:flex; justify-content:space-between; color:var(--muted); font-size:0.75rem; margin-top:2px;">
              <span>From: <b style="color:#dfe7ff">{sender}</b> <span style="font-size:0.7rem; opacity:0.8">({src_tag})</span></span>
              <span>{created}</span>
            </div>
          </div>
          <div style="margin-top:10px; display:flex; justify-content:space-between; gap:6px;">
            <a href="{img_url}" target="_blank" class="secondary" style="flex:1; text-align:center; padding:6px; font-size:0.8rem; text-decoration:none; border-radius:6px; border:1px solid #263043; display:inline-block">Full Size</a>
            <form method="post" action="/ui/images/delete?csrf={csrf}" style="display:inline" onsubmit="return confirm('Do you really want to delete this image from the archive?');">
              <input type="hidden" name="csrf_token" value="{csrf}">
              <input type="hidden" name="filename" value="{fname}">
              <button type="submit" style="background:#ef4444; color:#fff; padding:6px 10px; font-size:0.8rem; border-radius:6px; border:none; cursor:pointer">Delete</button>
            </form>
          </div>
        </div>
        """
        cards.append(card)

    if cards:
        grid_html = f'<div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(260px, 1fr)); gap:16px;">{"".join(cards)}</div>'
    else:
        grid_html = '<div style="text-align:center; padding:40px; color:var(--muted); background:#0d1117; border-radius:8px; border:1px dashed #263043;">No original images in archive yet. All images printed via Guest Links, Web UI, API, or ChatGPT will automatically be saved here in full original quality and color (up to 250 images).</div>'

    # Recovered unique printed images
    recovered_items = list_recovered_images("unique_printed")
    rec_cards = []
    for r in recovered_items:
        r_fname = r.get("filename", "")
        r_crop_raw = r.get("crop_path") or ""
        r_crop = os.path.basename(r_crop_raw) if r_crop_raw else ""
        r_dims = f"{r.get('width', 0)}×{r.get('height', 0)}"
        r_src = r.get("source", "print")
        r_date = r.get("date_iso", "")
        r_jid = r.get("job_id", "")
        r_prints = r.get("print_count", 1)
        r_preview_url = f"/ui/images/crop/{r_crop}" if r_crop else f"/ui/images/recovered/{r_fname}"
        r_full_url = f"/ui/images/recovered/{r_fname}"
        crop_btn = f"<a href='/ui/images/crop/{r_crop}' target='_blank' style='flex:1; text-align:center; padding:6px; font-size:0.8rem; text-decoration:none; border-radius:6px; background:#1e293b; color:#93c5fd; display:inline-block'>Cropped Photo</a>" if r_crop else ""

        rec_card = f"""
        <div class="card" style="padding:10px; display:flex; flex-direction:column; justify-content:space-between; background:#0d1117; border:1px solid #263043;">
          <a href="{r_full_url}" target="_blank" style="display:block; overflow:hidden; border-radius:6px; background:#000;">
            <img src="{r_preview_url}" alt="{r_jid}" style="width:100%; height:180px; object-fit:contain; background:#111; display:block; border-radius:6px;" loading="lazy">
          </a>
          <div style="margin-top:10px; font-size:0.85rem;">
            <div style="font-weight:700; color:#dfe7ff; font-family:monospace; font-size:0.8rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="{r_jid}">
              {r_jid}
            </div>
            <div style="display:flex; justify-content:space-between; color:var(--muted); font-size:0.75rem; margin-top:4px;">
              <span>{r_dims} · 576px thermal</span>
              <span>{r_prints}x printed</span>
            </div>
            <div style="display:flex; justify-content:space-between; color:var(--muted); font-size:0.75rem; margin-top:2px;">
              <span>Source: <b style="color:#dfe7ff">{r_src}</b></span>
              <span>{r_date}</span>
            </div>
          </div>
          <div style="margin-top:10px; display:flex; justify-content:space-between; gap:6px;">
            <a href="{r_full_url}" target="_blank" class="secondary" style="flex:1; text-align:center; padding:6px; font-size:0.8rem; text-decoration:none; border-radius:6px; border:1px solid #263043; display:inline-block">Full 576px</a>
            {crop_btn}
          </div>
        </div>
        """
        rec_cards.append(rec_card)

    rec_grid_html = f'<div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(260px, 1fr)); gap:16px;">{"".join(rec_cards)}</div>' if rec_cards else '<div style="color:var(--muted)">No recovered prints found.</div>'

    html = f"""
    <section class="card">
      <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:14px">
        <div>
          <h3 class="title" style="margin:0">Original Image Archive</h3>
          <div style="color:var(--muted); font-size:0.85rem; margin-top:4px">
            Archived original images in full quality and color (protected by Cloudflare Access).
          </div>
        </div>
        <div style="display:inline-flex; align-items:center; gap:8px">
          <span style="display:inline-block; padding:4px 12px; border-radius:999px; background:rgba(59,130,246,0.15); border:1px solid rgba(59,130,246,0.3); color:#60a5fa; font-weight:700; font-size:0.85rem">
            {count} / {MAX_ARCHIVED_IMAGES} Images
          </span>
          <a href="/api/images/original" target="_blank" class="secondary" style="padding:6px 10px; font-size:0.8rem; text-decoration:none; border-radius:6px; border:1px solid #263043; display:inline-block">REST API</a>
        </div>
      </div>
      {grid_html}
    </section>

    <section class="card" style="margin-top:24px">
      <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:14px">
        <div>
          <h3 class="title" style="margin:0">Recovered Printed Images & Photos</h3>
          <div style="color:var(--muted); font-size:0.85rem; margin-top:4px">
            All {len(recovered_items)} distinct unique images and photos printed on thermal paper (576px native thermal resolution).
          </div>
        </div>
        <div style="display:inline-flex; align-items:center; gap:8px">
          <span style="display:inline-block; padding:4px 12px; border-radius:999px; background:rgba(16,185,129,0.15); border:1px solid rgba(16,185,129,0.3); color:#34d399; font-weight:700; font-size:0.85rem">
            {len(recovered_items)} Unique Printed Images
          </span>
        </div>
      </div>
      {rec_grid_html}
    </section>
    """
    return html

@app.get("/ui/images", response_class=HTMLResponse)
def ui_images(request: Request):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    csrf_tok = _get_or_create_csrf(request)
    content = _render_images_admin(request, csrf=csrf_tok)
    if request.headers.get("X-Partial") == "true":
        return HTMLResponse(content)
    return html_page("Images", content, show_logout=True, csrf_token=csrf_tok, secure=_is_secure_request(request))

@app.post("/ui/images/delete", response_class=HTMLResponse)
async def ui_images_delete(request: Request):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    csrf_tok = _get_or_create_csrf(request)
    form = await request.form()
    filename = (form.get("filename") or "").strip()
    ok = delete_archived_image(filename)
    msg = "<div class='card'>Image was deleted from archive.</div>" if ok else "<div class='card' style='color:#ef4444'>Image could not be deleted.</div>"
    return html_page("Images", msg + _render_images_admin(request, csrf=csrf_tok), show_logout=True, csrf_token=csrf_tok, secure=_is_secure_request(request))

@app.get("/ui/images/original/{filename}")
def serve_ui_original_image(filename: str, request: Request):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    fpath = get_image_file_path(filename, category="original")
    if not fpath or not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="Image not found")
    ext = os.path.splitext(filename)[1].lower()
    media_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    return FileResponse(
        fpath,
        media_type=media_types.get(ext, "image/png"),
        headers={"Cache-Control": "private, no-cache, no-store"}
    )

@app.get("/ui/images/recovered/{filename}")
def serve_ui_recovered_image(filename: str, request: Request):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    fpath = get_image_file_path(filename, category="unique_printed") or get_image_file_path(filename, category="all_printed")
    if not fpath or not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(
        fpath,
        media_type="image/png",
        headers={"Cache-Control": "private, no-cache, no-store"}
    )

@app.get("/ui/images/crop/{filename}")
def serve_ui_crop_image(filename: str, request: Request):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    fpath = get_image_file_path(filename, category="cropped_photos")
    if not fpath or not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(
        fpath,
        media_type="image/png",
        headers={"Cache-Control": "private, no-cache, no-store"}
    )

@app.get("/images/original/{filename}")
def serve_original_image_legacy(filename: str, request: Request):
    if not require_ui_auth(request):
        return RedirectResponse(f"/ui/images/original/{filename}", status_code=307)
    return serve_ui_original_image(filename, request)

@app.get("/images/{category}/{filename}")
def serve_legacy_category_image(category: str, filename: str, request: Request):
    if not require_ui_auth(request):
        return RedirectResponse(f"/ui/images/{category}/{filename}", status_code=307)
    fpath = get_image_file_path(filename, category=category)
    if not fpath or not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="Image not found")
    ext = os.path.splitext(filename)[1].lower()
    return FileResponse(
        fpath,
        media_type="image/png" if ext == ".png" else "image/jpeg",
        headers={"Cache-Control": "private, no-cache, no-store"}
    )

@app.get("/api/images/original")
def api_list_original_images(request: Request, limit: int = 250):
    if not require_ui_auth(request):
        raise HTTPException(status_code=401, detail="Authentication required via Cloudflare Access or API key")
    images = list_archived_images(limit=min(limit, 250))
    return {
        "ok": True,
        "count": len(images),
        "total": len(images),
        "max_capacity": MAX_ARCHIVED_IMAGES,
        "images": images
    }

@app.post("/ui/guests/revoke", response_class=HTMLResponse)
async def ui_guests_revoke(request: Request):
    if not require_ui_auth(request):
        return _cloudflare_access_required()
    csrf_tok = _get_or_create_csrf(request)
    form = await request.form()
    tok = form.get("token") or ""
    ok = GUESTS.revoke(tok)
    msg = "<div class='card'>Token widerrufen.</div>" if ok else "<div class='card'>Token not found.</div>"
    return html_page("Guest", msg + _render_guests_admin(request, csrf=csrf_tok), show_logout=True, csrf_token=csrf_tok, secure=_is_secure_request(request))


# --- Routes: ChatGPT Plugin Discovery & Well-Known ----------------------------

@app.get("/.well-known/ai-plugin.json")
def well_known_ai_plugin():
    plugin_path = os.path.join(os.path.dirname(__file__), "plugin", "ai-plugin.json")
    if os.path.exists(plugin_path):
        with open(plugin_path, "r", encoding="utf-8") as f:
            return JSONResponse(json.load(f))
    raise HTTPException(404, "Plugin manifest not found")

@app.get("/.well-known/mcp.json")
def well_known_mcp():
    mcp_path = os.path.join(os.path.dirname(__file__), "plugin", "mcp.json")
    if os.path.exists(mcp_path):
        with open(mcp_path, "r", encoding="utf-8") as f:
            return JSONResponse(json.load(f))
    raise HTTPException(404, "MCP manifest not found")


# --- Routes: Debug ------------------------------------------------------------

@app.get("/debug/last")
async def debug_last(request: Request):
    check_api_key(request)
    path = "/tmp/last_print.png"
    if os.path.exists(path):
        return FileResponse(path, media_type="image/png")
    return {"error": "no debug file found"}

# --- Routes: ChatGPT Action OpenAPI Specification ----------------------------

@app.get("/chatgpt-action.json")
@app.get("/api/chatgpt-action.json")
def chatgpt_action_spec():
    """Returns an OpenAPI 3.1 schema ready for ChatGPT Custom Actions."""
    return JSONResponse({
        "openapi": "3.1.0",
        "info": {
            "title": "Thermal Receipt Printer API",
            "description": "Prints dynamic text tickets and images on a physical thermal receipt printer. Daily plans can use headings, one block per time slot, dividers, emoji, and QR codes for provided links.",
            "version": "2.0.0"
        },
        "servers": [
            {"url": "https://printer.nox007.tech", "description": "Primary Production Printer Service"},
            {"url": "https://nox007.tech", "description": "Legacy Ingress"}
        ],
        "paths": {
            "/api/print/tickets": {
                "post": {
                    "operationId": "printTickets",
                    "summary": "Print multiple tickets in a batch",
                    "description": "Prints 1 to 20 separate tickets in order. Use structured blocks for readable day plans, checklists, headings, time slots, dividers, and QR codes.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/BatchPrintPayload"
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "Batch successfully enqueued"
                        }
                    }
                }
            },
            "/api/print/text": {
                "post": {
                    "operationId": "printText",
                    "summary": "Print a single text ticket",
                    "description": "Print a single formatted ticket. For a daily plan use title/date, section headings, todo blocks with empty checkboxes for actionable tasks, text blocks for appointments/travel, and dividers; encode a supplied link in a qr_code block. Mark a todo checked only when completion is known. The renderer adds a static completion count automatically when todos are present.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/RawPayload"
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {"description": "Text ticket enqueued"}
                    }
                }
            },
            "/api/print/image-url": {
                "post": {
                    "operationId": "printImage",
                    "summary": "Print an image from HTTPS URL",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/ImageUrlPayload"
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {"description": "Image ticket enqueued"}
                    }
                }
            },
            "/api/status": {
                "get": {
                    "operationId": "getPrinterStatus",
                    "summary": "Check printer online status and queue length",
                    "responses": {
                        "200": {"description": "Status info"}
                    }
                }
            },
            "/queue/batch/{batch_id}": {
                "get": {
                    "operationId": "getBatchStatus",
                    "summary": "Get status of a specific batch print job",
                    "parameters": [
                        {
                            "name": "batch_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"}
                        }
                    ],
                    "responses": {
                        "200": {"description": "Batch details"}
                    }
                }
            }
        },
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "x-api-key"
                },
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer"
                }
            },
            "schemas": {
                "RawPayload": {
                    "type": "object",
                    "allOf": [
                        {"$ref": "#/components/schemas/TicketItem"},
                        {"type": "object", "properties": {"add_datetime": {"type": "boolean"}}}
                    ]
                },
                "ImageUrlPayload": {
                    "type": "object",
                    "properties": {
                        "image_url": {"type": "string"},
                        "title": {"type": "string"},
                        "subtitle": {"type": "string"}
                    }
                },
                "TicketItem": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["text", "image"]},
                        "content": {"type": "string"},
                        "text": {"type": "string", "description": "Backward-compatible alias for content."},
                        "title": {"type": "string"},
                        "subtitle": {"type": "string"},
                        "badge": {"type": "string"},
                        "badge_style": {"type": "string", "enum": ["inverted", "border", "bold"]},
                        "badge_alignment": {"type": "string", "enum": ["left", "center", "right"]},
                        "align": {"type": "string", "enum": ["left", "center", "right"]},
                        "font_size": {"type": "string", "enum": ["small", "normal", "large"]},
                        "bold": {"type": "boolean"},
                        "show_timestamp": {"type": "boolean"},
                        "timestamp_format": {"type": "string"},
                        "timestamp_timezone": {"type": "string"},
                        "show_sender": {"type": "boolean"},
                        "sender": {"type": "string"},
                        "footer": {"type": "string"},
                        "blocks": {
                            "type": "array",
                            "description": "Ordered layout blocks. Use heading, todo, text, badge, divider, spacer, image, and qr_code for readable daily plans and structured tickets.",
                            "items": {
                                "type": "object",
                                "required": ["type"],
                                "properties": {
                                    "type": {"type": "string", "enum": ["heading", "text", "todo", "badge", "divider", "spacer", "image", "qr_code"]},
                                    "text": {"type": "string"},
                                    "checked": {"type": "boolean", "default": False, "description": "Whether this task is already complete; leave false when completion is unknown."},
                                    "level": {"type": "integer", "minimum": 1, "maximum": 3},
                                    "align": {"type": "string", "enum": ["left", "center", "right"]},
                                    "bold": {"type": "boolean"},
                                    "font_size": {"type": "string", "enum": ["small", "normal", "large"]},
                                    "badge_style": {"type": "string", "enum": ["inverted", "border", "bold"]},
                                    "divider_style": {"type": "string", "enum": ["solid", "dashed", "dotted"]},
                                    "height_px": {"type": "integer", "minimum": 4, "maximum": 200},
                                    "qr_data": {"type": "string", "description": "Text or HTTPS link encoded in the QR code."},
                                    "qr_box_size": {"type": "integer", "minimum": 2, "maximum": 10}
                                }
                            }
                        },
                        "image_url": {"type": "string"},
                        "image_base64": {"type": "string"},
                        "cut": {"type": "boolean"},
                        "copies": {"type": "integer", "minimum": 1, "maximum": 5}
                    }
                },
                "BatchPrintPayload": {
                    "type": "object",
                    "required": ["tickets"],
                    "properties": {
                        "separate": {"type": "boolean"},
                        "idempotency_key": {"type": "string"},
                        "tickets": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/TicketItem"}
                        }
                    }
                }
            }
        },
        "security": [
            {"ApiKeyAuth": []},
            {"BearerAuth": []}
        ]
    })
