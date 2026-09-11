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

import brand_mapping
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


def run_subprocess_job(job_id: str, workspace_id: str, cmd: list, env: dict,
                       temp_files: list[Path], on_success=None):
    """Run a pipeline subprocess in a background daemon thread and stream its
    output into the job's log. Shared by scrape jobs (run_facebook.py) and
    publish jobs (publish_wordpress.py). `on_success(job_id)` runs after a
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
    """Post-success work for a discovery (scrape) job: fold its post_count
    (from manifest.json) into the job record, merge processed post IDs into
    the workspace's dedup state, and launch one brand sub-workflow job per
    brand that has posts in its manifest."""
    manifest_file = OUTPUT_DIR / job_id / "manifest.json"
    if manifest_file.exists():
        try:
            post_count = json.loads(manifest_file.read_text(encoding="utf-8")).get("post_count")
            update_job(job_id, {"post_count": post_count})
        except Exception:
            pass
    _merge_manifest_into_workspace(job_id, workspace_id)
    _launch_brand_jobs(job_id, workspace_id)


def _launch_brand_jobs(parent_job_id: str, workspace_id: str):
    """Sub-workflow launcher: after the discovery job completes, create +
    launch one brand job per brand that has posts in its manifest
    (output/<parent_job_id>/<brand-slug>/posts.json). Each brand job does
    the heavy work — extra-image discovery via last_media_id, download,
    process, KIE analysis, organize, and (when the brand has a WordPress
    page ID) publishing — as its own first-class job record with its own
    logs, running in parallel with the other brands' jobs."""
    ws = next((w for w in load_workspaces() if w["id"] == workspace_id), None)
    if not ws:
        return

    brand_configs = ws.get("brands") or []
    pending: list[tuple] = []   # (brand, brand_slug, brand_config, page_id, post_count)
    if brand_configs:
        for brand_config in brand_configs:
            brand = brand_config.get("brand")
            if not brand:
                continue
            brand_slug = brand_mapping.brand_slug(brand)
            posts = _read_brand_manifest(parent_job_id, brand_slug)
            if not posts:
                append_log(parent_job_id, f"[brand] {brand}: no posts matched this brand — skipping brand job")
                continue
            pending.append((brand, brand_slug, brand_config,
                            (brand_config.get("page_id") or "").strip() or None, len(posts)))
    else:
        # No brands configured on the workspace: launch a processing job per
        # brand folder the discovery phase produced (publishing disabled —
        # there's no page ID to publish to).
        for posts_file in sorted(OUTPUT_DIR.glob(f"{parent_job_id}/*/posts.json")):
            brand_slug = posts_file.parent.name
            try:
                count = len(json.loads(posts_file.read_text(encoding="utf-8")).get("posts") or [])
            except Exception:
                count = 0
            if not count:
                continue
            pending.append((brand_slug, brand_slug, {"publish_target": "retailshout"}, None, count))

    # KIE's account-wide rate budget is enforced by kie_vision's shared
    # file-based limiter (data/kie_rate_window.json) — every concurrent
    # KIE-calling process draws from the same window, so no per-job split is
    # needed and a finished job's capacity is automatically freed.
    for brand, brand_slug, brand_config, page_id, count in pending:
        launch_brand_job(ws, parent_job_id, brand, brand_slug, brand_config,
                         page_id, count)


def _read_brand_manifest(parent_job_id: str, brand_slug: str):
    posts_file = OUTPUT_DIR / parent_job_id / brand_slug / "posts.json"
    if not posts_file.exists():
        return []
    try:
        return json.loads(posts_file.read_text(encoding="utf-8")).get("posts") or []
    except Exception:
        return []


