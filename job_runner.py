#!/usr/bin/env python3
"""
Reusable scrape-job launcher.

Builds the run_facebook.py CLI command (with the workspace's single-brand
config), creates a job record, and starts it in a background thread. Used by
both the manual "Run Bot" form (app.py) and the scheduled source scanner
(scheduler.py) so there is a single pipeline entry point. ONE job does
everything — discovery, image download/processing/analysis, and publishing;
no sub-jobs are launched.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

import brand_mapping
from data_store import (
    BASE_DIR, DATA_DIR, OUTPUT_DIR, append_log, load_jobs, save_jobs, update_job,
    load_workspaces, update_workspace, normalize_workspace, PUBLISH_TARGETS, VALID_CATEGORIES,
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


def run_subprocess_job(job_id: str, workspace_id: str, cmd: list, env: dict,
                       temp_files: list[Path], on_success=None):
    """Run the pipeline subprocess in a background daemon thread and stream
    its output into the job's log. `on_success(job_id)` runs after a
    successful exit (exit code 0)."""
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
            update_job(job_id, {"status": "completed", "finished_at": datetime.now().isoformat()})
            append_log(job_id, f"[{ts}] ✅ Pipeline completed successfully")
            if on_success:
                try:
                    on_success(job_id)
                except Exception as exc:
                    append_log(job_id, f"[{ts}] ⚠️  Post-job hook failed: {exc}")
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


def _finish_scrape_job(job_id: str, workspace_id: str):
    """Post-success work for a pipeline job: fold its post_count (from
    manifest.json) into the job record and merge processed post IDs into the
    workspace's dedup state."""
    manifest_file = OUTPUT_DIR / job_id / "manifest.json"
    if manifest_file.exists():
        try:
            post_count = json.loads(manifest_file.read_text(encoding="utf-8")).get("post_count")
            update_job(job_id, {"post_count": post_count})
        except Exception:
            pass
    _merge_manifest_into_workspace(job_id, workspace_id)


# ── Global job queue ──
# Only ONE pipeline subprocess runs at a time across the whole app — a job
# launched while another is still running waits in this queue ("queued"
# status) and starts when the previous one finishes. Every job is a separate
# process with its own KIE rate limiter, so running two concurrently could
# collectively blow KIE's ~20-requests/10s account cap (429s); serializing
# them keeps every job inside the shared account budget.
_job_queue: "queue.Queue" = queue.Queue()
_queue_worker_started = False


