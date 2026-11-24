# MQTT Thermal Printer API & Service

This repository provides a production-ready API, Web UI, and ChatGPT/MCP integration for sending print jobs to a physical thermal receipt printer over a dedicated Mosquitto MQTT broker and MongoDB queue.

- **Primary Endpoint**: `https://printer.nox007.tech`
- **Web Interface**: `https://printer.nox007.tech/ui`
- **Live Status & SSE Stream**: `https://printer.nox007.tech/live-status` & `https://printer.nox007.tech/events`
- **ChatGPT Custom Action Spec**: `https://printer.nox007.tech/chatgpt-action.json` (also `/api/chatgpt-action.json`)
- **Streamable MCP Server**: `https://printer.nox007.tech/mcp`
- **Cloudflare Protected Resource Metadata**: `https://printer.nox007.tech/.well-known/cloudflare-access-protected-resource/mcp`
*(Legacy access via `https://nox007.tech` remains supported for backward compatibility).*

---

## 1. System Overview & Architecture

> Network addresses, the hardware address and MQTT topics shown here are documentation placeholders. Set `PRINTER_IP` and `PRINTER_TOPIC` to the values for the target deployment; do not publish real device identifiers.


1. **Content Sources & Formats**
   - Single text tickets, formatted receipts with templates, and raw plain text.
   - Images via file upload, Base64 data, or public HTTPS URLs with SSRF protection.
   - Multi-ticket batch printing (`/api/print/batch`) with guaranteed sequential ordering, bottom margin feeds (70px), and rotary cutter separation.
   - Model Context Protocol (MCP) and ChatGPT Custom Actions for direct AI assistant control.
   - Clean ticket finish: automatic trailing dashed lines (`---------------------`) have been completely removed; bottom feed margin ensures the physical rotary cutter never clips content.

2. **Rendering & Image Processing**
   - Renders 576-pixel wide monochrome bitmaps (203 DPI, 80mm paper roll).
   - Tone controls: Gamma, Contrast, Brightness, and Inversion.
   - Dithering algorithms: Floyd-Steinberg error diffusion, Ordered Bayer, or Threshold.
   - Full typography support: German/French umlauts (ä, ö, ü, ß, é, è, à), Swiss currency symbols (CHF), Unicode punctuation, and emoji rendered as monochrome artwork using Noto Color Emoji.
   - SSRF Protection: Private IP / loopback / cloud-metadata blocking, redirect limits, and decompression bomb protection.

3. **Queue & Transport (MongoDB & Mosquitto MQTT)**
   - Persistent queue stored in MongoDB (`printer.queue`, `printer.archive`, `printer.batches`).
   - Lifecycle states: `queued`, `processing`, `published`, `acknowledged`, `printed`, `failed`, `cancelled`.
   - Idempotency-Key support to eliminate duplicate printing on retries.
   - Background worker monitors printer online status (via `Hearbeat` / `heartbeat` topics) and flushes jobs with QoS 2.

4. **Printer Hardware Profile**
   - **Model**: `HS-C830ULWB-2199` (HSPOS / Hoin 80mm Cloud Thermal Receipt Printer).
   - **Auto-Cutter**: Integrated rotary cutter with partial and full cut support (`cut_paper: 1`).
   - **Local Network**: Accessible at `192.0.2.1` via Tailscale subnet route `192.0.2.0/24 dev tailscale0`.
   - **MAC & MQTT Topic**: MAC `00:00:5E:00:53:01` -> Subscribe Topic `Prn00005E005301`.
   - **Hardware Ports**:
     - `80`: Embedded Web Administration (Status, Wi-Fi STA, Ethernet/IP).
     - `8000`: IPP Everywhere Sample print server.
     - `9100`: Direct RAW ESC/POS printer port (verified functional with text, Umlauts, and cutter).
     - `9101`: USSI command interface.
   - **Cloud MQTT Protocol**: The printer subscribes to topic `Prn00005E005301` and natively expects base64 monochrome PNGs wrapped in JSON:
     ```json
     {
       "ticket_id": "job-...",
       "data_type": "png",
       "data_base64": "<base64_encoded_png>",
       "paper_type": 0,
       "paper_width_mm": 0,
       "paper_height_mm": 0,
       "cut_paper": 1,
       "source": "printer"
     }
     ```
   - **Heartbeat & Ack**: Printer publishes heartbeats to `Hearbeat` and print acks to `PrintSuccess`.

