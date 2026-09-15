#!/usr/bin/env python3
"""
Source schedule watcher.

Runs a lightweight APScheduler job every second that only re-reads
workspaces.json and (re)registers a per-workspace CronTrigger job whenever
that workspace's schedule config changed. Facebook itself is NEVER touched
by this per-second tick — it is only touched by the CronTrigger job when
the configured day/time is actually due (or via "Run Now").

Each workspace's schedule config lives in:
    workspace["schedule"] = {
        "enabled": bool,
        "day": "saturday",
        "time": "08:00",
        "timezone": "Asia/Karachi",
        "start_date": "YYYY-MM-DD" | None,   # fixed window start; None = rolling from last run
        "min_comments": int,
    }
    workspace["fb_sources"] = ["https://www.facebook.com/examplepage", ...]
    workspace["schedule_state"] = {
        "last_run_at": iso str,
        "last_run_status": "launched" | "no_sources" | "error",
        "last_matched_count": int,
        "last_job_id": str | None,
        "last_errors": [str],
        "processed_post_ids": [str],
    }

Unlike the old YouTube version, there's no cheap metadata-only endpoint to
pre-scan Facebook posts before deciding whether to launch a job — listing
posts *is* the scrape. So a due/"Run Now" tick launches the job unconditionally;
the job itself (run_facebook.py) determines how many posts actually matched
and reports back via output/<job_id>/manifest.json (see job_runner.py).
"""

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, available_timezones

import urllib.request

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from data_store import append_log, load_workspaces, update_workspace
from job_runner import launch_scrape_job

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_scheduler = BackgroundScheduler(timezone="UTC")
_job_signatures: dict[str, tuple] = {}   # workspace_id -> (day, hour, minute, tz) currently registered

_scanning_lock = threading.Lock()
_scanning: set[str] = set()               # workspace_ids currently being scanned (overlap guard)


# ── HELPERS ───────────────────────────────────────────────────────────────────

_TZ_ALIASES = {
    "PKT": "Asia/Karachi",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "EST": "America/New_York",
    "EDT": "America/New_York",
}


def _safe_tz(tz_name: str) -> str:
    tz_name = _TZ_ALIASES.get(tz_name, tz_name)
    return tz_name if tz_name in available_timezones() else "UTC"


def _parse_time(time_str: str) -> tuple[int, int]:
    try:
        hour, minute = time_str.strip().split(":")
        return int(hour), int(minute)
    except Exception:
        return 8, 0


