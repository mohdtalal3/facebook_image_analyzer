#!/usr/bin/env python3
"""Flask frontend for the Facebook Product Image Analyzer"""

import csv
import io
import uuid
from datetime import datetime

from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, send_file, abort

import brand_mapping
import fb_client
import zip_export
from data_store import (
    load_workspaces, save_workspaces,
    load_jobs, save_jobs,
    get_logs,
    load_fb_auth, save_fb_auth,
    load_scrape_stats,
    WORKSPACES_FILE, JOBS_FILE,
)
from job_runner import launch_scrape_job
import scheduler as sched_module

app = Flask(__name__)
app.secret_key = "fb-analyzer-dashboard-secret-2026"


# ── CONTEXT PROCESSOR ─────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    jobs = load_jobs()
    running_count = sum(1 for j in jobs if j.get("status") == "running")
    return {
        "all_workspaces": load_workspaces(),
        "running_jobs_count": running_count,
        "compute_next_run": sched_module.compute_next_run,
    }


def _urls_from_field(field_name: str) -> list[str]:
    raw = request.form.get(field_name, "")
    return [u.strip() for u in raw.splitlines() if u.strip()]


def _brand_config_from_form() -> dict:
    """Parse the workspace's single-brand configuration from the workspace
    form. Each workspace tracks exactly ONE brand, and each category
    publishes to its own website + WordPress page (used only when that
    category is selected in `categories`). Returns the config dict; `brand`
    may be empty (the caller validates), everything else is normalized."""
    brand = (request.form.get("brand") or "").strip()
    week_start = (request.form.get("week_start") or "").strip().lower()

    def _target(field: str) -> str:
        value = request.form.get(field)
        return value if value in ("retailshout", "aos") else "retailshout"

    categories = []
    if request.form.get("cat_food") == "on":
        categories.append("food")
    if request.form.get("cat_non_food") == "on":
        categories.append("non_food")
    return {
        "brand": brand if brand in brand_mapping.BRAND_KEYWORDS else "",
        "food_publish_target": _target("food_publish_target"),
        "food_page_id": (request.form.get("food_page_id") or "").strip(),
        "non_food_publish_target": _target("non_food_publish_target"),
        "non_food_page_id": (request.form.get("non_food_page_id") or "").strip(),
        "image_prompt": (request.form.get("image_prompt") or "").strip(),
        "page_title": (request.form.get("page_title") or "").strip(),
        "week_start": week_start if week_start in (
            "monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday") else "friday",
        "categories": categories or ["food", "non_food"],
    }


def _explicit_sources_from_form() -> list[dict]:
    """Build a typed sources list from the three explicit Post/Page/Group
    URL fields — the user tells us the type, we don't guess it (share
    links, mobile links, etc. make URL-shape guessing unreliable)."""
    sources = []
    sources += [{"url": u, "type": "post"} for u in _urls_from_field("post_urls")]
    sources += [{"url": u, "type": "page"} for u in _urls_from_field("page_urls")]
    sources += [{"url": u, "type": "group"} for u in _urls_from_field("group_urls")]
    return sources


# ── ROUTES: DASHBOARD ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    workspaces = load_workspaces()
    jobs = load_jobs()
    recent = sorted(jobs, key=lambda j: j.get("created_at", ""), reverse=True)[:10]
    stats = {
        "total_workspaces": len(workspaces),
        "total_jobs": len(jobs),
        "running": sum(1 for j in jobs if j.get("status") == "running"),
        "completed": sum(1 for j in jobs if j.get("status") == "completed"),
        "failed": sum(1 for j in jobs if j.get("status") == "failed"),
    }
    return render_template("index.html", workspaces=workspaces, recent_jobs=recent, stats=stats)


# ── ROUTES: WORKSPACES ────────────────────────────────────────────────────────

@app.route("/workspaces/new", methods=["GET", "POST"])
def workspace_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        brand_config = _brand_config_from_form()
        if not name:
            return render_template("workspace_form.html", workspace=None, error="Workspace name is required.",
                                   all_brands=sorted(brand_mapping.BRAND_KEYWORDS.keys()))
        if not brand_config["brand"]:
            return render_template("workspace_form.html", workspace=None,
                                   error="Please select the brand this workspace tracks.",
                                   all_brands=sorted(brand_mapping.BRAND_KEYWORDS.keys()))

        min_comments_raw = request.form.get("min_comments", "").strip()
        ws = {
            "id": str(uuid.uuid4()),
            "name": name,
            "min_comments": int(min_comments_raw) if min_comments_raw.isdigit() else 0,
            "created_at": datetime.now().isoformat(),
            "fb_sources": [],
            **brand_config,
            "schedule": {"enabled": False, "day": "saturday", "time": "08:00", "timezone": "UTC", "start_date": None, "min_comments": 0},
            "schedule_state": {},
        }
        workspaces = load_workspaces()
        workspaces.append(ws)
        save_workspaces(workspaces)
        return redirect(url_for("workspace_detail", workspace_id=ws["id"]))

    return render_template("workspace_form.html", workspace=None, error=None,
                           all_brands=sorted(brand_mapping.BRAND_KEYWORDS.keys()))


@app.route("/workspaces/<workspace_id>")
def workspace_detail(workspace_id):
    workspaces = load_workspaces()
    ws = next((w for w in workspaces if w["id"] == workspace_id), None)
    if not ws:
        return redirect(url_for("index"))

    jobs = load_jobs()
    ws_jobs = sorted(
        [j for j in jobs if j.get("workspace_id") == workspace_id],
        key=lambda j: j.get("created_at", ""),
        reverse=True,
    )
    return render_template("workspace_detail.html", workspace=ws, jobs=ws_jobs)


@app.route("/workspaces/<workspace_id>/edit", methods=["GET", "POST"])
def workspace_edit(workspace_id):
    workspaces = load_workspaces()
    ws = next((w for w in workspaces if w["id"] == workspace_id), None)
    if not ws:
        return redirect(url_for("index"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        brand_config = _brand_config_from_form()
        if not name:
            return render_template("workspace_form.html", workspace=ws, error="Workspace name is required.",
                                   all_brands=sorted(brand_mapping.BRAND_KEYWORDS.keys()))
        if not brand_config["brand"]:
            return render_template("workspace_form.html", workspace=ws,
                                   error="Please select the brand this workspace tracks.",
                                   all_brands=sorted(brand_mapping.BRAND_KEYWORDS.keys()))
        ws["name"] = name
        min_comments_raw = request.form.get("min_comments", "").strip()
        ws["min_comments"] = int(min_comments_raw) if min_comments_raw.isdigit() else 0
        ws.update(brand_config)
        # drop legacy field names replaced by the per-category publish config
        for legacy_key in ("brands", "page_id", "publish_target"):
            ws.pop(legacy_key, None)
        save_workspaces(workspaces)
        return redirect(url_for("workspace_detail", workspace_id=workspace_id))

    return render_template("workspace_form.html", workspace=ws, error=None,
                           all_brands=sorted(brand_mapping.BRAND_KEYWORDS.keys()))


@app.route("/workspaces/<workspace_id>/delete", methods=["POST"])
def workspace_delete(workspace_id):
    workspaces = [w for w in load_workspaces() if w["id"] != workspace_id]
    save_workspaces(workspaces)
    return redirect(url_for("index"))


@app.route("/workspaces/<workspace_id>/run", methods=["POST"])
def workspace_run(workspace_id):
    workspaces = load_workspaces()
    ws = next((w for w in workspaces if w["id"] == workspace_id), None)
    if not ws:
        flash("Workspace not found.", "danger")
        return redirect(url_for("index"))

    sources = _explicit_sources_from_form()
    if not sources:
        flash("Please enter at least one Facebook URL (Post, Page, or Group).", "warning")
        return redirect(url_for("workspace_detail", workspace_id=workspace_id))

    start_date = request.form.get("start_date", "").strip() or None
    end_date = request.form.get("end_date", "").strip() or None

    min_comments_raw = request.form.get("min_comments", "").strip()
    min_comments = int(min_comments_raw) if min_comments_raw.isdigit() else (ws.get("min_comments") or 0)

    brand_limit_raw = request.form.get("brand_post_limit", "").strip()
    brand_post_limit = int(brand_limit_raw) if brand_limit_raw.isdigit() else 0

    job_id = launch_scrape_job(
        ws,
        sources=sources,
        start_date=start_date,
        end_date=end_date,
        min_comments=min_comments,
        triggered_by="manual",
        brand_post_limit=brand_post_limit,
    )

    return redirect(url_for("job_detail", job_id=job_id))


# ── ROUTES: JOBS ──────────────────────────────────────────────────────────────

@app.route("/jobs")
def jobs_list():
    jobs = sorted(load_jobs(), key=lambda j: j.get("created_at", ""), reverse=True)
    return render_template("jobs.html", jobs=jobs)


@app.route("/jobs/<job_id>")
def job_detail(job_id):
    jobs = load_jobs()
    job = next((j for j in jobs if j["id"] == job_id), None)
    if not job:
        return redirect(url_for("jobs_list"))
    return render_template("job_detail.html", job=job)


@app.route("/jobs/<job_id>/download")
def job_download(job_id):
    jobs = load_jobs()
    job = next((j for j in jobs if j["id"] == job_id), None)
    if not job:
        abort(404)
    try:
        zip_path = zip_export.build_job_zip(job_id)
    except FileNotFoundError:
        flash("No output found for this job yet.", "warning")
        return redirect(url_for("job_detail", job_id=job_id))
    return send_file(zip_path, as_attachment=True, download_name=f"facebook_export_{job_id[:8]}.zip")


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/jobs/<job_id>/logs")
def api_job_logs(job_id):
    logs = get_logs(job_id)
    jobs = load_jobs()
    job = next((j for j in jobs if j["id"] == job_id), {"status": "unknown"})
    return jsonify({"logs": logs, "status": job["status"]})


@app.route("/api/jobs/<job_id>/status")
def api_job_status(job_id):
    jobs = load_jobs()
    job = next((j for j in jobs if j["id"] == job_id), None)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "status": job["status"],
        "created_at": job.get("created_at"),
        "finished_at": job.get("finished_at"),
        "post_count": job.get("post_count"),
    })


@app.route("/api/stats")
def api_stats():
    jobs = load_jobs()
    return jsonify({
        "total": len(jobs),
        "pending": sum(1 for j in jobs if j.get("status") == "pending"),
        "running": sum(1 for j in jobs if j.get("status") == "running"),
        "completed": sum(1 for j in jobs if j.get("status") == "completed"),
        "failed": sum(1 for j in jobs if j.get("status") == "failed"),
    })


# ── ROUTES: SCRAPE STATS ─────────────────────────────────────────────────────

def _filtered_scrape_stats() -> tuple[list, str, str, str]:
    """Load scraper stats, applying the optional brand + date-range filters
    from the query string (?brand=&date_from=YYYY-MM-DD&date_to=YYYY-MM-DD)."""
    stats = load_scrape_stats()
    brand = (request.args.get("brand") or "").strip()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    if brand:
        stats = [s for s in stats if s.get("brand") == brand]
    if date_from:
        stats = [s for s in stats if (s.get("date") or "") >= date_from]
    if date_to:
        stats = [s for s in stats if (s.get("date") or "") <= date_to]
    return stats, brand, date_from, date_to


def _aggregate_scrape_stats(stats: list) -> list[dict]:
    """Roll per-run records up into one row per brand with food / non-food
    columns: how many products were looked up (total), matched (found) and
    had a price (with_price)."""
    agg: dict = {}
    for s in stats:
        key = (s.get("brand") or "Unknown", s.get("brand_slug") or "")
        row = agg.setdefault(key, {
            "brand": key[0],
            "brand_slug": key[1],
            "food": {"total": 0, "found": 0, "with_price": 0},
            "non_food": {"total": 0, "found": 0, "with_price": 0},
        })
        cat = s.get("category") if s.get("category") in ("food", "non_food") else "food"
        for field in ("total", "found", "with_price"):
            row[cat][field] += int(s.get(field) or 0)
    return sorted(agg.values(), key=lambda r: r["brand"].lower())


@app.route("/scrape-stats")
def scrape_stats():
    stats, brand, date_from, date_to = _filtered_scrape_stats()
    summary = _aggregate_scrape_stats(stats)
    brands = sorted({s.get("brand") for s in load_scrape_stats() if s.get("brand")})
    runs = sorted(stats, key=lambda s: s.get("recorded_at", ""), reverse=True)
    return render_template("scrape_stats.html",
                           runs=runs, summary=summary,
                           selected_brand=brand, date_from=date_from, date_to=date_to,
                           all_brands=brands)


@app.route("/scrape-stats/download.csv")
def scrape_stats_csv():
    stats, brand, date_from, date_to = _filtered_scrape_stats()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "brand", "category", "products_looked_up",
                     "products_found", "with_price", "job_id"])
    for s in sorted(stats, key=lambda s: (s.get("date", ""), s.get("brand", ""), s.get("category", ""))):
        writer.writerow([s.get("date"), s.get("brand"), s.get("category"),
                         s.get("total"), s.get("found"), s.get("with_price"), s.get("job_id")])
    buf.seek(0)
    return send_file(io.BytesIO(buf.getvalue().encode("utf-8-sig")), as_attachment=True,
                     download_name="scrape_stats.csv", mimetype="text/csv")


# ── ROUTES: SOURCE SCHEDULE ──────────────────────────────────────────────────────

@app.route("/workspaces/<workspace_id>/schedule", methods=["POST"])
def workspace_schedule_save(workspace_id):
    workspaces = load_workspaces()
    ws = next((w for w in workspaces if w["id"] == workspace_id), None)
    if not ws:
        flash("Workspace not found.", "danger")
        return redirect(url_for("index"))

    enabled = request.form.get("schedule_enabled", "off") == "on"
    day = request.form.get("schedule_day", "saturday").strip().lower()
    time_str = request.form.get("schedule_time", "08:00").strip()
    tz_name = request.form.get("schedule_timezone", "UTC").strip()
    start_date = request.form.get("schedule_start_date", "").strip() or None

    min_comments_raw = request.form.get("schedule_min_comments", "").strip()
    min_comments = int(min_comments_raw) if min_comments_raw.isdigit() else 0

    page_urls = _urls_from_field("schedule_page_urls")
    group_urls = _urls_from_field("schedule_group_urls")
    sources = (
        [{"url": u, "type": "page"} for u in page_urls] +
        [{"url": u, "type": "group"} for u in group_urls]
    )

    ws["fb_sources"] = sources
    ws["min_comments"] = min_comments
    ws["schedule"] = {
        "enabled": enabled,
        "day": day,
        "time": time_str,
        "timezone": tz_name,
        "start_date": start_date,
        "min_comments": min_comments,
    }
    save_workspaces(workspaces)
    flash("Schedule saved.", "success")
    return redirect(url_for("workspace_detail", workspace_id=workspace_id))


@app.route("/workspaces/<workspace_id>/schedule/run-now", methods=["POST"])
def workspace_schedule_run_now(workspace_id):
    workspaces = load_workspaces()
    ws = next((w for w in workspaces if w["id"] == workspace_id), None)
    if not ws:
        flash("Workspace not found.", "danger")
        return redirect(url_for("index"))
    if not ws.get("fb_sources"):
        flash("Add at least one Facebook source URL before running a scan.", "warning")
        return redirect(url_for("workspace_detail", workspace_id=workspace_id))

    job_id = sched_module.run_now(workspace_id)
    if job_id:
        flash("Scan launched — redirecting to job logs.", "success")
        return redirect(url_for("job_detail", job_id=job_id))
    flash("Could not launch scan (a scan may already be in progress).", "info")
    return redirect(url_for("workspace_detail", workspace_id=workspace_id))


@app.route("/workspaces/<workspace_id>/schedule/disable", methods=["POST"])
def workspace_schedule_disable(workspace_id):
    workspaces = load_workspaces()
    ws = next((w for w in workspaces if w["id"] == workspace_id), None)
    if not ws:
        flash("Workspace not found.", "danger")
        return redirect(url_for("index"))

    schedule = ws.get("schedule") or {}
    schedule["enabled"] = False
    ws["schedule"] = schedule
    save_workspaces(workspaces)
    flash("Schedule disabled.", "info")
    return redirect(url_for("workspace_detail", workspace_id=workspace_id))


# ── ROUTES: SETTINGS ──────────────────────────────────────────────────────────

@app.route("/settings")
def settings():
    workspaces_raw = WORKSPACES_FILE.read_text(encoding="utf-8")
    jobs_raw = JOBS_FILE.read_text(encoding="utf-8")
    fb_auth = load_fb_auth()
    return render_template("settings.html", workspaces_raw=workspaces_raw, jobs_raw=jobs_raw, fb_auth=fb_auth)


@app.route("/api/constants", methods=["GET"])
def api_constants_get():
    """The editable pipeline constants (constants.py) for the Settings tab."""
    from constants_store import read_values
    return jsonify({"constants": list(read_values().values())})


@app.route("/api/constants", methods=["POST"])
def api_constants_save():
    """Save {key: value} updates into constants.py. Applies to newly launched
    jobs (subprocesses re-import constants.py on launch)."""
    from constants_store import write_values
    updates = request.get_json(silent=True) or {}
    errors = write_values(updates)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    return jsonify({"ok": True})


@app.route("/settings/save", methods=["POST"])
def settings_save():
    target = request.form.get("target", "")
    content = request.form.get("content", "")

    try:
        import json as _json
        parsed = _json.loads(content)
    except Exception as e:
        flash(f"Invalid JSON: {e}", "danger")
        return redirect(url_for("settings"))

    if target == "workspaces":
        save_workspaces(parsed)
        flash("workspaces.json saved.", "success")
    elif target == "jobs":
        save_jobs(parsed)
        flash("jobs.json saved.", "success")
    else:
        flash("Unknown target.", "danger")

    return redirect(url_for("settings"))


@app.route("/settings/fb-auth", methods=["POST"])
def settings_fb_auth_save():
    cookie_string = request.form.get("cookie_string", "").strip()
    fb_dtsg = request.form.get("fb_dtsg", "").strip()
    save_fb_auth(cookie_string, fb_dtsg)
    flash("Facebook session saved.", "success")
    return redirect(url_for("settings"))


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 50)
    print("  Facebook Product Image Analyzer")
    print("  http://localhost:5007")
    print("=" * 50)
    sched_module.init_scheduler()
    app.run(debug=False, host="0.0.0.0", port=5006, use_reloader=False)