---

## 2. Cloudflare Access Managed OAuth (ChatGPT MCP Integration)

The printer service uses the exact same production-proven Cloudflare Access Managed OAuth architecture as **Trip API** (`https://ojp20.nox007.tech/mcp`) and **Pompadour Booker** (`https://barber.nox007.tech/mcp`):

- **Architecture**: Cloudflare Zero Trust Access acts as the OAuth 2.0 authorization server at the edge with Dynamic Client Registration (DCR) enabled for ChatGPT. Managed OAuth grant: 15-minute access tokens and a 720-hour (30-day) refresh session; Access rechecks the single-user policy during refresh. The separate Access application session remains 24 hours.
- **Protected Resource Metadata (RFC 9728)**: Intercepted at Cloudflare Edge and served from `https://printer.nox007.tech/.well-known/cloudflare-access-protected-resource/mcp`.
- **Team Domain**: `thegroupofnox.cloudflareaccess.com`
- **Application AUD**: `cc9ecf68acbaf15b59cb685e8a5acf7fc2431434d8a48c653ddabfdd960459d7`
- **Single-User Access Policy**: Restricted to `account-1@example.invalid`.
- **DCR Redirect URIs**: `["https://chatgpt.com/connector_platform_oauth_redirect", "https://chatgpt.com/connector/oauth/*"]`
- **Origin Token Validation**:
  - Cloudflare Edge validates the client's opaque token and proxies requests via Cloudflare Tunnel (`mchost24`) -> Caddy -> `printer-api:8080`.
  - Cloudflare injects the signed `Cf-Access-Jwt-Assertion` header.
  - The origin verifies the JWT signature against Cloudflare Access JWKS (`https://thegroupofnox.cloudflareaccess.com/cdn-cgi/access/certs`), checking issuer (`https://thegroupofnox.cloudflareaccess.com`), audience (`PRINTER_MCP_CF_ACCESS_AUD`), expiration, and allowed identity email (`account-1@example.invalid`).
  - Internal CLI and automated tests authenticate via `APP_API_KEY` or `CHATGPT_API_KEY`.
- **Available MCP Tools**:
  1. `print_text`: Prints single formatted text tickets with optional title and cut.
  2. `print_image`: Prints images from HTTPS URLs or Base64 with dithering.
  3. `print_tickets`: Prints multi-ticket batches with sequential ordering and rotary cutter separation.
  4. `get_printer_status`: Checks online presence, heartbeat, and queue backlog.
  5. `get_print_job_status`: Checks status of an individual print job.
  6. `get_batch_status`: Checks progress of a multi-ticket batch.
  7. `get_printer_capabilities`: Returns hardware and protocol capabilities.

---

## 3. Real-Time Live Print Status & SSE State Machine

The Web UI features a zero-unnecessary-polling live status architecture:

- **Status State Machine**:
  - `offline`: Printer disconnected or no heartbeat received within 90s.
  - `idle`: Printer connected and idle (`Printer online`).
  - `queued`: Print job received and added to MongoDB queue.
  - `sending`: Job is being published to MQTT broker.
  - `printing`: MQTT transmission active (`Printing...` / `Printing 2/5`).
  - `printed`: Printer published `PrintSuccess` acknowledgement (`Printed` / `5 tickets printed`). Auto-transitions back to `idle` after 6 seconds.
  - `failed`: Communication or decoding error.
- **Server-Sent Events (SSE)**: Native streaming via `GET /events` (and `/ui/events`). UI updates instantly upon state transitions without polling.
- **Page Visibility API**: When the browser tab is hidden or backgrounded (`document.hidden`), SSE connections and fallback polling pause immediately to save mobile battery and bandwidth.

---

## 4. Guest Links Lifecycle Management

Located at `/ui/guests` in the Web UI:

- **On/Off Toggle**: Each guest link can be enabled or disabled instantly without changing tokens, quotas, or URLs.
- **Permanent Delete**: Permanently removes the guest token with an interactive confirmation prompt. Historical print records in `printer.archive` are preserved for accountability.
- **Canonical URLs**: All guest links use canonical HTTPS format `https://printer.nox007.tech/guest/<token>`.
- **Mobile Touch Targets**: All toggle and delete controls have a minimum touch target of 44px for effortless operation on smartphones.

---

## 5. Multi-Tier Categorized Rate Limiting

To prevent accidental denial-of-service from aggressive UI reloads while keeping legitimate operations smooth, rate limiting is segmented by request type:

| Category | Limit | Routes Included |
|---|---|---|
| **exempt** | Unlimited | Static assets, `/favicon.ico`, health probes, well-known endpoints |
| **status_read** | 1,200 req / min | `/ui/status`, `/live-status`, `/queue/status`, `/events` SSE |
| **ui_page** | 300 req / min | HTML page navigations (`/ui`, `/guest/*`) |
| **write_print** | 60 req / min | Print submissions (`/api/print/*`, `/ui/print/*`) |
| **admin_action** | 30 req / min | Queue clearing, resets, guest link creation / deletion |

All rate-limited responses return standard HTTP 429 with `Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Category` headers.

---

## 6. Remote MCP Tools Reference

The Remote MCP server (`https://printer.nox007.tech/mcp`) exposes 9 production-grade tools:

| Tool | Parameters | Description |
|---|---|---|
| `print_text` | `content`/`text`, `title`, `subtitle`, `badge`, `badge_style`, `badge_alignment`, `align`, `bold`, `font_size`, `show_timestamp`, `timestamp_format`, `timestamp_timezone`, `show_sender`, `sender`, `footer`, `blocks`, `cut`, `copies`, `wait_for_result`, `timeout_seconds` | Prints a single formatted receipt ticket with rich layout options, thermal badges, structured blocks, and optional synchronous wait for firmware ACK. |
| `print_tickets` | `tickets`, `separate`, `idempotency_key`, `wait_for_result`, `timeout_seconds` | Batch prints 1-20 sequential tickets with physical tear-off feed spacing, unified rendering pipeline, and hardware confirmation tracking. |
| `preview_ticket` | Same layout options as `print_text` (+ optional `image_url`/`image_base64`) | Renders base64-encoded monochrome 576px PNG preview without queuing or printing anything. |
| `preview_batch` | `tickets`, `separate` | Renders an array of preview images for inspecting multi-ticket batches before physical printing. |
| `print_image` | `image_url`, `image_base64`, `title`, `subtitle`, `content`, `badge`, `badge_style`, `cut`, `wait_for_result` | Downloads or decodes image, applies Floyd-Steinberg dithering, prints 576px canvas. |
| `get_printer_status` | *(none)* | Live status: printer online/offline, heartbeat age, queue count, and flusher status. |
| `get_print_job_status` | `job_id` | Status of an individual print job (`queued`, `rendering`, `published`, `printed`, `unconfirmed`, `failed`). |
| `get_batch_status` | `batch_id` | Progress of a multi-ticket batch (`confirmed_tickets`, `published_tickets`, `unconfirmed_tickets`). |
| `get_printer_capabilities` | *(none)* | Hardware parameters: 80mm, 203 DPI, 576px, supported badges, font sizes, alignment, monochrome PNG protocol. |

