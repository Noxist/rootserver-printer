#!/usr/bin/env python3
"""
Recover all high quality images and post-conversion full-resolution printed images.
Extracts:
1. Original uncompressed / full-color images from original_images archive.
2. Full native 576px resolution post-conversion bitmaps from MongoDB archive.
3. Cropped photo regions without trailing tear-off margin.
4. Complete JSON manifest and Markdown index.
"""
import os
import io
import json
import time
import shutil
import hashlib
import base64
from pymongo import MongoClient
from PIL import Image

MONGO_URI = os.environ.get("MONGO_URI")
TARGET_BASE = "/data/recovered_images"

os.makedirs(os.path.join(TARGET_BASE, "original"), exist_ok=True)
os.makedirs(os.path.join(TARGET_BASE, "unique_printed"), exist_ok=True)
os.makedirs(os.path.join(TARGET_BASE, "all_printed"), exist_ok=True)
os.makedirs(os.path.join(TARGET_BASE, "cropped_photos"), exist_ok=True)

client = MongoClient(MONGO_URI)
db = client["printer"]

# ----------------- 1. Recover Original Full-Resolution Images -----------------
print("=== Step 1: Recovering Original Full-Resolution Images ===")
orig_dir = os.environ.get("ORIGINAL_IMAGE_DIR", "/data/original_images")
recovered_originals = []

if os.path.isdir(orig_dir):
    for fname in os.listdir(orig_dir):
        if fname == "index.json" or fname.endswith(".tmp"):
            continue
        fpath = os.path.join(orig_dir, fname)
        if os.path.isfile(fpath):
            dest_path = os.path.join(TARGET_BASE, "original", fname)
            shutil.copy2(fpath, dest_path)
            size = os.path.getsize(fpath)
            try:
                with Image.open(fpath) as img:
                    w, h = img.size
                    fmt = img.format or "UNKNOWN"
            except Exception:
                w, h, fmt = 0, 0, "RAW"
            
            recovered_originals.append({
                "filename": fname,
                "path": f"original/{fname}",
                "format": fmt,
                "width": w,
                "height": h,
                "size_bytes": size,
                "type": "original_high_res"
            })
            print(f"Recovered original: {fname} ({fmt}, {w}x{h}, {size} bytes)")

# Also check MongoDB original_images collection for metadata
orig_coll = db["original_images"]
for doc in orig_coll.find():
    fname = doc.get("filename")
    for r in recovered_originals:
        if r["filename"] == fname:
            r["source"] = doc.get("source")
            r["sender"] = doc.get("sender")
            r["original_name"] = doc.get("original_name")
            r["created_iso"] = doc.get("created_iso")

# ----------------- 2. Recover Converted Full-Resolution Images -----------------
print("\n=== Step 2: Recovering Post-Conversion Printed Images from Archive ===")
arch_coll = db["archive"]
all_docs = list(arch_coll.find().sort("ts", 1))

all_printed_recovered = []
unique_printed_recovered = []
seen_hashes = {}

for doc in all_docs:
    jid = doc.get("job_id") or str(doc.get("_id"))
    meta = doc.get("meta") or {}
    source = meta.get("source", "unknown")
    b64 = doc.get("b64", "")
    if not b64:
        continue
    
    raw = base64.b64decode(b64)
    hsh = hashlib.sha256(raw).hexdigest()
    
    try:
        im = Image.open(io.BytesIO(raw))
    except Exception:
        continue
    
    w, h = im.size
    
    # Calculate transition entropy to separate real graphic images from text
    px = list(im.convert("L").getdata())
    transitions = 0
    step = max(1, h // 100)
    sample_rows = range(0, h, step)
    for y in sample_rows:
        row = px[y*w : (y+1)*w]
        for x in range(len(row)-1):
            if (row[x] < 128) != (row[x+1] < 128):
                transitions += 1
    avg_trans = transitions / len(sample_rows)
    
    # Check if this job is an image/photo
    is_test_img = (source == "test_flusher_image") or (w == 576 and h == 120) or ("test" in str(jid) and "ack" in str(jid))
    is_graphic_image = (avg_trans > 40 or "image" in source or len(raw) > 15000) and not is_test_img
    
    if not is_graphic_image:
        continue
    
    ts = doc.get("ts") or doc.get("created_at") or 0
    ts_str = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts)) if ts else "unknown"
    ts_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "unknown"
    
    # Safe name
    safe_jid = "".join(c for c in jid if c.isalnum() or c in ("-", "_"))
    fname_all = f"{ts_str}_{source}_{safe_jid}_{w}x{h}.png"
    dest_all = os.path.join(TARGET_BASE, "all_printed", fname_all)
    with open(dest_all, "wb") as f:
        f.write(raw)
    
    item_info = {
        "job_id": jid,
        "source": source,
        "filename": fname_all,
        "path": f"all_printed/{fname_all}",
        "width": w,
        "height": h,
        "size_bytes": len(raw),
        "sha256": hsh,
        "timestamp": ts,
        "date_iso": ts_iso,
        "avg_trans": round(avg_trans, 1),
        "meta": meta
    }
    all_printed_recovered.append(item_info)
    
    # If first time seeing this exact image hash, record in unique_printed
    if hsh not in seen_hashes:
        seen_hashes[hsh] = []
        uid_idx = len(unique_printed_recovered) + 1
        fname_uniq = f"unique_{uid_idx:02d}_{ts_str}_{source}_{safe_jid}_{w}x{h}.png"
        dest_uniq = os.path.join(TARGET_BASE, "unique_printed", fname_uniq)
        with open(dest_uniq, "wb") as f:
            f.write(raw)
        
        # Also generate cropped photo (auto-trim trailing white feed margin)
        # Find non-white bounding box
        try:
            inv = im.convert("L").point(lambda p: 255 if p < 240 else 0)
            bbox = inv.getbbox()
            if bbox:
                # Add slight 8px margin
                pad = 8
                crop_box = (
                    max(0, bbox[0] - pad),
                    max(0, bbox[1] - pad),
                    min(w, bbox[2] + pad),
                    min(h, bbox[3] + pad)
                )
                cropped = im.crop(crop_box)
                fname_crop = f"crop_{uid_idx:02d}_{ts_str}_{source}_{safe_jid}_{cropped.width}x{cropped.height}.png"
                dest_crop = os.path.join(TARGET_BASE, "cropped_photos", fname_crop)
                cropped.save(dest_crop, format="PNG")
                crop_path = f"cropped_photos/{fname_crop}"
                crop_dims = f"{cropped.width}x{cropped.height}"
            else:
                crop_path = None
                crop_dims = None
        except Exception:
            crop_path = None
            crop_dims = None
        
        uniq_item = dict(item_info)
        uniq_item["filename"] = fname_uniq
        uniq_item["path"] = f"unique_printed/{fname_uniq}"
        uniq_item["crop_path"] = crop_path
        uniq_item["crop_dims"] = crop_dims
        uniq_item["print_count"] = 1
        unique_printed_recovered.append(uniq_item)
    else:
        # Increment print count
        for u in unique_printed_recovered:
            if u["sha256"] == hsh:
                u["print_count"] = u.get("print_count", 1) + 1
                break
    seen_hashes[hsh].append(jid)

print(f"Recovered {len(all_printed_recovered)} full-resolution converted print jobs.")
print(f"Recovered {len(unique_printed_recovered)} distinct unique printed images.")

# ----------------- 3. Generate Manifest & Index -----------------
manifest = {
    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "total_original_images": len(recovered_originals),
    "total_unique_printed_images": len(unique_printed_recovered),
    "total_printed_image_jobs": len(all_printed_recovered),
    "original_images": recovered_originals,
    "unique_printed_images": unique_printed_recovered,
    "all_printed_jobs": all_printed_recovered
}

with open(os.path.join(TARGET_BASE, "manifest.json"), "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2, ensure_ascii=False)

# Generate README.md
md_lines = [
    "# Recovered Thermal Printer Images Archive",
    "",
    f"Archive generated on **{manifest['generated_at']}**.",
    "",
    "## 1. Summary of Recovered Images",
    "",
    f"- **Original Full-Resolution Images (Pre-Conversion / Color)**: {len(recovered_originals)}",
    f"- **Distinct Unique Images Printed (Post-Conversion 576px Bitmaps)**: {len(unique_printed_recovered)}",
    f"- **Total Printed Image Jobs Recovered**: {len(all_printed_recovered)}",
    "",
    "---",
    "",
    "## 2. Original Full-Resolution Images (Pre-Conversion)",
    "",
    "| Filename | Format | Dimensions | Size | Source | Sender | Date |",
    "|---|---|---|---|---|---|---|",
]

for orig in recovered_originals:
    md_lines.append(
        f"| [{orig['filename']}]({orig['path']}) | {orig.get('format')} | {orig.get('width')}x{orig.get('height')} | "
        f"{orig.get('size_bytes')} B | {orig.get('source', '-')} | {orig.get('sender', '-')} | {orig.get('created_iso', '-')} |"
    )

md_lines.extend([
    "",
    "---",
    "",
    "## 3. Distinct Unique Printed Images (Post-Conversion / Full Native 576px Resolution)",
    "",
    "| # | Job ID | Date | Source | Full Dims | Cropped Dims | Prints | Full Image | Cropped Photo |",
    "|---|---|---|---|---|---|---|---|---|",
])

for i, u in enumerate(unique_printed_recovered, 1):
    c_link = f"[{u['crop_dims']}]({u['crop_path']})" if u.get("crop_path") else "-"
    md_lines.append(
        f"| {i} | `{u['job_id']}` | {u['date_iso']} | {u['source']} | {u['width']}x{u['height']} | "
        f"{u.get('crop_dims', '-')} | {u.get('print_count', 1)}x | [Full 576px]({u['path']}) | {c_link} |"
    )

with open(os.path.join(TARGET_BASE, "README.md"), "w", encoding="utf-8") as f:
    f.write("\n".join(md_lines))

print("\n=== Recovery Successfully Completed! ===")
