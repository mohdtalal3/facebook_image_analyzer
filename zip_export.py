#!/usr/bin/env python3
"""
Build a downloadable ZIP of a job's processed output, and keep the exports/
directory from accumulating stale files indefinitely.
"""

import time
import zipfile
from pathlib import Path

from data_store import OUTPUT_DIR, EXPORTS_DIR
from constants import ZIP_MAX_AGE_SECONDS


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

    Mirrors output/<job_id>/ 1:1 (see CONTEXT.md "Output Folder Structure"):
        <brand>/<category>/images/post_<id>_<filename>          # flat, for quick browsing
        <brand>/<category>/image_analysis.json                   # combined, for quick browsing
        <brand>/<category>/post_<id>/post.json                   # full per-post detail, for debugging
        <brand>/<category>/post_<id>/images/original/<filename>
        <brand>/<category>/post_<id>/analysis/image_analysis.json
        image_analysis_mapping.json

    Excludes _staging/ (should be empty on a clean run anyway) and every
    images/processed/ folder — grayscale/compressed intermediates for the AI
    call, not something the export needs.

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

    with zipfile.ZipFile(tmp_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(job_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(job_dir)
            if rel.parts[0] == "_staging" or "processed" in rel.parts:
                continue
            zf.write(path, str(rel))

    tmp_zip_path.replace(zip_path)
    return str(zip_path)