### Layout & Formatting Attributes
- **Canonical Content Field**: `content` is the canonical text field. `text` is supported as a backward-compatible alias. Identical values are safely merged; conflicting values are rejected with HTTP 422 before queuing. Empty tickets are rejected.
- **Badge Types & Styles**: High-contrast thermal badges with styles `inverted` (solid black rounded box with crisp white text), `border` (outline box), or `bold` (`◆ TEXT ◆`). Alignment: `left`, `center`, `right`.
- **Structured Blocks**: Custom tickets support heterogeneous `blocks`: `heading`, `text`, `todo`, `badge`, `divider`, `spacer`, `image`, and `qr_code`. Each `todo` block prints an empty square by default; set `checked: true` only for a completed item. Tickets with todos automatically show a static “X von Y erledigt” count calculated at render time.
- **Daily-plan layout**: For natural requests such as “print my daily plan”, MCP guidance asks the model to use a clear title/date, section headings, concise time-slot blocks, and separators. It can include a centered QR code for a supplied link. Complex plans can be previewed with `preview_ticket` before printing.
- **REST / Custom Actions**: `/api/print/text` now accepts the same structured `blocks` and layout fields, and the Custom Action OpenAPI schema exposes them. This keeps formatted plans available outside the MCP plugin too.
- **Timestamp & Sender Controls**: `show_timestamp` defaults to `False`. When enabled, uses `Europe/Zurich` timezone and configurable format. `show_sender` defaults to `False` (eliminating unwanted automatic `From: ChatGPT` signatures).
- **Physical Print Confirmation**: Correlation between outgoing MQTT jobs and incoming firmware `PrintSuccess` ACKs (`OK;{job_id}` or `4;[Prn...];5000;{job_id}`). Status transitions from `published` to `printed` upon physical ACK.

---

## 7. Automated Test Suites

Five comprehensive automated test suites guarantee stability:

1. **MCP Layout & Field Regression Suite (15 Tests)**:
   ```bash
   docker compose exec -T printer-api python3 -m unittest test_mcp_layout_regression.py
   ```
   Verifies `content` vs `text` aliasing, conflict rejection, empty ticket rejection, subtitle rendering, timestamp suppression/presence, sender suppression/presence, thermal badges (inverted/border/bold), structured blocks + QR codes, emoji artwork, to-do checkbox states and static completion counts, non-queuing preview tools, status progression (`queued` -> `published` -> `printed`), and multi-ticket batch rendering.

2. **Ticket Encoding & Regression Suite (10 Tests)**:
   ```bash
   docker compose exec -T printer-api python3 test_encoding_regression.py
   ```
   Verifies deterministic rendering, umlauts, CHF symbols, dithering, bottom feed margins, and the removal of trailing dashed lines.

3. **Printer System Integration Suite (18 Tests)**:
   ```bash
   docker compose exec -T printer-api python3 test_printer_system.py
   ```
   Verifies Mosquitto MQTT authentication, ACL enforcement, queue operations, batch execution, idempotency, SSRF defense, tool discovery, and background flusher payload validation.

4. **OAuth 2.1, Guest Links & Live Status Suite**:
   ```bash
   docker compose exec -T printer-api python3 test_oauth_and_features.py
   ```
   Verifies RFC metadata, Dynamic Client Registration, PKCE S256, token issuance, refresh, revocation, scoped MCP calls, guest toggle/delete lifecycle, SSE streaming, and rate limit categories.

5. **Cloudflare Edge Managed OAuth & MCP Verification**:
   ```bash
   python3 test_cloudflare_mcp_verification.py
   ```
   Verifies Cloudflare Access Managed OAuth challenge at edge, JWT assertion validation against CF JWKS, audience check, single-user email validation, and all 9 MCP tools via JSON-RPC.

---

## 8. Queue Processing & Flusher Resilience