def launch_brand_job(ws: dict, parent_job_id: str, brand: str, brand_slug: str,
                     brand_config: dict, page_id: str | None, post_count: int) -> str:
    """Create a brand-job record for one brand and start run_brand_job.py in
    the background. The brand job does the heavy work — extra-image
    discovery (last_media_id walk), download, process, KIE analysis,
    organize, and WordPress publish — all under the parent job's output dir,
    with its own log."""
    job_id = str(uuid.uuid4())
    publish_target = brand_config.get("publish_target") or "retailshout"
    image_prompt = (brand_config.get("image_prompt") or "").strip() or None
    job = {
        "id": job_id,
        "workspace_id": ws["id"],
        "workspace_name": ws["name"],
        "job_kind": "brand",
        "parent_job_id": parent_job_id,
        "brand": brand,
        "brand_slug": brand_slug,
        "publish_target": publish_target,
        "page_id": page_id,
        "sources": [],
        "start_date": None,
        "end_date": None,
        "min_comments": 0,
        "status": "pending",
        "triggered_by": "brand",
        "created_at": datetime.now().isoformat(),
        "finished_at": None,
        "pid": None,
        "output_dir": f"output/{parent_job_id}",
        "post_count": post_count,
    }
    jobs = load_jobs()
    jobs.append(job)
    save_jobs(jobs)

    cmd = [
        sys.executable, str(BASE_DIR / "run_brand_job.py"),
        "--job-id", job_id,
        "--parent-job-id", parent_job_id,
        "--brand", brand,
        "--brand-slug", brand_slug,
        "--publish-target", publish_target,
    ]
    if page_id:
        cmd += ["--page-id", page_id]
    if image_prompt:
        cmd += ["--image-prompt", image_prompt]
    page_title = (brand_config.get("page_title") or "").strip() or None
    if page_title:
        cmd += ["--page-title", page_title]
    week_start = (brand_config.get("week_start") or "").strip().lower() or None
    if week_start:
        cmd += ["--week-start", week_start]

    ts = datetime.now().strftime("%H:%M:%S")
    append_log(job_id, f"[{ts}] ── Brand job {job_id[:8]}... created ────────────────────")
    append_log(job_id, f"[{ts}] Workspace     : {ws['name']}")
    append_log(job_id, f"[{ts}] Brand         : {brand}")
    append_log(job_id, f"[{ts}] Posts to process: {post_count}")
    append_log(job_id, f"[{ts}] Publish target: {publish_target}{' | WP page ' + page_id if page_id else ' | publishing disabled (no page ID)'}")
    append_log(job_id, f"[{ts}] Source job    : {parent_job_id}")
    append_log(job_id, f"[{ts}] ────────────────────────────────────────────────────")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    t = threading.Thread(
        target=run_subprocess_job,
        args=(job_id, ws["id"], cmd, env, []),
        daemon=True,
    )
    t.start()

    return job_id


def run_job_background(job_id: str, workspace_id: str, cmd: list, env: dict, temp_files: list[Path]):
    """Run the scrape pipeline subprocess in a background daemon thread."""
    run_subprocess_job(job_id, workspace_id, cmd, env, temp_files,
                       on_success=lambda jid: _finish_scrape_job(jid, workspace_id))


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
        "job_kind": "scrape",
        "sources": sources,
        "start_date": start_date,
        "end_date": end_date,
        "min_comments": min_comments,
        "brand_post_limit": brand_post_limit or 0,
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

    brand_configs = ws.get("brands") or []
    brands_file = None
    if brand_configs:
        brands_file = DATA_DIR / f"brands_{job_id}.json"
        brands_file.write_text(json.dumps(brand_configs, ensure_ascii=False), encoding="utf-8")
        temp_files.append(brands_file)

    cmd = [
        sys.executable, str(BASE_DIR / "run_facebook.py"),
        "--job-id", job_id,
        "--sources-file", str(sources_file),
        "--min-comments", str(min_comments or 0),
        "--phase", "discover",
    ]
    if brands_file:
        cmd += ["--brands-file", str(brands_file)]
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
    if brand_configs:
        append_log(job_id, f"[{ts}] Brands        : {', '.join(b.get('brand', '?') for b in brand_configs)}")
    append_log(job_id, f"[{ts}] Min comments  : {min_comments or 0}")
    if brand_post_limit and brand_post_limit > 0:
        append_log(job_id, f"[{ts}] Brand limit   : max {brand_post_limit} post(s) per brand (testing)")
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
