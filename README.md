# Facebook Product Image Analyzer

An AI-powered pipeline that scrapes posts from a Facebook page, group, or a single post, filters them by comment engagement, and runs every post image through an AI vision model to extract brand, product name, and food/non-food classification — all from a Flask web dashboard.

---

## What It Does

1. **Identify** — You tell it explicitly whether a URL is a Post, Page, or Group (via separate fields in the UI — not guessed from the URL's shape, which is unreliable for things like Facebook share links)
2. **Fetch** — Pulls posts in a configured date range (page/group) or a single post, filtered by a minimum-comment threshold
3. **Collect** — Fetches every image attached to each selected post (comment scraping is currently a toggle-off-able step — see Testing Toggles below)
4. **Process** — Converts each image to grayscale and compresses it to <= 200KB
5. **Analyze** — Sends the processed image to **KIE AI** (`gpt-5-6-luna`) to extract brand, product name, and food/non-food category — in parallel across a post's images, rate-limited to stay under KIE's request cap
6. **Save** — Writes structured per-post JSON plus a job-wide image→analysis mapping
7. **Export** — Bundles everything for a job (post JSON + original images + the mapping — not the intermediate processed images) into a downloadable ZIP from the dashboard

All of this runs through a Flask web dashboard in the background — configure a workspace, run it on demand or on a recurring schedule, and watch live logs while it runs.

---

## Features

- **Flask dashboard** at `http://localhost:5007`
- **Explicit source typing** — Post URLs / Page URLs / Group URLs are separate fields, mirroring the old PyQt6 GUI's tabs, so there's no ambiguity about what a pasted URL means
- **Workspaces** — separate configs per source/project, each with its own default minimum-comment threshold
- **Background jobs** — fire and forget; view live logs and a processing-status stepper while the pipeline runs
- **Recurring schedule** — day/time/timezone-based, re-scrapes configured Page/Group sources automatically and skips posts already processed (a schedule can't target a single post — use the Run Bot form for that)
- **Comment threshold filter** — only process posts with at least N comments (0 = process all)
- **Concurrent, rate-limited image analysis** — a post's images are processed and analyzed in parallel (`ThreadPoolExecutor`), while a shared rate limiter keeps actual KIE API calls under its ~20 requests/10s cap regardless of how many threads are running
- **Per-image failure isolation** — a failed image analysis never loses the rest of the post's data
- **Dedup** — a post is never reprocessed within a job, and processed post IDs carry forward across scheduled runs
- **ZIP download** — built on demand per job, old exports swept automatically

---

## Project Structure

```
facebook-image-analyzer/
├── app.py                  # Flask dashboard
├── job_runner.py           # Builds + launches the run_facebook.py subprocess, streams logs
├── scheduler.py            # Recurring per-workspace schedule (APScheduler)
├── data_store.py           # JSON storage for workspaces/jobs/logs/Facebook auth
├── fb_client.py            # Wrapper around the vendored facebook/ scraper library
├── run_facebook.py         # The pipeline itself — fetch → comments → images → process → analyze → save
├── image_pipeline.py       # Grayscale + JPEG compression to <= 200KB
├── kie_vision.py           # KIE image upload + GPT-5.6 Luna product extraction (+ rate limiter, standalone CLI)
├── zip_export.py           # On-demand per-job ZIP export
│
├── facebook/                # Vendored Facebook scraper library (separate git repo)
│   ├── main.py                    # URL/ID extraction, comment fetching
│   ├── post_scraper.py            # Page post fetching + date/comment/image-count filtering
│   ├── group_post_scraper_v2.py   # Group post fetching + date/comment/image-count filtering
│   ├── comment_scraper.py         # Comments + replies
│   ├── single_post_image.py       # Single-post album image fetching
│   └── proxy_utils.py             # Rotating/static proxy selection
│
├── templates/               # Flask HTML templates
│   ├── base.html
│   ├── index.html
│   ├── workspace_form.html
│   ├── workspace_detail.html   # Run Bot form (Post/Page/Group URL tabs) + schedule modal
│   ├── jobs.html
│   ├── job_detail.html
│   └── settings.html
│
├── data/                    # Dashboard state (gitignored)
│   ├── workspaces.json
│   ├── jobs.json
│   ├── fb_auth.json          # Facebook session cookie + fb_dtsg (edited from /settings)
│   └── logs/<job_id>.log
│
├── output/                  # Pipeline output (gitignored)
│   └── <job_id>/
│       ├── post_<post_id>/
│       │   ├── post.json
│       │   ├── images/{original,processed}/
│       │   └── analysis/image_analysis.json
│       ├── image_analysis_mapping.json
│       └── manifest.json
│
└── exports/                 # On-demand ZIP downloads (gitignored)
    └── facebook_export_<job_id>.zip
```

---

## Requirements

- Python 3.11+
- API key (see Environment Variables below)

### Install Python dependencies

```bash
pip install flask apscheduler python-dotenv requests pillow
```

---

## Environment Variables

Create a `.env` file in the project root:

```env
# KIE AI — image upload + GPT-5.6 Luna product extraction
KIE_API_KEY=your_kie_key
```

The vendored `facebook/` scraper library reads its own `facebook/.env` for proxy config (via `python-dotenv`, which searches upward from the calling module's own directory — so this file is picked up automatically without extra config):

```env
ROTATING_PROXY=http://user:pass@host:port   # used when no Facebook session cookie is configured
STATIC_PROXY=http://user:pass@host:port     # used when a Facebook session cookie is configured
```

Your Facebook session (cookie string + `fb_dtsg` token) is **not** an env var — it's entered on the dashboard's **Settings** page and stored in `data/fb_auth.json`. Refresh it there whenever scraping starts failing (these tokens expire periodically). Neither `.env` file, nor `data/fb_auth.json`, is ever committed to git.

---

## Running the Dashboard

```bash
python3 app.py
```

Open **http://localhost:5007** in your browser.

### Dashboard workflow

1. **Configure your Facebook session** — go to Settings, paste your logged-in cookie string and `fb_dtsg` token
2. **Create a Workspace** — give it a name and a default minimum-comment threshold
3. **Open the workspace** → on the Run Bot form, pick the right tab (**Post URLs** / **Page URLs** / **Group URLs**) for each URL you paste, set a date range (Page/Group only) and a minimum-comment threshold
4. Click **Run Bot** — the pipeline starts in the background
5. View live logs and the processing-status stepper on the Job page
6. Once completed, click **Download ZIP** to get everything from that run

### Recurring schedule

Each workspace can also have a weekly day/time/timezone schedule that re-scrapes a configured list of **Page**/**Group** source URLs automatically, skipping posts already processed in a previous run. A recurring schedule doesn't apply to a single post — use the Run Bot form's Post URLs tab for one-off posts.

---

## Testing Toggles

`run_facebook.py` has two constants near the top meant for fast/cheap local iteration — flip them back before relying on real output:

```python
FETCH_COMMENTS = False       # skip comment scraping entirely for page/group posts
MAX_IMAGES_PER_POST = 2      # cap images actually downloaded+analyzed per post
```

`MAX_IMAGES_PER_POST` is enforced at fetch time (the scraper stops downloading once the cap is hit), not by downloading everything and discarding the extras.

You can also test the KIE integration directly, without running a full job:

```bash
python3 kie_vision.py /path/to/image.jpg
```

---

## Output Format

Each processed post gets its own folder under `output/<job_id>/post_<post_id>/`:

```json
{
  "post": {
    "post_id": "123456",
    "post_url": "https://www.facebook.com/...",
    "page_url": "https://www.facebook.com/examplepage",
    "page_name": "Example Page",
    "post_text": "...",
    "published_at": "2026-08-01T12:00:00+00:00",
    "comment_count": 67
  },
  "comments": [ ... ],
  "images": [
    {
      "image_id": "image_001",
      "original_filename": "123456.jpg",
      "processed_filename": "123456_processed.jpg",
      "analysis": {
        "brand": "Coca-Cola",
        "product_name": "Coca-Cola Zero Sugar",
        "category": "food",
        "analysis_status": "success",
        "tokens_used": 1834,
        "credits_consumed": 0.01
      }
    }
  ]
}
```

`output/<job_id>/image_analysis_mapping.json` is a job-wide, uniquely-keyed cross-reference from every processed image filename to its post ID and extracted product info — meant for quick lookups without opening every post's JSON individually.

The downloadable ZIP contains `posts/*.json`, `images/` (originals only), and `image_analysis_mapping.json` — processed (grayscale/compressed) images are an intermediate artifact for the AI call and aren't included in the export.

---

## More Detail

See `CONTEXT.md` for the full architecture writeup, data model, and a running log of non-obvious design decisions (why source typing is explicit, how date filtering actually works, KIE quirks discovered while integrating, etc.) — paste it into a fresh chat with an AI assistant to get full context on this codebase without needing to re-explore it.
