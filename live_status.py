# live_status.py — Live Printer Status State Machine & Server-Sent Events (SSE)
from __future__ import annotations
import asyncio
import time
import threading
from typing import Dict, Any, List, Optional
import json
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

live_router = APIRouter()

class LivePrintStatusTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._state: str = "idle"  # offline, idle, queued, sending, printing, printed, failed
        self._label: str = "Printer online"
        self._detail: str = ""
        self._job_id: Optional[str] = None
        self._batch_id: Optional[str] = None
        self._batch_current: int = 0
        self._batch_total: int = 0
        self._last_state_change: float = time.time()
        self._printed_timestamp: float = 0.0
        self._subscribers: List[asyncio.Queue] = []
        self._subscribers_lock = threading.Lock()

    def get_status(self, is_printer_online: bool = True) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            # If in printed state and 6 seconds elapsed, transition back to idle
            if self._state == "printed" and (now - self._printed_timestamp) >= 6.0:
                self._state = "idle"
                self._label = "Printer online"
                self._detail = "Ready for printing"
                self._job_id = None
                self._batch_id = None
                self._last_state_change = now

            if not is_printer_online and self._state in {"idle", "printed"}:
                return {
                    "state": "offline",
                    "label": "Printer offline",
                    "detail": "Printer not reached or heartbeat missing",
                    "online": False,
                    "printer_online": False,
                    "busy": False,
                    "job_id": self._job_id,
                    "batch_id": self._batch_id,
                    "progress": "",
                }

            progress = f"{self._batch_current}/{self._batch_total}" if self._batch_total > 1 else ""

            return {
                "state": self._state,
                "label": self._label,
                "detail": self._detail,
                "online": is_printer_online,
                "printer_online": is_printer_online,
                "busy": self._state in {"queued", "sending", "printing"},
                "job_id": self._job_id,
                "batch_id": self._batch_id,
                "progress": progress,
                "batch_current": self._batch_current,
                "batch_total": self._batch_total,
                "updated_at": self._last_state_change,
            }

    def notify_queued(self, job_id: str, batch_id: Optional[str] = None, total: int = 1):
        with self._lock:
            self._state = "queued"
            self._job_id = job_id
            self._batch_id = batch_id
            self._batch_total = total
            self._batch_current = 0
            self._label = f"Printing 1/{total}" if total > 1 else "Printing..."
            self._detail = "Job in Warteschlange..."
            self._last_state_change = time.time()
        self._broadcast()

    def notify_sending(self, job_id: str, batch_id: Optional[str] = None, current: int = 1, total: int = 1):
        with self._lock:
            self._state = "printing"
            self._job_id = job_id
            self._batch_id = batch_id
            self._batch_current = current
            self._batch_total = total
            self._label = f"Printing {current}/{total}" if total > 1 else "Printing..."
            self._detail = "Transferring to printer..."
            self._last_state_change = time.time()
        self._broadcast()

    def notify_printed(self, job_id: Optional[str] = None, batch_id: Optional[str] = None):
        with self._lock:
            now = time.time()
            self._state = "printed"
            self._printed_timestamp = now
            self._last_state_change = now

            if self._batch_total > 1:
                self._label = f"{self._batch_total} tickets printed"
            else:
                self._label = "Printed"
            self._detail = "Print completed successfully"
        self._broadcast()

    def notify_failed(self, error_msg: str):
        with self._lock:
            self._state = "failed"
            self._label = "Print error"
            self._detail = error_msg
            self._last_state_change = time.time()
        self._broadcast()

    def register_subscriber(self, q: asyncio.Queue):
        with self._subscribers_lock:
            self._subscribers.append(q)

    def unregister_subscriber(self, q: asyncio.Queue):
        with self._subscribers_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _broadcast(self):
        # Notify all active SSE listeners
        with self._subscribers_lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(True)
                except Exception:
                    pass

LIVE_TRACKER = LivePrintStatusTracker()

@live_router.get("/ui/live-status")
@live_router.get("/api/live-status")
@live_router.get("/live-status")
async def get_live_status_endpoint():
    from logic import printer_status
    st = printer_status()
    online = bool(st.get("online", False))
    return JSONResponse(LIVE_TRACKER.get_status(is_printer_online=online))

@live_router.get("/ui/events")
@live_router.get("/api/events")
@live_router.get("/events")
async def sse_events_endpoint(request: Request):
    """Server-Sent Events stream for zero-polling real-time status updates."""
    from logic import printer_status


    async def event_generator():
        q = asyncio.Queue()
        LIVE_TRACKER.register_subscriber(q)
        try:
            # Yield initial status immediately
            st = printer_status()
            online = bool(st.get("online", False))
            current = LIVE_TRACKER.get_status(is_printer_online=online)
            yield f"event: status\ndata: {json.dumps(current)}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    await asyncio.wait_for(q.get(), timeout=10.0)
                except asyncio.TimeoutError:
                    pass

                st = printer_status()
                online = bool(st.get("online", False))
                current = LIVE_TRACKER.get_status(is_printer_online=online)
                yield f"event: status\ndata: {json.dumps(current)}\n\n"
        finally:
            LIVE_TRACKER.unregister_subscriber(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )

