#!/usr/bin/env python3
"""
Build a downloadable ZIP of a job's processed output, and keep the exports/
directory from accumulating stale files indefinitely.
"""

import json
import time
import zipfile
from pathlib import Path

from data_store import OUTPUT_DIR, EXPORTS_DIR

ZIP_MAX_AGE_SECONDS = 24 * 3600  # stale exports get swept on every new build


def cleanup_old_zips(max_age: int = ZIP_MAX_AGE_SECONDS):
    now = time.time()
    for f in EXPORTS_DIR.glob("*.zip*"):
        try:
            if now - f.stat().st_mtime > max_age:
                f.unlink()
        except OSError:
            pass


def _dir_mtime(path: Path) -> float:
    latest = path.stat().st_mtime
    for p in path.rglob("*"):
        try:
            latest = max(latest, p.stat().st_mtime)
        except OSError:
            pass
    return latest


def build_job_zip(job_id: str) -> str:
    """Build (or reuse an up-to-date) ZIP for a job's output directory.

    Layout:
        posts/post_<id>.json
        images/post_<id>_<original filename>
        image_analysis_mapping.json

    Processed (grayscale/compressed) images are intentionally left out —
    they're an intermediate artifact for the AI call, not something the
    export needs; only originals + analysis results are included.

    Returns the zip file path.
    """
    cleanup_old_zips()

    job_dir = OUTPUT_DIR / job_id
    if not job_dir.exists():
        raise FileNotFoundError(f"No output directory for job {job_id}")

    zip_path = EXPORTS_DIR / f"facebook_export_{job_id}.zip"
    if zip_path.exists() and zip_path.stat().st_mtime >= _dir_mtime(job_dir):
        return str(zip_path)

    tmp_zip_path = zip_path.with_suffix(".zip.tmp")
    mapping: dict = {}

    with zipfile.ZipFile(tmp_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for post_dir in sorted(job_dir.iterdir()):
            if not post_dir.is_dir() or not post_dir.name.startswith("post_"):
                continue
            post_id = post_dir.name[len("post_"):]

            post_json = post_dir / "post.json"
            if post_json.exists():
                zf.write(post_json, f"posts/post_{post_id}.json")

            orig_dir = post_dir / "images" / "original"
            if orig_dir.exists():
                for img in sorted(orig_dir.iterdir()):
                    if img.is_file():
                        zf.write(img, f"images/post_{post_id}_{img.name}")

            analysis_json = post_dir / "analysis" / "image_analysis.json"
            if analysis_json.exists():
                try:
                    mapping.update(json.loads(analysis_json.read_text(encoding="utf-8")))
                except Exception:
                    pass

        job_mapping_file = job_dir / "image_analysis_mapping.json"
        if job_mapping_file.exists():
            zf.writestr("image_analysis_mapping.json", job_mapping_file.read_text(encoding="utf-8"))
        elif mapping:
            zf.writestr("image_analysis_mapping.json", json.dumps(mapping, indent=2, ensure_ascii=False))

    tmp_zip_path.replace(zip_path)
    return str(zip_path)