def compute_next_run(schedule: dict | None, now: datetime | None = None) -> datetime | None:
    """Pure calculation of the next fire time for display purposes only."""
    if not schedule or not schedule.get("enabled"):
        return None
    day_name = (schedule.get("day") or "saturday").lower()
    if day_name not in WEEKDAYS:
        return None
    hour, minute = _parse_time(schedule.get("time") or "08:00")
    tz = ZoneInfo(_safe_tz(schedule.get("timezone") or "UTC"))

    now = now.astimezone(tz) if now else datetime.now(tz)
    target_weekday = WEEKDAYS[day_name]
    days_ahead = (target_weekday - now.weekday()) % 7
    candidate = (now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def _send_slack_notification(lines: list[str]):
    """Send scan summary to Slack via webhook URL from env (if configured)."""
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        return
    try:
        text = "\n".join(lines)
        payload = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        print("[scheduler] Slack notification sent.")
    except Exception as e:
        print(f"[scheduler] Slack notification failed: {e}")


def _log_scan_only(workspace_id: str, lines: list[str]):
    """Write scan info to a standalone log file when no job was created."""
    from data_store import LOGS_DIR
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOGS_DIR / f"scan_{workspace_id[:8]}_{ts}.log"
    with open(log_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[scheduler] Scan log written to {log_file.name}")


# ── SCAN (heavy job — only runs when due, or via Run Now) ───────────────────

def _do_scan(workspace_id: str, manual: bool = False):
    workspaces = load_workspaces()
    ws = next((w for w in workspaces if w["id"] == workspace_id), None)
    if not ws:
        return

    schedule = ws.get("schedule") or {}
    sources = ws.get("fb_sources") or []
    min_comments = schedule.get("min_comments", ws.get("min_comments", 0)) or 0

    now_utc = datetime.now(timezone.utc)
    existing_state = ws.get("schedule_state") or {}

    scan_log_lines = []
    scan_log_lines.append(f"{'━' * 60}")
    scan_log_lines.append(f"📋 FACEBOOK SOURCE SCAN — {ws['name']}")
    scan_log_lines.append(f"   Triggered by: {'Run Now (manual)' if manual else 'Schedule'}")
    scan_log_lines.append(f"   Sources: {len(sources)}")
    scan_log_lines.append(f"   Min comments: {min_comments}")
    scan_log_lines.append("")

    if not sources:
        scan_log_lines.append("⚠️  No Facebook source URLs configured — nothing to scan.")
        update_workspace(workspace_id, {
            "schedule_state": {**existing_state, "last_run_at": now_utc.isoformat(), "last_run_status": "no_sources"},
        })
        _log_scan_only(workspace_id, scan_log_lines)
        return

    fixed_start = (schedule.get("start_date") or "").strip().lower()
    if fixed_start in WEEKDAYS:
        # Weekday-based rolling window: "monday" + a Saturday schedule means
        # every run scrapes from the MOST RECENT monday (run date minus the
        # day difference) → run date. A new monday each week.
        target = WEEKDAYS[fixed_start]
        days_back = (now_utc.weekday() - target) % 7
        start_date = (now_utc - timedelta(days=days_back)).date().isoformat()
        scan_log_lines.append(f"🗓️  Fixed weekday start: {fixed_start} → {start_date}")
    else:
        last_run_at = existing_state.get("last_run_at")
        if last_run_at:
            start_date = last_run_at[:10]
        else:
            start_date = (now_utc - timedelta(days=7)).date().isoformat()
    end_date = now_utc.date().isoformat()

    scan_log_lines.append(f"📅 Date window: {start_date} → {end_date}")
    scan_log_lines.append(f"🚀 Launching scrape job with {len(sources)} source(s):")
    for s in sources:
        scan_log_lines.append(f"   • [{s.get('type')}] {s.get('url')}")
    scan_log_lines.append("")

    job_id = launch_scrape_job(
        ws,
        sources=sources,
        start_date=start_date,
        end_date=end_date,
        min_comments=min_comments,
        triggered_by="manual-run-now" if manual else "schedule",
        skip_post_ids=existing_state.get("processed_post_ids"),
    )
    for line in scan_log_lines:
        append_log(job_id, line)

    update_workspace(workspace_id, {
        "schedule_state": {
            **existing_state,
            "last_run_at": now_utc.isoformat(),
            "last_run_status": "launched",
            "last_job_id": job_id,
        },
    })

    _send_slack_notification(scan_log_lines)


def scan_workspace(workspace_id: str, manual: bool = False):
    """Entry point invoked by CronTrigger (scheduled) or Run Now (manual)."""
    with _scanning_lock:
        if workspace_id in _scanning:
            print(f"[scheduler] Scan already running for workspace {workspace_id}, skipping")
            return
        _scanning.add(workspace_id)
    try:
        _do_scan(workspace_id, manual=manual)
    except Exception as e:
        print(f"[scheduler] Scan error for workspace {workspace_id}: {e}")
    finally:
        with _scanning_lock:
            _scanning.discard(workspace_id)


def run_now(workspace_id: str) -> str | None:
    """Launch a scan/job synchronously enough to return the job_id so the
    caller can redirect. The scrape itself always runs in its own background
    thread via launch_scrape_job."""
    ws = next((w for w in load_workspaces() if w["id"] == workspace_id), None)
    if not ws:
        return None

    with _scanning_lock:
        if workspace_id in _scanning:
            print(f"[scheduler] Scan already running for workspace {workspace_id}, skipping")
            return None
        _scanning.add(workspace_id)
    try:
        _do_scan(workspace_id, manual=True)
    except Exception as e:
        print(f"[scheduler] Scan error for workspace {workspace_id}: {e}")
        return None
    finally:
        with _scanning_lock:
            _scanning.discard(workspace_id)

    ws = next((w for w in load_workspaces() if w["id"] == workspace_id), None)
    if ws:
        state = ws.get("schedule_state") or {}
        return state.get("last_job_id")
    return None


# ── WATCHER (runs every second — cheap, no network calls) ───────────────────

def _watcher_tick():
    try:
        workspaces = load_workspaces()
    except Exception:
        return

    current_ids = set()

    for ws in workspaces:
        wsid = ws["id"]
        schedule = ws.get("schedule") or {}
        job_id = f"scan_{wsid}"

        if not schedule.get("enabled"):
            if _scheduler.get_job(job_id):
                _scheduler.remove_job(job_id)
            _job_signatures.pop(wsid, None)
            continue

        day_name = (schedule.get("day") or "saturday").lower()
        if day_name not in WEEKDAYS:
            continue
        hour, minute = _parse_time(schedule.get("time") or "08:00")
        tz_name = _safe_tz(schedule.get("timezone") or "UTC")

        current_ids.add(wsid)
        signature = (day_name, hour, minute, tz_name)

        if _job_signatures.get(wsid) == signature and _scheduler.get_job(job_id):
            continue  # unchanged — nothing to do

        day_abbr = day_name[:3]  # 'saturday' -> 'sat'
        trigger = CronTrigger(day_of_week=day_abbr, hour=hour, minute=minute, timezone=tz_name)
        _scheduler.add_job(
            scan_workspace,
            trigger=trigger,
            id=job_id,
            args=[wsid],
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        _job_signatures[wsid] = signature

    # Remove jobs for workspaces that were deleted
    for job in list(_scheduler.get_jobs()):
        if job.id.startswith("scan_"):
            wsid = job.id[len("scan_"):]
            if wsid not in current_ids:
                _scheduler.remove_job(job.id)
                _job_signatures.pop(wsid, None)


def init_scheduler():
    """Start the background scheduler. Safe to call multiple times."""
    if _scheduler.running:
        return
    _scheduler.add_job(
        _watcher_tick,
        trigger=IntervalTrigger(seconds=1),
        id="workspace_schedule_watcher",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    # Restore schedules immediately on startup without waiting for the first tick
    _watcher_tick()
