# queue_print.py — MongoDB-based Persistent Queue with Job Lifecycle, Idempotency, and Batch Support
import os
import time
import uuid
import threading
import traceback
from typing import Optional, Dict, Any, List

from logic import (
    mqtt_publish_image_base64,
    _get_settings_collection,
    printer_status,
    log,
)

SLEEP_SECONDS = int(os.getenv("PRINT_QUEUE_POLL", "5"))
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "50"))

_running = False
_thread: Optional[threading.Thread] = None
_lock = threading.Lock()

# ----------------- MongoDB Helpers -----------------

def _get_db():
    coll = _get_settings_collection()
    return coll.database if coll is not None else None

def _get_queue_coll():
    db = _get_db()
    return db["queue"] if db is not None else None

def _get_archive_coll():
    db = _get_db()
    return db["archive"] if db is not None else None

def _get_batches_coll():
    db = _get_db()
    return db["batches"] if db is not None else None

# ----------------- In-Flight & Print Confirmation Tracking -----------------

_in_flight_lock = threading.Lock()
_recent_published_jobs: List[Dict[str, Any]] = []

def _record_published_job(job_id: str, batch_id: Optional[str], published_at: float):
    with _in_flight_lock:
        _recent_published_jobs.append({
            "job_id": job_id,
            "batch_id": batch_id,
            "published_at": published_at,
            "confirmed": False
        })
        if len(_recent_published_jobs) > 50:
            _recent_published_jobs.pop(0)

def handle_printer_ack(topic: str, payload_str: str):
    """
    Correlates incoming PrintSuccess acknowledgement with the active published job.
    Updates MongoDB archive document to 'printed' and updates batch stats.
    """
    now = time.time()
    target_job_id = None
    target_batch_id = None

    already_confirmed = False
    with _in_flight_lock:
        if payload_str:
            for item in reversed(_recent_published_jobs):
                if item["job_id"] in payload_str:
                    target_job_id = item["job_id"]
                    target_batch_id = item["batch_id"]
                    already_confirmed = item.get("confirmed", False)
                    item["confirmed"] = True
                    break

        if not target_job_id:
            for item in reversed(_recent_published_jobs):
                if not item["confirmed"] and (now - item["published_at"]) <= 30.0:
                    target_job_id = item["job_id"]
                    target_batch_id = item["batch_id"]
                    already_confirmed = False
                    item["confirmed"] = True
                    break

    if not target_job_id:
        log(f"[Queue] Received {topic} ACK but no matching unconfirmed job found in 30s window.")
        return

    log(f"[Queue] Confirmed physical print for job {target_job_id} via {topic} (payload='{payload_str}')")

    a_coll = _get_archive_coll()
    if a_coll is not None:
        a_coll.update_one(
            {"job_id": target_job_id},
            {"$set": {
                "status": "printed",
                "confirmed_at": now,
                "confirmation_topic": topic,
                "confirmation_payload": payload_str,
                "updated_at": now
            }}
        )

    if target_batch_id and not already_confirmed:
        b_coll = _get_batches_coll()
        if b_coll is not None:
            b_coll.update_one(
                {"batch_id": target_batch_id},
                {"$inc": {"confirmed_tickets": 1}, "$set": {"updated_at": now}}
            )

    try:
        from live_status import LIVE_TRACKER
        LIVE_TRACKER.notify_printed(job_id=target_job_id, batch_id=target_batch_id)
    except Exception:
        pass

# ----------------- Idempotency -----------------

def check_idempotency(key: str) -> Optional[Dict[str, Any]]:
    """Checks if a job or batch with the given idempotency_key has already been submitted."""
    if not key:
        return None

    # Check batches
    batches = _get_batches_coll()
    if batches is not None:
        batch_doc = batches.find_one({"idempotency_key": key}, {"_id": 0})
        if batch_doc:
            return {"type": "batch", "data": batch_doc}

    # Check queue
    queue = _get_queue_coll()
    if queue is not None:
        q_doc = queue.find_one({"idempotency_key": key}, {"b64": 0})
        if q_doc:
            q_doc["_id"] = str(q_doc["_id"])
            return {"type": "job", "data": q_doc}

    # Check archive
    archive = _get_archive_coll()
    if archive is not None:
        a_doc = archive.find_one({"idempotency_key": key}, {"b64": 0})
        if a_doc:
            a_doc["_id"] = str(a_doc["_id"])
            return {"type": "job", "data": a_doc}

    return None