- **Payload Extraction**: Flusher cleanly unpacks `b64 = job.get("b64")` and `cut = int(job.get("cut", 1))` before invoking `mqtt_publish_image_base64(b64, cut_paper=cut, ticket_id=jid)`.
- **Duplicate Key Protection**: Uses `replace_one({"_id": job["_id"]}, job, upsert=True)` when recording published or skipped tickets into `printer.archive` to prevent MongoDB `E11000` collisions.
- **Exponential Backoff**: Transient errors (e.g. temporary network blips) set `retry_after = now + (10 * attempts)` up to 3 attempts. Unrecoverable errors (malformed payloads) transition immediately to `status: "failed"`.
- **Live Status Delivery**: Each flush step updates `LIVE_TRACKER` with batch and job identifiers.



## Maintenance 2026-10-27 — ChatGPT OAuth duration

Cloudflare Access Managed OAuth for the printer MCP app now issues 15-minute access tokens with a 720-hour (30-day) refresh grant. Existing ChatGPT grants may need one reconnection to receive the new duration; the ChatGPT UI refresh cycle remains to be verified. Pre-change OAuth settings and rollback reference: `/home/leandro/service_data/cloudflare_access/backups/chatgpt-mcp-oauth-before-20261027.json`. To roll back, GET the current app, preserve all other fields, and restore the prior `oauth_configuration` from that snapshot.

## Maintenance 2026-10-28 — Emoji and structured plan rendering

The printer image pipeline now uses `fonts-noto-color-emoji` to rasterize emoji into monochrome ticket artwork; text wrapping and alignment account for emoji width across MCP, REST, and Web UI text rendering. MCP and Custom Action descriptions explain how to format daily plans with structured blocks and QR codes using `qr_data`. QR rendering was already supported. Before this change, image: `services-printer-api:rollback-20261028-emoji-layout`. Roll back with `docker tag services-printer-api:rollback-20261028-emoji-layout services-printer-api:latest` followed by `docker compose -f /home/leandro/services/docker-compose.yml up -d --no-deps printer-api`. Persistent data and queue storage are unchanged.

## Maintenance 2026-10-28 — To-do checkbox blocks

Structured `todo` blocks render a square checkbox and task text; `checked: true` adds a check mark. MCP and Custom Action descriptions direct day-plan tasks to these blocks while keeping appointments and travel notes as regular text. Build and deploy only `printer-api`. Before deployment, keep the current image as `services-printer-api:rollback-20261028-todo-checkboxes`; rollback by retagging that image to `services-printer-api:latest` and recreating only `printer-api`. No persistent data changes.

## Maintenance 2026-10-28 — Static checklist progress

Tickets containing todo blocks now render “X von Y erledigt” from the checkbox states at creation time. This count is a snapshot on paper and does not update after printing. HTML input is not required for reusable layouts: ChatGPT can compose structured blocks for each ticket, and previews can be requested before enqueueing.


## Private publication snapshot

This reviewed snapshot excludes the runtime images link and data. Personal email addresses and credential-like test values are replaced by nonfunctional examples; configure real accounts separately. Before updates review each file, run safe tests and a secret scan, push from a separate checkout and verify the remote tree. Do not run physical-print tests without explicit authorization.


## Private GitHub repository (2026-11-17)

A public, reviewed source snapshot is available at `https://github.com/Noxist/rootserver-printer` on `main`. The publication checkout is separate from the live working copy. Runtime data, secrets and other private material are excluded; any substituted placeholders and deployment limitations are documented in the repository. For updates review each changed file, run relevant isolated checks and secret scanning, push only a fast-forward, and verify the GitHub commit and tree. Do not overwrite or merge the live working history.

2026-11-24: The production origin identifies this sanitized repository; the live checkout has an independent Git history and no upstream. Use a separate checkout for development, review and scan each change, and push fast-forward only. Never merge or push the live checkout history into this public snapshot.

The earlier `readme_printer.md` described older layout and test counts; this canonical document incorporates the subsequent feature and maintenance updates.
