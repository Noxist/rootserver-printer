# image_archive.py
"""
Rolling archive for original quality, full-color images (last 250 images).
Saves original uploaded bytes or PIL Image without dithering or monochrome conversion.
Maintains rolling buffer of max 250 images with metadata in MongoDB and local index.
"""
from __future__ import annotations

import os
import io
import time
import json
import secrets
from typing import Optional, List, Dict, Any, Union
from PIL import Image

try:
    from pymongo import MongoClient, DESCENDING
except ImportError:
    MongoClient = None
    DESCENDING = None

ORIGINAL_IMAGE_DIR = os.getenv("ORIGINAL_IMAGE_DIR", "/data/original_images")
MAX_ARCHIVED_IMAGES = int(os.getenv("MAX_ARCHIVED_IMAGES", "250"))

_cached_mongo_client = None

def _get_mongo_coll():
    global _cached_mongo_client
    backend = os.getenv("GUEST_DB_BACKEND", "file").lower()
    uri = os.getenv("MONGO_URI")
    if backend == "mongo" and uri and MongoClient is not None:
        try:
            if _cached_mongo_client is None:
                _cached_mongo_client = MongoClient(uri, serverSelectionTimeoutMS=2000)
                db_name = os.getenv("MONGO_DB", "printer")
                coll = _cached_mongo_client[db_name]["original_images"]
                coll.create_index([("created_at", -1)])
            else:
                db_name = os.getenv("MONGO_DB", "printer")
                coll = _cached_mongo_client[db_name]["original_images"]
            return coll
        except Exception:
            return None
    return None

def _ensure_dir():
    os.makedirs(ORIGINAL_IMAGE_DIR, exist_ok=True)

def _get_local_index() -> Dict[str, Any]:
    _ensure_dir()
    index_path = os.path.join(ORIGINAL_IMAGE_DIR, "index.json")
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"images": []}

def _save_local_index(data: Dict[str, Any]):
    _ensure_dir()
    index_path = os.path.join(ORIGINAL_IMAGE_DIR, "index.json")
    tmp = index_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, index_path)

def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"