# ----------------- Enqueue Operations -----------------

def enqueue_base64_png(
    b64png: str,
    cut_paper: int = 1,
    meta: Optional[Dict[str, Any]] = None,
    job_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    seq: int = 0,
    idempotency_key: Optional[str] = None,
) -> Optional[str]:
    """Saves a single ticket into the MongoDB print queue."""
    coll = _get_queue_coll()
    if coll is None:
        log("[Queue] Error: No MongoDB connection. Job cannot be queued.")
        return None

    meta = meta or {}
    jid = job_id or f"job-{uuid.uuid4().hex[:12]}"
    now = time.time()

    payload = {
        "job_id": jid,
        "batch_id": batch_id,
        "seq": int(seq),
        "idempotency_key": idempotency_key,
        "b64": b64png,
        "cut": int(cut_paper),
        "meta": meta,
        "ts": now,
        "created_at": now,
        "updated_at": now,
        "status": "queued",
        "attempts": 0,
    }

    try:
        coll.insert_one(payload)
        log(f"[Queue] Job {jid} enqueued (source: {meta.get('source', 'api')}, batch: {batch_id})")
        if not batch_id:
            try:
                from live_status import LIVE_TRACKER
                LIVE_TRACKER.notify_queued(jid, None, 1)
            except Exception:
                pass
        return jid
    except Exception as e:
        log(f"[Queue] Failed to enqueue job {jid}: {e}")
        return None



