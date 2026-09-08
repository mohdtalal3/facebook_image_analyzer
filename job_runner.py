#!/usr/bin/env python3
"""
Reusable scrape-job launcher.

Builds the run_facebook.py CLI command, creates a job record, and starts it
in a background thread. Used by both the manual "Run Bot" form (app.py) and
the scheduled source scanner (scheduler.py) so there is a single scraping
pipeline entry point.
"""

import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from data_store import (
    BASE_DIR, DATA_DIR, OUTPUT_DIR, append_log, load_jobs, save_jobs, update_job,
    load_workspaces, update_workspace,
)


def _merge_manifest_into_workspace(job_id: str, workspace_id: str):
    """After a job finishes, fold the post IDs it processed into the
    workspace's schedule_state so the next scheduled/manual run skips them
    (cross-run dedup)."""
    manifest_file = OUTPUT_DIR / job_id / "manifest.json"
    if not manifest_file.exists():
        return
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception:
        return

    new_ids = manifest.get("processed_post_ids") or []
    ws = next((w for w in load_workspaces() if w["id"] == workspace_id), None)
    if not ws:
        return

    state = ws.get("schedule_state") or {}
    existing = set(state.get("processed_post_ids") or [])
    merged = list(existing | set(new_ids))[-5000:]  # cap unbounded growth

    update_workspace(workspace_id, {
        "schedule_state": {
            **state,
            "processed_post_ids": merged,
            "last_job_id": job_id,
            "last_matched_count": len(new_ids),
        },
    })


def run_job_background(job_id: str, workspace_id: str, cmd: list, env: dict, temp_files: list[Path]):
    """Run the pipeline subprocess in a background daemon thread."""
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            cwd=str(BASE_DIR),
        )

        update_job(job_id, {"pid": process.pid, "status": "running"})

        for line in iter(process.stdout.readline, ""):
            stripped = line.rstrip()
            if stripped:
                ts = datetime.now().strftime("%H:%M:%S")
                append_log(job_id, f"[{ts}] {stripped}")

        process.wait()
        ts = datetime.now().strftime("%H:%M:%S")

        if process.returncode == 0:
            manifest_file = OUTPUT_DIR / job_id / "manifest.json"
            post_count = None
            if manifest_file.exists():
                try:
                    post_count = json.loads(manifest_file.read_text(encoding="utf-8")).get("post_count")
                except Exception:
                    pass
            update_job(job_id, {"status": "completed", "finished_at": datetime.now().isoformat(), "post_count": post_count})
            append_log(job_id, f"[{ts}] ✅ Pipeline completed successfully")
            _merge_manifest_into_workspace(job_id, workspace_id)
        else:
            update_job(job_id, {"status": "failed", "finished_at": datetime.now().isoformat()})
            append_log(job_id, f"[{ts}] ❌ Pipeline failed (exit code {process.returncode})")

    except Exception as exc:
        ts = datetime.now().strftime("%H:%M:%S")
        update_job(job_id, {"status": "failed", "finished_at": datetime.now().isoformat()})
        append_log(job_id, f"[{ts}] ❌ Exception: {exc}")
    finally:
        for f in temp_files:
            if f and f.exists():
                f.unlink()


def launch_scrape_job(
    ws: dict,
    sources: list[dict],
    start_date: str | None = None,
    end_date: str | None = None,
    min_comments: int = 0,
    triggered_by: str = "manual",
    skip_post_ids: list[str] | None = None,
) -> str:
    """Create a job record for `ws` and start run_facebook.py in the background.

    `sources` is a list of {"url": str, "type": "page"|"group"|"post"|None}.
    Returns the new job_id. Shared by the manual "Run Bot" form and the
    scheduled source scanner — this is the single scraping pipeline entry
    point (do not duplicate this logic elsewhere).
    """
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "workspace_id": ws["id"],
        "workspace_name": ws["name"],
        "sources": sources,
        "start_date": start_date,
        "end_date": end_date,
        "min_comments": min_comments,
        "status": "pending",
        "triggered_by": triggered_by,
        "created_at": datetime.now().isoformat(),
        "finished_at": None,
        "pid": None,
        "output_dir": f"output/{job_id}",
        "post_count": None,
    }
    jobs = load_jobs()
    jobs.append(job)
    save_jobs(jobs)

    # ── Write per-job temp input files ──
    sources_file = DATA_DIR / f"sources_{job_id}.json"
    sources_file.write_text(json.dumps(sources, ensure_ascii=False), encoding="utf-8")
    temp_files = [sources_file]

    cmd = [
        sys.executable, str(BASE_DIR / "run_facebook.py"),
        "--job-id", job_id,
        "--sources-file", str(sources_file),
        "--min-comments", str(min_comments or 0),
    ]
    if start_date:
        cmd += ["--start-date", start_date]
    if end_date:
        cmd += ["--end-date", end_date]

    if skip_post_ids:
        skip_file = DATA_DIR / f"skip_{job_id}.json"
        skip_file.write_text(json.dumps(list(skip_post_ids)), encoding="utf-8")
        temp_files.append(skip_file)
        cmd += ["--skip-post-ids-file", str(skip_file)]

    # ── Write startup log entries ──
    ts = datetime.now().strftime("%H:%M:%S")
    append_log(job_id, f"[{ts}] ── Job {job_id[:8]}... created ({triggered_by}) ──────────────")
    append_log(job_id, f"[{ts}] Workspace     : {ws['name']}")
    append_log(job_id, f"[{ts}] Min comments  : {min_comments or 0}")
    append_log(job_id, f"[{ts}] Date window   : {start_date or 'open'} → {end_date or 'today'}")
    append_log(job_id, f"[{ts}] Sources ({len(sources)}):")
    for s in sources:
        append_log(job_id, f"[{ts}]   [{s.get('type') or 'auto'}] {s.get('url')}")
    append_log(job_id, f"[{ts}] ────────────────────────────────────────────────────")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    t = threading.Thread(target=run_job_background, args=(job_id, ws["id"], cmd, env, temp_files), daemon=True)
    t.start()

    return job_id