def save_original_image(
    image_input: Union[bytes, bytearray, Image.Image],
    filename_hint: Optional[str] = None,
    source: str = "unknown",
    sender: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Saves the original, full-color, un-dithered image to the rolling archive (max 250 images).
    Preserves exact original bytes when bytes/bytearray is passed.
    """
    _ensure_dir()
    ts = int(time.time())
    ts_str = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts))
    rand_hex = secrets.token_hex(4)

    raw_bytes: bytes
    img_format = "PNG"
    width = 0
    height = 0

    if isinstance(image_input, (bytes, bytearray)):
        raw_bytes = bytes(image_input)
        try:
            with Image.open(io.BytesIO(raw_bytes)) as pil_img:
                width, height = pil_img.size
                img_format = pil_img.format or "PNG"
        except Exception as e:
            print(f"[archive] Warning: Could not parse image header: {e}", flush=True)
            return None
    elif isinstance(image_input, Image.Image):
        width, height = image_input.size
        img_format = getattr(image_input, "format", None) or "PNG"
        buf = io.BytesIO()
        # Save in full original quality and color
        save_fmt = img_format.upper()
        if save_fmt in ("JPEG", "JPG"):
            # Convert RGBA to RGB for JPEG if needed
            if image_input.mode in ("RGBA", "LA", "P"):
                rgb_img = image_input.convert("RGB")
                rgb_img.save(buf, format="JPEG", quality=95)
            else:
                image_input.save(buf, format="JPEG", quality=95)
        elif save_fmt == "WEBP":
            image_input.save(buf, format="WEBP", quality=95)
        else:
            image_input.save(buf, format="PNG")
        raw_bytes = buf.getvalue()
    else:
        return None

    # Determine extension
    ext = img_format.lower()
    if ext == "jpeg":
        ext = "jpg"
    if not ext or len(ext) > 5:
        ext = "png"

    safe_hint = ""
    if filename_hint:
        base = os.path.splitext(os.path.basename(filename_hint))[0]
        safe_hint = "_" + "".join(c for c in base if c.isalnum() or c in ("-", "_"))[:20]

    filename = f"{ts_str}_{rand_hex}{safe_hint}.{ext}"
    file_path = os.path.join(ORIGINAL_IMAGE_DIR, filename)

    try:
        with open(file_path, "wb") as f:
            f.write(raw_bytes)
    except Exception as exc:
        print(f"[archive] Failed to write image file {file_path}: {exc}", flush=True)
        return None

    size_bytes = len(raw_bytes)
    record = {
        "filename": filename,
        "original_name": filename_hint or filename,
        "format": img_format.upper(),
        "width": width,
        "height": height,
        "size_bytes": size_bytes,
        "size_human": _format_size(size_bytes),
        "created_at": ts,
        "created_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
        "source": source,
        "sender": sender or "Unknown",
        "url": f"/images/original/{filename}",
        "metadata": metadata or {},
    }

    # Store in MongoDB if available
    coll = _get_mongo_coll()
    if coll is not None:
        try:
            coll.insert_one(dict(record, _id=filename))
        except Exception as e:
            print(f"[archive] Mongo insert failed: {e}", flush=True)

    # Store in local index
    try:
        local_idx = _get_local_index()
        local_idx["images"].insert(0, record)
        # Prune local index if needed
        local_idx["images"] = local_idx["images"][: MAX_ARCHIVED_IMAGES * 2]
        _save_local_index(local_idx)
    except Exception as e:
        print(f"[archive] Local index save failed: {e}", flush=True)

    # Prune to enforce strictly <= MAX_ARCHIVED_IMAGES
    _prune_archive()

    print(f"[archive] Saved original image: {filename} ({width}x{height}, {record['size_human']}, sender={sender})", flush=True)
    return record


def _prune_archive():
    """Prunes image archive to keep only the newest MAX_ARCHIVED_IMAGES."""
    _ensure_dir()
    coll = _get_mongo_coll()

    retained_filenames = set()

    if coll is not None:
        try:
            cursor = coll.find().sort("created_at", -1)
            all_docs = list(cursor)
            if len(all_docs) > MAX_ARCHIVED_IMAGES:
                to_delete = all_docs[MAX_ARCHIVED_IMAGES:]
                for doc in to_delete:
                    fname = doc.get("filename")
                    if fname:
                        fpath = os.path.join(ORIGINAL_IMAGE_DIR, fname)
                        if os.path.exists(fpath):
                            try:
                                os.remove(fpath)
                            except Exception:
                                pass
                        coll.delete_one({"_id": doc["_id"]})
                all_docs = all_docs[:MAX_ARCHIVED_IMAGES]
            retained_filenames = {d.get("filename") for d in all_docs if d.get("filename")}
        except Exception as e:
            print(f"[archive] Pruning Mongo error: {e}", flush=True)

    # Also synchronize local index & disk directory
    local_idx = _get_local_index()
    images = local_idx.get("images", [])

    # Keep only files that exist on disk
    valid_images = []
    for img in images:
        fpath = os.path.join(ORIGINAL_IMAGE_DIR, img["filename"])
        if os.path.exists(fpath):
            valid_images.append(img)

    # Sort newest first
    valid_images.sort(key=lambda x: x.get("created_at", 0), reverse=True)

    if len(valid_images) > MAX_ARCHIVED_IMAGES:
        for img in valid_images[MAX_ARCHIVED_IMAGES:]:
            fpath = os.path.join(ORIGINAL_IMAGE_DIR, img["filename"])
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except Exception:
                    pass
        valid_images = valid_images[:MAX_ARCHIVED_IMAGES]

    local_idx["images"] = valid_images
    _save_local_index(local_idx)

    # Disk scan fallback: clean up any files on disk not in top 250
    try:
        all_disk_files = [
            f for f in os.listdir(ORIGINAL_IMAGE_DIR)
            if f != "index.json" and not f.endswith(".tmp") and os.path.isfile(os.path.join(ORIGINAL_IMAGE_DIR, f))
        ]
        if len(all_disk_files) > MAX_ARCHIVED_IMAGES:
            # Sort by mtime descending
            all_disk_files.sort(
                key=lambda f: os.path.getmtime(os.path.join(ORIGINAL_IMAGE_DIR, f)),
                reverse=True
            )
            for old_file in all_disk_files[MAX_ARCHIVED_IMAGES:]:
                try:
                    os.remove(os.path.join(ORIGINAL_IMAGE_DIR, old_file))
                except Exception:
                    pass
    except Exception as e:
        print(f"[archive] Disk cleanup scan error: {e}", flush=True)


def list_archived_images(limit: int = MAX_ARCHIVED_IMAGES) -> List[Dict[str, Any]]:
    """Returns the list of up to `limit` most recent archived original images."""
    coll = _get_mongo_coll()
    if coll is not None:
        try:
            docs = list(coll.find().sort("created_at", -1).limit(limit))
            out = []
            for d in docs:
                item = dict(d)
                if "_id" in item:
                    del item["_id"]
                out.append(item)
            if out:
                return out
        except Exception:
            pass

    # Fallback to local index
    local_idx = _get_local_index()
    images = local_idx.get("images", [])
    return images[:limit]


RECOVERED_IMAGE_DIR = os.getenv("RECOVERED_IMAGE_DIR", "/data/recovered_images")


def get_image_file_path(filename: str, category: Optional[str] = None) -> Optional[str]:
    """Safe path resolver that prevents path traversal attacks across original and recovered directories."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    # 1. Check ORIGINAL_IMAGE_DIR
    fpath = os.path.join(ORIGINAL_IMAGE_DIR, filename)
    if os.path.isfile(fpath):
        return fpath
    # 2. Check RECOVERED_IMAGE_DIR categories
    cats = [category] if category else ["original", "unique_printed", "cropped_photos", "all_printed"]
    for c in cats:
        if not c:
            continue
        p = os.path.join(RECOVERED_IMAGE_DIR, c, filename)
        if os.path.isfile(p):
            return p
    return None


def list_recovered_images(category: str = "unique_printed") -> List[Dict[str, Any]]:
    """Returns list of recovered images from manifest."""
    manifest_path = os.path.join(RECOVERED_IMAGE_DIR, "manifest.json")
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if category == "unique_printed":
                    return data.get("unique_printed_images", [])
                elif category == "original":
                    return data.get("original_images", [])
                elif category == "all_printed":
                    return data.get("all_printed_jobs", [])
        except Exception:
            pass
    return []


def delete_archived_image(filename: str) -> bool:
    """Deletes a specific archived image by filename."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return False
    fpath = os.path.join(ORIGINAL_IMAGE_DIR, filename)
    deleted = False
    if os.path.exists(fpath):
        try:
            os.remove(fpath)
            deleted = True
        except Exception:
            pass

    coll = _get_mongo_coll()
    if coll is not None:
        try:
            coll.delete_one({"_id": filename})
            deleted = True
        except Exception:
            pass

    local_idx = _get_local_index()
    orig_len = len(local_idx.get("images", []))
    local_idx["images"] = [img for img in local_idx.get("images", []) if img.get("filename") != filename]
    if len(local_idx["images"]) < orig_len:
        deleted = True
        _save_local_index(local_idx)

    return deleted