def enqueue_batch(
    tickets_data: List[Dict[str, Any]],
    separate: bool = True,
    idempotency_key: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Atomically enqueues a batch of tickets maintaining order via sequential indices.
    Returns batch information.
    """
    if idempotency_key:
        existing = check_idempotency(idempotency_key)
        if existing:
            log(f"[Queue] Idempotency match found for key '{idempotency_key}'")
            return {
                "idempotent": True,
                "batch_id": existing["data"].get("batch_id") or existing["data"].get("job_id"),
                "status": existing["data"].get("status", "unknown"),
                "details": existing["data"],
            }

    batch_id = f"batch-{uuid.uuid4().hex[:12]}"
    meta = meta or {}
    job_ids = []

    batches_coll = _get_batches_coll()
    now = time.time()

    for idx, t in enumerate(tickets_data):
        jid = f"{batch_id}-{idx+1:02d}"
        b64 = t["b64"]
        cut = t.get("cut", 1 if separate else 0)
        t_meta = {**meta, **t.get("meta", {}), "batch_index": idx, "total_in_batch": len(tickets_data)}

        res = enqueue_base64_png(
            b64png=b64,
            cut_paper=cut,
            meta=t_meta,
            job_id=jid,
            batch_id=batch_id,
            seq=idx,
            idempotency_key=f"{idempotency_key}-t{idx}" if idempotency_key else None,
        )
        if res:
            job_ids.append(res)

    batch_record = {
        "batch_id": batch_id,
        "idempotency_key": idempotency_key,
        "total_tickets": len(tickets_data),
        "queued_tickets": len(job_ids),
        "job_ids": job_ids,
        "separate": separate,
        "created_at": now,
        "updated_at": now,
        "status": "queued",
        "meta": meta,
    }

    if batches_coll is not None:
        try:
            batches_coll.insert_one(batch_record)
            try:
                from live_status import LIVE_TRACKER
                LIVE_TRACKER.notify_queued(batch_id, batch_id, len(tickets_data))
            except Exception:
                pass
        except Exception as e:
            log(f"[Queue] Warning: Could not record batch document: {e}")


    return {
        "batch_id": batch_id,
        "job_ids": job_ids,
        "total_tickets": len(tickets_data),
        "status": "queued",
    }

# ----------------- Queue Management Queries -----------------

def get_queue_list(limit: int = 50, skip: int = 0) -> List[Dict[str, Any]]:
    """Returns queued jobs without raw image payload for lightweight listing."""
    coll = _get_queue_coll()
    if coll is None:
        return []
    cursor = coll.find({}, {"b64": 0}).sort([("ts", 1), ("seq", 1)]).skip(skip).limit(limit)
    jobs = []
    for doc in cursor:
        doc["_id"] = str(doc["_id"])
        if "created_at" in doc:
            doc["created_at_readable"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(doc["created_at"]))
        jobs.append(doc)
    return jobs


def get_queue_status() -> Dict[str, Any]:
    """Returns live stats on queue depth, flusher status, and printer connectivity."""
    q_coll = _get_queue_coll()
    a_coll = _get_archive_coll()
    p_status = printer_status()

    queue_count = q_coll.count_documents({}) if q_coll is not None else -1
    archive_count = a_coll.count_documents({}) if a_coll is not None else -1

    return {
        "flusher_running": _running,
        "queue_count": queue_count,
        "archive_count": archive_count,
        "printer_online": p_status.get("online", False),
        "printer_status_detail": p_status.get("detail", ""),
        "last_seen": p_status.get("last_seen", 0.0),
    }


def clear_queue() -> int:
    """Deletes all pending jobs from MongoDB queue."""
    q_coll = _get_queue_coll()
    if q_coll is None:
        return 0
    res = q_coll.delete_many({})
    log(f"[Queue] Cleared {res.deleted_count} queued jobs from MongoDB.")
    return res.deleted_count


def reset_queue() -> Dict[str, Any]:
    """Safely halts worker, moves pending jobs to archive as cancelled, and restarts worker."""
    stop_background_flusher()
    q_coll = _get_queue_coll()
    a_coll = _get_archive_coll()

    cancelled_count = 0
    if q_coll is not None and a_coll is not None:
        cursor = q_coll.find()
        to_move = []
        ids = []
        now = time.time()
        for doc in cursor:
            doc["status"] = "cancelled"
            doc["reason"] = "queue_reset"
            doc["archived_at"] = now
            to_move.append(doc)
            ids.append(doc["_id"])

        if to_move:
            a_coll.insert_many(to_move)
            q_coll.delete_many({"_id": {"$in": ids}})
            cancelled_count = len(to_move)

    start_background_flusher()
    log(f"[Queue] Reset complete: {cancelled_count} jobs cancelled, flusher restarted.")
    return {"status": "reset", "cancelled_jobs": cancelled_count}


def get_job_status(job_id: str) -> Optional[Dict[str, Any]]:
    """Fetches status of a specific job by its ID or idempotency key with full confirmation details."""
    now = time.time()
    q_coll = _get_queue_coll()
    if q_coll is not None:
        doc = q_coll.find_one({"$or": [{"job_id": job_id}, {"idempotency_key": job_id}]}, {"b64": 0})
        if doc:
            doc["_id"] = str(doc["_id"])
            doc["location"] = "queue"
            doc["printer_confirmation"] = False
            doc["detail"] = "In MongoDB queue awaiting transmission"
            return doc

    a_coll = _get_archive_coll()
    if a_coll is not None:
        doc = a_coll.find_one({"$or": [{"job_id": job_id}, {"idempotency_key": job_id}]}, {"b64": 0})
        if doc:
            doc["_id"] = str(doc["_id"])
            doc["location"] = "archive"
            st = doc.get("status")
            pub_at = doc.get("published_at", 0)

            if st == "printed":
                doc["printer_confirmation"] = True
                doc["detail"] = f"Print confirmed by printer via {doc.get('confirmation_topic', 'PrintSuccess')}"
            elif st == "published":
                if (now - pub_at) > 20.0:
                    doc["status"] = "unconfirmed"
                    doc["printer_confirmation"] = False
                    doc["detail"] = "Published to MQTT, but no firmware confirmation was received within 20s"
                else:
                    doc["printer_confirmation"] = False
                    doc["detail"] = "Dispatched to printer via MQTT; awaiting firmware receipt confirmation"
            elif st == "failed":
                doc["printer_confirmation"] = False
                doc["detail"] = doc.get("last_error", "Print job failed")
            elif st == "skipped":
                doc["printer_confirmation"] = False
                doc["detail"] = f"Skipped: {doc.get('reason')}"
            return doc

    return None


def get_batch_status(batch_id: str) -> Optional[Dict[str, Any]]:
    """Returns batch progress and individual ticket states with confirmation breakdown."""
    now = time.time()
    b_coll = _get_batches_coll()
    batch_doc = b_coll.find_one({"batch_id": batch_id}, {"_id": 0}) if b_coll is not None else None

    # Fetch jobs belonging to this batch
    q_coll = _get_queue_coll()
    a_coll = _get_archive_coll()

    tickets = []
    if q_coll is not None:
        for d in q_coll.find({"batch_id": batch_id}, {"b64": 0}).sort("seq", 1):
            d["_id"] = str(d["_id"])
            d["location"] = "queue"
            d["printer_confirmation"] = False
            tickets.append(d)

    if a_coll is not None:
        for d in a_coll.find({"batch_id": batch_id}, {"b64": 0}).sort("seq", 1):
            d["_id"] = str(d["_id"])
            d["location"] = "archive"
            st = d.get("status")
            pub_at = d.get("published_at", 0)
            if st == "printed":
                d["printer_confirmation"] = True
            elif st == "published" and (now - pub_at) > 20.0:
                d["status"] = "unconfirmed"
                d["printer_confirmation"] = False
            else:
                d["printer_confirmation"] = False
            tickets.append(d)

    tickets.sort(key=lambda x: x.get("seq", 0))

    total = len(tickets)
    confirmed_count = sum(1 for t in tickets if t.get("status") == "printed")
    published_count = sum(1 for t in tickets if t.get("status") in ("published", "printed"))
    unconfirmed_count = sum(1 for t in tickets if t.get("status") == "unconfirmed")
    failed_count = sum(1 for t in tickets if t.get("status") in ("failed", "skipped_overflow", "cancelled"))
    queued_count = sum(1 for t in tickets if t.get("status") in ("queued", "processing", "pending"))

    if total > 0 and confirmed_count == total:
        overall_status = "printed"
    elif total > 0 and unconfirmed_count > 0 and (confirmed_count + unconfirmed_count == total):
        overall_status = "unconfirmed"
    elif total > 0 and published_count == total:
        overall_status = "published"
    elif queued_count > 0:
        overall_status = "processing" if published_count > 0 else "queued"
    elif failed_count == total and total > 0:
        overall_status = "failed"
    else:
        overall_status = "mixed"

    return {
        "batch_id": batch_id,
        "status": overall_status,
        "total_tickets": total,
        "confirmed_tickets": confirmed_count,
        "published_tickets": published_count,
        "unconfirmed_tickets": unconfirmed_count,
        "failed_tickets": failed_count,
        "queued_tickets": queued_count,
        "tickets": tickets,
    }


def wait_for_job_confirmation(job_id: str, timeout_seconds: float = 15.0) -> Dict[str, Any]:
    """
    Waits synchronously for a job to reach printed (firmware ACK) or failed state.
    Triggers flush immediately to minimize wait time.
    """
    deadline = time.time() + max(2.0, min(timeout_seconds, 35.0))
    try:
        flush_once()
    except Exception:
        pass

    while time.time() < deadline:
        st = get_job_status(job_id)
        if st:
            status_val = st.get("status")
            if status_val in ("printed", "failed"):
                return st
        time.sleep(0.3)
        try:
            flush_once()
        except Exception:
            pass

    final_st = get_job_status(job_id)
    if final_st:
        if final_st.get("status") == "published":
            final_st["status"] = "unconfirmed"
            final_st["detail"] = f"Job published to MQTT, but no firmware ACK received within {timeout_seconds}s"
        return final_st
    return {"job_id": job_id, "status": "unconfirmed", "detail": "Job status could not be confirmed within timeout"}


def wait_for_batch_confirmation(batch_id: str, timeout_seconds: float = 25.0) -> Dict[str, Any]:
    """
    Waits synchronously for all tickets in a batch to be printed or fail.
    """
    deadline = time.time() + max(3.0, min(timeout_seconds, 45.0))
    try:
        flush_once()
    except Exception:
        pass

    while time.time() < deadline:
        bst = get_batch_status(batch_id)
        if bst:
            status_val = bst.get("status")
            if status_val in ("printed", "failed", "unconfirmed"):
                return bst
        time.sleep(0.4)
        try:
            flush_once()
        except Exception:
            pass

    final_bst = get_batch_status(batch_id)
    return final_bst or {"batch_id": batch_id, "status": "unconfirmed", "detail": "Batch confirmation timed out"}


def get_history(limit: int = 50, skip: int = 0) -> List[Dict[str, Any]]:
    """Returns recent jobs from MongoDB archive."""
    a_coll = _get_archive_coll()
    if a_coll is None:
        return []
    cursor = a_coll.find({}, {"b64": 0}).sort("archived_at", -1).skip(skip).limit(limit)
    records = []
    for doc in cursor:
        doc["_id"] = str(doc["_id"])
        if "archived_at" in doc:
            doc["archived_at_readable"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(doc["archived_at"]))
        records.append(doc)
    return records

# ----------------- Legacy Migration -----------------

def migrate_files_to_mongo():
    """Migrates any leftover legacy JSON queue files into MongoDB."""
    queue_dir_str = os.getenv("PRINT_QUEUE_DIR", "/tmp/print-queue")
    from pathlib import Path
    queue_dir = Path(queue_dir_str)
    if not queue_dir.exists():
        return

    files = sorted(queue_dir.glob("*.json"))
    if not files:
        return

    coll = _get_queue_coll()
    if coll is None:
        return

    import json
    count = 0
    for p in files:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "b64" in data:
                doc = {
                    "job_id": f"migrated-{uuid.uuid4().hex[:8]}",
                    "b64": data["b64"],
                    "cut": int(data.get("cut", 1)),
                    "meta": data.get("meta", {}),
                    "ts": data.get("ts", time.time()),
                    "created_at": data.get("ts", time.time()),
                    "status": "queued",
                }
                coll.insert_one(doc)
                p.unlink()
                count += 1
        except Exception as e:
            log(f"[Queue] Legacy migration error for {p.name}: {e}")

    if count > 0:
        log(f"[Queue] Migrated {count} legacy queue files to MongoDB.")

# ----------------- Worker & Flush Loop -----------------

def _clean_overflow_queue():
    """Archives excess tickets to protect against infinite queue bloat."""
    q_coll = _get_queue_coll()
    a_coll = _get_archive_coll()
    if q_coll is None or a_coll is None:
        return

    try:
        count = q_coll.count_documents({})
        if count > MAX_QUEUE_SIZE:
            excess = count - MAX_QUEUE_SIZE
            log(f"[Queue] Queue size ({count}) exceeds limit ({MAX_QUEUE_SIZE}). Archiving {excess} oldest jobs.")
            cursor = q_coll.find().sort("ts", 1).limit(excess)
            to_move = []
            ids_to_remove = []
            now = time.time()
            for doc in cursor:
                doc["archived_at"] = now
                doc["reason"] = "skipped_overflow"
                doc["status"] = "skipped_overflow"
                to_move.append(doc)
                ids_to_remove.append(doc["_id"])
            if to_move:
                for doc in to_move:
                    a_coll.replace_one({"_id": doc["_id"]}, doc, upsert=True)
                q_coll.delete_many({"_id": {"$in": ids_to_remove}})
    except Exception as e:
        log(f"[Queue] Overflow cleanup error: {e}")


def flush_once() -> int:
    """Processes pending queue jobs sequentially when the printer is online."""
    status = printer_status()
    if not status.get("online", False):
        return 0

    q_coll = _get_queue_coll()
    a_coll = _get_archive_coll()
    if q_coll is None:
        return 0

    _clean_overflow_queue()

    now = time.time()
    # Get pending jobs sorted by timestamp and sequence, honoring backoff
    query = {
        "status": {"$in": ["queued", "pending"]},
        "$or": [
            {"retry_after": {"$exists": False}},
            {"retry_after": {"$lte": now}}
        ]
    }
    jobs = list(q_coll.find(query).sort([("ts", 1), ("seq", 1)]).limit(10))
    if not jobs:
        return 0

    printed_count = 0
    for job in jobs:
        jid = job.get("job_id", str(job["_id"]))
        try:
            # Set state to processing
            q_coll.update_one({"_id": job["_id"]}, {"$set": {"status": "processing", "updated_at": time.time()}})

            # Notify live tracker of active sending
            try:
                from live_status import LIVE_TRACKER
                j_meta = job.get("meta") or {}
                j_tot = j_meta.get("total_in_batch", 1)
                j_cur = j_meta.get("batch_index", 0) + 1
                LIVE_TRACKER.notify_sending(jid, job.get("batch_id"), current=j_cur, total=j_tot)
            except Exception:
                pass

            # Extract image payload and cutter setting
            b64 = job.get("b64")
            if not b64 or not isinstance(b64, str):
                raise ValueError(f"Job {jid} has missing or empty image payload (b64)")

            cut = int(job.get("cut", 1))

            # Publish to MQTT topic with unique job ticket_id
            mqtt_publish_image_base64(b64, cut_paper=cut, ticket_id=jid)

            finish_time = time.time()
            _record_published_job(jid, job.get("batch_id"), finish_time)
            job["archived_at"] = finish_time
            job["published_at"] = finish_time
            # We explicitly record 'published' rather than claiming physical completion
            job["status"] = "published"
            job["reason"] = "sent_to_mqtt"

            if a_coll is not None:
                a_coll.replace_one({"_id": job["_id"]}, job, upsert=True)

            q_coll.delete_one({"_id": job["_id"]})
            log(f"[Queue] Published job {jid} to MQTT successfully.")
            printed_count += 1

            # Controlled inter-ticket pacing to prevent buffer overrun on thermal printer
            time.sleep(1.8)

        except Exception as e:
            log(f"[Queue] Error processing job {jid}: {e}")
            fail_time = time.time()
            attempts = int(job.get("attempts", 0)) + 1
            # Corrupt payload or max attempts reached -> mark failed permanently
            is_unrecoverable = isinstance(e, (ValueError, TypeError, KeyError)) or attempts >= 3
            new_status = "failed" if is_unrecoverable else "queued"
            update_fields = {
                "status": new_status,
                "last_error": str(e),
                "updated_at": fail_time,
                "attempts": attempts,
            }
            if not is_unrecoverable:
                update_fields["retry_after"] = fail_time + (10 * attempts)

            q_coll.update_one(
                {"_id": job["_id"]},
                {"$set": update_fields},
            )
            break

    return printed_count


def _loop():
    global _running
    while _running:
        try:
            flush_once()
        except Exception:
            traceback.print_exc()
        time.sleep(SLEEP_SECONDS)


def start_background_flusher():
    global _running, _thread
    with _lock:
        if _running:
            return
        _running = True
        _thread = threading.Thread(target=_loop, name="mongo-queue-flusher", daemon=True)
        _thread.start()
        log("[Queue] MongoDB queue flusher thread started.")


def stop_background_flusher():
    global _running
    with _lock:
        _running = False
        log("[Queue] MongoDB queue flusher stopped.")
