# Thermodrucker Plugin Skill & Instructions

Dieses Plugin verbindet ChatGPT mit einem physischen 80mm-Thermobondrucker (`HS-C830ULWB-2199`) über einen sicheren Remote-MCP-Server (`https://printer.nox007.tech/mcp`).

## Verfügbare Tools

1. **`print_text`**
   - **Zweck**: Druckt einen einzelnen Textbeleg / ein Ticket.
   - **Parameter**:
     - `text` (String, erforderlich): Der auszudruckende Text. Zeilenumbrüche (`\n`) werden beibehalten.
     - `title` (String, optional): Fetter Überschriftstitel auf dem Ticket.
     - `cut` (Boolean, Standard `true`): Führt nach dem Druck Papiervorschub zum Abreissen durch.

2. **`print_image`**
   - **Zweck**: Druckt ein Bild von einer HTTPS-URL oder als Base64-String.
   - **Parameter**:
     - `image_url` (String, optional): Öffentliche HTTPS-URL des Bildes (PNG, JPEG, WebP, GIF).
     - `image_base64` (String, optional): Base64-kodierte Bilddaten.
     - `title` (String, optional): Fetter Header über dem Bild.
     - `subtitle` (String, optional): Kleiner Untertitel unter dem Header.

3. **`print_tickets`**
   - **Zweck**: Druckt mehrere Tickets in einem einzigen Batch-Auftrag mit garantierter Reihenfolge.
   - **Parameter**:
     - `tickets` (Array von Objekten, max. 20): Liste von Tickets. Jedes Ticket kann Text (`content`, `title`), ein Bild (`image_url`, `image_base64`) oder gemischte Elemente enthalten.
     - `separate` (Boolean, Standard `true`): Trennt Tickets physisch durch Vorschub und Abrissmarkierungen.
     - `idempotency_key` (String, optional): Eindeutiger Schlüssel zur Vermeidung von Doppel-Drucken bei Retries.

4. **`get_printer_status`**
   - **Zweck**: Prüft, ob der Drucker online ist (Heartbeat innerhalb der letzten 90 Sekunden), die Anzahl anstehender Warteschlangen-Jobs und die MQTT-Broker-Verbindung.

5. **`get_print_job_status`**
   - **Zweck**: Prüft den aktuellen Ausführungsstatus eines Druckauftrags anhand seiner `job_id`.

6. **`get_batch_status`**
   - **Zweck**: Prüft den Fortschritt eines Batch-Auftrags anhand der `batch_id`.

7. **`get_printer_capabilities`**
   - **Zweck**: Liefert technische Details des Druckers (80mm / 576px Druckbreite, 203 DPI, Floyd-Steinberg-Dithering, MQTT Monochrome Base64 PNG).

## Beispiele für ChatGPT-Interaktionen

- **Einfacher Text**: „Drucke eine Einkaufsliste mit Milch, Brot und Kaffee.“ -> Tool: `print_text(text="• Milch\n• Brot\n• Kaffee", title="Einkaufsliste")`
- **Bild**: „Drucke dieses Logo https://example.com/logo.png.“ -> Tool: `print_image(image_url="https://example.com/logo.png", title="Logo")`
- **Mehrere Tickets**: „Drucke 3 Tickets mit 3 Witzen.“ -> Tool: `print_tickets(tickets=[{"content": "Witz 1..."}, {"content": "Witz 2..."}, {"content": "Witz 3..."}], separate=True)`
- **Statusabfrage**: „Ist der Drucker online?“ -> Tool: `get_printer_status()`