def _queue_worker():
    while True:
        job_id, ws_id, cmd, env, temp_files, on_success = _job_queue.get()
        try:
            append_log(job_id, f"[{datetime.now().strftime('%H:%M:%S')}] ▶️ Job started (was queued)")
            run_subprocess_job(job_id, ws_id, cmd, env, temp_files, on_success=on_success)
        except Exception as exc:
            append_log(job_id, f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Queue worker error: {exc}")
        finally:
            _job_queue.task_done()


def _enqueue_job(job_id: str, ws_id: str, cmd: list, env: dict,
                 temp_files: list, on_success=None):
    """Queue a pipeline job and make sure the single worker thread is
    running. Jobs execute strictly one at a time, in launch order."""
    global _queue_worker_started
    _job_queue.put((job_id, ws_id, cmd, env, temp_files, on_success))
    if not _queue_worker_started:
        threading.Thread(target=_queue_worker, daemon=True, name="job-queue-worker").start()
        _queue_worker_started = True


def launch_scrape_job(
    ws: dict,
    sources: list[dict],
    start_date: str | None = None,
    end_date: str | None = None,
    min_comments: int = 0,
    triggered_by: str = "manual",
    skip_post_ids: list[str] | None = None,
    brand_post_limit: int = 0,
) -> str:
    """Create a job record for `ws` and start run_facebook.py in the
    background. ONE job does everything: discovery → image
    download/processing/analysis → publish prep → publishing, all for the
    workspace's single brand.

    `sources` is a list of {"url": str, "type": "page"|"group"|"post"|None}.
    Returns the new job_id. Shared by the manual "Run Bot" form and the
    scheduled source scanner — this is the single pipeline entry point (do
    not duplicate this logic elsewhere).
    """
    job_id = str(uuid.uuid4())

    # Defensive: callers normally pass a workspace from load_workspaces()
    # (already normalized), but a hand-edited record or legacy brands[] dict
    # gets normalized here too so the single-brand fields always exist.
    ws = normalize_workspace(ws)

    brand = (ws.get("brand") or "").strip() or None
    brand_slug = brand_mapping.brand_slug(brand) if brand else None
    # Per-category publish destinations — food and non-food each get their own
    # website + WordPress page, used only when that category is selected.
    food_target = ws.get("food_publish_target") if ws.get("food_publish_target") in PUBLISH_TARGETS else "retailshout"
    food_page_id = (ws.get("food_page_id") or "").strip() or None
    non_food_target = ws.get("non_food_publish_target") if ws.get("non_food_publish_target") in PUBLISH_TARGETS else "retailshout"
    non_food_page_id = (ws.get("non_food_page_id") or "").strip() or None
    categories = [c for c in (ws.get("categories") or []) if c in VALID_CATEGORIES] or list(VALID_CATEGORIES)
    image_prompt = (ws.get("image_prompt") or "").strip() or None
    page_title = (ws.get("page_title") or "").strip() or None
    week_start = (ws.get("week_start") or "").strip().lower() or None

    job = {
        "id": job_id,
        "workspace_id": ws["id"],
        "workspace_name": ws["name"],
        "job_kind": "scrape",
        "brand": brand,
        "brand_slug": brand_slug,
        "food_publish_target": food_target,
        "food_page_id": food_page_id,
        "non_food_publish_target": non_food_target,
        "non_food_page_id": non_food_page_id,
        "categories": categories,
        "sources": sources,
        "start_date": start_date,
        "end_date": end_date,
        "min_comments": min_comments,
        "brand_post_limit": brand_post_limit or 0,
        "status": "queued",
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
        "--food-publish-target", food_target,
        "--non-food-publish-target", non_food_target,
        "--categories", ",".join(categories),
    ]
    if brand:
        cmd += ["--brand", brand]
    if food_page_id:
        cmd += ["--food-page-id", food_page_id]
    if non_food_page_id:
        cmd += ["--non-food-page-id", non_food_page_id]
    if image_prompt:
        cmd += ["--image-prompt", image_prompt]
    if page_title:
        cmd += ["--page-title", page_title]
    if week_start:
        cmd += ["--week-start", week_start]
    if brand_post_limit and brand_post_limit > 0:
        cmd += ["--brand-post-limit", str(brand_post_limit)]
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
    append_log(job_id, f"[{ts}] Brand         : {brand or '(any single-detected brand)'}")
    append_log(job_id, f"[{ts}] Categories    : {', '.join(categories)}")
    append_log(job_id, f"[{ts}] Publishing    : Food → {food_target}{f' (page {food_page_id})' if food_page_id else ' — not configured (skipped)'} | "
                        f"Non-Food → {non_food_target}{f' (page {non_food_page_id})' if non_food_page_id else ' — not configured (skipped)'}")
    append_log(job_id, f"[{ts}] Min comments  : {min_comments or 0}")
    if brand_post_limit and brand_post_limit > 0:
        append_log(job_id, f"[{ts}] Post limit    : max {brand_post_limit} post(s) (testing)")
    append_log(job_id, f"[{ts}] Date window   : {start_date or 'open'} → {end_date or 'today'}")
    append_log(job_id, f"[{ts}] Sources ({len(sources)}):")
    for s in sources:
        append_log(job_id, f"[{ts}]   [{s.get('type') or 'auto'}] {s.get('url')}")
    append_log(job_id, f"[{ts}] ────────────────────────────────────────────────────")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    # Queue the job — it runs when the currently-running pipeline job (if
    # any) finishes, keeping every job inside the shared KIE account limits.
    update_job(job_id, {"status": "queued"})
    _enqueue_job(
        job_id, ws["id"], cmd, env, temp_files,
        on_success=lambda jid: _finish_scrape_job(jid, ws["id"]),
    )

    return job_id
