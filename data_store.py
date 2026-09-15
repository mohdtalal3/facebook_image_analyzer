#!/usr/bin/env python3
"""
Shared data-access layer for workspaces.json and jobs.json.

Extracted from app.py so both the Flask app and the background scheduler
(scheduler.py) can read/write the same files safely without importing
app.py itself (which would re-run the Flask app as a side effect).
"""

import json
import threading
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = DATA_DIR / "logs"
WORKSPACES_FILE = DATA_DIR / "workspaces.json"
JOBS_FILE = DATA_DIR / "jobs.json"
FB_AUTH_FILE = DATA_DIR / "fb_auth.json"
SCRAPER_STATS_FILE = DATA_DIR / "scraper_stats.json"
OUTPUT_DIR = BASE_DIR / "output"
EXPORTS_DIR = BASE_DIR / "exports"

# Ensure data directories and files exist on import
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
EXPORTS_DIR.mkdir(exist_ok=True)
if not WORKSPACES_FILE.exists():
    WORKSPACES_FILE.write_text("[]", encoding="utf-8")
if not JOBS_FILE.exists():
    JOBS_FILE.write_text("[]", encoding="utf-8")
if not FB_AUTH_FILE.exists():
    FB_AUTH_FILE.write_text(
        json.dumps({"cookie_string": "", "fb_dtsg": "", "updated_at": None}, indent=2),
        encoding="utf-8",
    )
if not SCRAPER_STATS_FILE.exists():
    SCRAPER_STATS_FILE.write_text("[]", encoding="utf-8")

_lock = threading.Lock()


# ── WORKSPACES ──────────────────────────────────────────────────────────────

VALID_CATEGORIES = ("food", "non_food")
PUBLISH_TARGETS = ("retailshout", "aos")


def normalize_workspace(ws: dict) -> dict:
    """Normalize a workspace record to the current one-brand-per-workspace
    shape with per-category publishing — food and non-food each get their own
    publish target + WordPress page ID, used only when that category is
    selected. Legacy shapes are folded in, in memory (the next save through
    the workspace form persists the new shape):
      - brands[] lists (the old multi-brand config): the first entry becomes
        the workspace's single brand; its page_id/publish_target become the
        FOOD destination.
      - the intermediate single page_id/publish_target fields: kept as the
        FOOD destination.
    Also defaults the food/non-food category selection to both when absent.
    Applied to every workspace read via load_workspaces(), and available to
    callers that receive a workspace dict from elsewhere (e.g. a hand-edited
    file)."""
    if isinstance(ws.get("brands"), list) and ws["brands"]:
        first = ws["brands"][0] or {}
        ws.setdefault("brand", first.get("brand") or "")
        ws.setdefault("food_page_id", first.get("page_id") or "")
        ws.setdefault("food_publish_target", first.get("publish_target") or "retailshout")
        ws.setdefault("image_prompt", first.get("image_prompt") or "")
        ws.setdefault("page_title", first.get("page_title") or "")
        ws.setdefault("week_start", first.get("week_start") or "friday")
    if "food_page_id" not in ws:
        ws["food_page_id"] = ws.get("page_id") or ""
    if "food_publish_target" not in ws:
        ws["food_publish_target"] = ws.get("publish_target") or "retailshout"
    if ws.get("food_publish_target") not in PUBLISH_TARGETS:
        ws["food_publish_target"] = "retailshout"
    if ws.get("non_food_publish_target") not in PUBLISH_TARGETS:
        ws["non_food_publish_target"] = "retailshout"
    ws.setdefault("non_food_page_id", "")
    if not isinstance(ws.get("categories"), list) or not ws["categories"]:
        ws["categories"] = list(VALID_CATEGORIES)
    return ws


def load_workspaces() -> list:
    with _lock:
        data = json.loads(WORKSPACES_FILE.read_text(encoding="utf-8"))
    return [normalize_workspace(ws) for ws in data]


def save_workspaces(data: list):
    with _lock:
        WORKSPACES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def update_workspace(workspace_id: str, updates: dict):
    """Atomically merge `updates` into the workspace dict with the given id."""
    with _lock:
        workspaces = json.loads(WORKSPACES_FILE.read_text(encoding="utf-8"))
        for ws in workspaces:
            if ws["id"] == workspace_id:
                ws.update(updates)
                break
        WORKSPACES_FILE.write_text(json.dumps(workspaces, indent=2, ensure_ascii=False), encoding="utf-8")


# ── JOBS ────────────────────────────────────────────────────────────────────

def load_jobs() -> list:
    with _lock:
        return json.loads(JOBS_FILE.read_text(encoding="utf-8"))


def save_jobs(data: list):
    with _lock:
        JOBS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def update_job(job_id: str, updates: dict):
    with _lock:
        jobs = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
        for job in jobs:
            if job["id"] == job_id:
                job.update(updates)
                break
        JOBS_FILE.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")


# ── FACEBOOK AUTH (session cookie + fb_dtsg, edited from /settings) ─────────

def load_fb_auth() -> dict:
    with _lock:
        return json.loads(FB_AUTH_FILE.read_text(encoding="utf-8"))


def save_fb_auth(cookie_string: str, fb_dtsg: str):
    import datetime as _dt
    with _lock:
        FB_AUTH_FILE.write_text(
            json.dumps({
                "cookie_string": cookie_string,
                "fb_dtsg": fb_dtsg,
                "updated_at": _dt.datetime.now().isoformat(),
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ── SCRAPER STATS (per-run product lookup results, shown on /scrape-stats) ──

def load_scrape_stats() -> list:
    with _lock:
        return json.loads(SCRAPER_STATS_FILE.read_text(encoding="utf-8"))


def record_scrape_stats(brand: str, brand_slug: str, category: str, job_id: str,
                        total: int, found: int, with_price: int):
    """Append one scrape-run summary record — how many products the brand
    scraper looked up (total), how many matched (found) and how many of
    those had a price (with_price). One record per brand+category per run."""
    record = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "recorded_at": datetime.now().isoformat(),
        "brand": brand,
        "brand_slug": brand_slug,
        "category": category,
        "job_id": job_id,
        "total": total,
        "found": found,
        "with_price": with_price,
    }
    with _lock:
        stats = json.loads(SCRAPER_STATS_FILE.read_text(encoding="utf-8"))
        stats.append(record)
        SCRAPER_STATS_FILE.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")


# ── LOGS ────────────────────────────────────────────────────────────────────

def append_log(job_id: str, line: str):
    log_file = LOGS_DIR / f"{job_id}.log"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_logs(job_id: str) -> str:
    log_file = LOGS_DIR / f"{job_id}.log"
    if log_file.exists():
        return log_file.read_text(encoding="utf-8")
    return ""
