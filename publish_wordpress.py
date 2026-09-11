#!/usr/bin/env python3
"""
Per-brand WordPress publisher — the Facebook-pipeline equivalent of
publish_youtube.py.

Reads a completed scrape job's output for ONE brand
(output/<parent_job_id>/<brand-slug>/food/) — the flat images/ folder plus
its combined image_analysis.json (KIE product names) — uploads every food
image to the brand's WordPress site, and updates the brand's configured
WordPress page with a finds-item listing (same CSS classes/structure as the
other RetailShout publishers).

Only the food category is published — non_food/uncategorized output is
ignored. Credentials come from the root .env:
  retailshout -> WP_URL_RS / WP_USERNAME_RS / WP_PASSWORD_RS
  aos         -> WP_URL / WP_USERNAME / WP_PASSWORD

Usage (normally launched by job_runner.py, not by hand):
  python publish_wordpress.py --parent-job-id <uuid> --brand "ALDI" \
      --brand-slug aldi --page-id 12345 --publish-target retailshout
"""

import argparse
import json
import os
from html import escape
from pathlib import Path

import requests
from dotenv import load_dotenv

from wordpress_publisher import WordPressPublisher

# Load .env from project root (WP_URL_RS/... or WP_URL/... per publish target)
load_dotenv(Path(__file__).resolve().parent / ".env")

PUBLISH_TARGETS = {
    "retailshout": {
        "env": ("WP_URL_RS", "WP_USERNAME_RS", "WP_PASSWORD_RS"),
        "site_name": "retailshout.com",
    },
    "aos": {
        "env": ("WP_URL", "WP_USERNAME", "WP_PASSWORD"),
        "site_name": "aisleofshame.com",
    },
}

# KIE food subcategories (kie_vision.PRODUCT_EXTRACTION_PROMPT) — the page
# groups food items under these headings; anything null/unrecognized lands
# in "Other".
SUBCATEGORIES = [
    "Bakery & Deli",
    "Dairy & Eggs",
    "Meat & Seafood",
    "Frozen Foods & Breakfast",
    "Snacks & Pantry Staples",
    "Beverages & Energy",
]
ITEMS_PER_CATEGORY_LIMIT = 20  # items visible per subcategory before "Show more"


def load_food_images(brand_dir: Path) -> tuple[list[Path], dict]:
    """Load the brand's food-category flat images + their KIE analysis
    mapping. Returns ([image paths], {filename: analysis entry})."""
    images_dir = brand_dir / "images"
    analysis_file = brand_dir / "image_analysis.json"

    analysis = {}
    if analysis_file.exists():
        try:
            analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️  Could not read {analysis_file.name}: {e}")

    if not images_dir.exists():
        return [], analysis
    images = sorted(p for p in images_dir.iterdir() if p.is_file())
    return images, analysis


def product_name_for(image_path: Path, analysis: dict) -> str | None:
    """KIE-detected product name for an image, from the combined mapping
    (keyed by the same `post_<id>_<filename>` name the flat images dir uses)."""
    entry = analysis.get(image_path.name) or {}
    return entry.get("product_name") or None


def slugify(text: str) -> str:
    """Create a URL-safe anchor from a subcategory name."""
    return text.lower().replace(" ", "-").replace("&", "and").replace("/", "-").replace("--", "-")


def render_item_html(item_index: int, name: str, img_url: str,
                     price: str | None = None, description: str | None = None,
                     defer_image: bool = False) -> str:
    """Render one product using the same finds-item CSS classes as the
    other RetailShout publishers (mirrors publish_youtube.render_item_html).
    `price`/`description` come from the brand scraper analysis (may be None).
    Title line reads "N) Product Name — $3.89" with the price in red; the
    description is shown below the image — descriptions longer than 2 lines
    (or with a line over 110 chars) are clamped to 2 lines with a
    "Show more" toggle that reveals the rest. With `defer_image` the image
    URL goes into data-src for lazy loading (hidden show-more items)."""
    title = name or f"Item #{item_index}"
    title_safe = escape(title)
    title_html = f"{item_index}) {title_safe}"
    if price:
        title_html += f' — <span style="color:#d40000;font-weight:600;">{escape(str(price))}</span>'
    img_attr = "data-src" if defer_image else "src"
    lazy_class = "lazy-load" if defer_image else ""
    html = '    <div class="finds-item">'
    html += '<div class="finds-item-header">'
    html += f'<span class="finds-item-title">{title_html}</span>'
    html += '</div>'
    html += (
        f'<div style="text-align: center; margin-top: 10px; padding: 10px;">'
        f'<div style="position:relative;display:inline-block;max-width:400px;width:100%;">'
        f'<img {img_attr}="{img_url}" alt="{title_safe}" '
        f'style="max-width: 400px; width: 100%; height: auto; display: block;" class="{lazy_class}" />'
        f'</div></div>'
    )
    if description:
        desc_style = ("max-width:400px;margin:10px auto 0;text-align:left;font-size:14px;"
                      "line-height:1.5;color:#333;")
        desc_lines = [l.strip() for l in description.split("\n") if l.strip()]
        desc_html = "".join(f"<div>{escape(line)}</div>" for line in desc_lines)

        is_long = len(desc_lines) > 2 or any(len(l) > 110 for l in desc_lines)

        if not is_long:
            html += f'<div class="finds-item-desc" style="{desc_style}">{desc_html}</div>'
        else:
            desc_id = f"desc_{item_index}"
            clamp_style = ("display: -webkit-box; -webkit-line-clamp: 2; "
                           "-webkit-box-orient: vertical; overflow: hidden;")
            html += f'<div class="finds-item-desc" style="{desc_style}">'
            html += f'<div id="{desc_id}" style="{clamp_style}">{desc_html}</div>'
            html += (f' <a href="javascript:void(0)" onclick="var d=document.getElementById(\'{desc_id}\');'
                     f'if(d.style.webkitLineClamp===\'2\'||d.style.webkitLineClamp===\'\')'
                     f'{{d.style.webkitLineClamp=\'unset\';d.style.display=\'block\';this.textContent=\' Show less\'}}'
                     f'else{{d.style.webkitLineClamp=\'2\';d.style.display=\'-webkit-box\';this.textContent=\' Show more\'}}" '
                     f'style="color: #d40000; font-size: 0.85em; cursor: pointer; text-decoration: underline;"> Show more</a>')
            html += '</div>'
    html += '</div>'
    return html


def make_show_more_button(anchor: str, label: str, hidden_count: int) -> str:
    """Generate a show more/less button with inline onclick (no external JS
    needed) — hidden items' images are lazy-loaded (data-src) and swapped
    into src when the section is expanded."""
    safe_label = label.replace("'", "\\'")
    onclick = (
        "event.stopPropagation();"
        f"var d=document.getElementById('finds-more-{anchor}');"
        "if(d){"
        "if(d.style.display==='none'){"
        "d.style.display='block';"
        "this.setAttribute('aria-expanded','true');"
        "this.textContent='Show less';"
        "var imgs=d.querySelectorAll('img[data-src]');"
        "for(var i=0;i<imgs.length;i++){"
        "if(imgs[i].getAttribute('data-src')){"
        "imgs[i].setAttribute('src',imgs[i].getAttribute('data-src'));"
        "imgs[i].removeAttribute('data-src');"
        "}"
        "}"
        "}else{"
        "d.style.display='none';"
        "this.setAttribute('aria-expanded','false');"
        "var n=d.querySelectorAll('.finds-item').length;"
        f"this.textContent='Show more in {safe_label} ('+n+' more)';"
        "}"
        "}"
    )
    return (
        f'\n  <button class="aos-show-more" data-target="{anchor}" data-label="{label}" '
        f'aria-expanded="false" onclick="{onclick}">'
        f'Show more in {label} ({hidden_count} more)</button>'
    )


def build_toc(subcategories: list[str]) -> str:
    """Table of contents linking to each subcategory section."""
    toc_items = [f'<li><a href="#finds-cat-{slugify(c)}">{escape(c)}</a></li>'
                 for c in subcategories]
    if not toc_items:
        return ""
    return (f'  <div id="toc" class="toc">\n'
            f'    <h3 class="toc-title">Jump to Category</h3>\n'
            f'    <ul class="toc-list">\n'
            f'      {"".join(toc_items)}\n'
            f'    </ul>\n'
            f'  </div>')


def build_subcategory_section(subcategory: str, items: list[tuple]) -> str:
    """One subcategory section: heading + first ITEMS_PER_CATEGORY_LIMIT items
    visible, the rest in a hidden show-more block (lazy-loaded images)."""
    anchor = slugify(subcategory)
    html = f'\n  <h3 id="finds-cat-{anchor}" class="aos-category-title">{escape(subcategory)}</h3>'
    html += '\n  <div class="aos-category-items">'

    visible_items = items[:ITEMS_PER_CATEGORY_LIMIT]
    hidden_items = items[ITEMS_PER_CATEGORY_LIMIT:] if len(items) > ITEMS_PER_CATEGORY_LIMIT else []

    item_index = 1
    for item in visible_items:
        html += "\n" + render_item_html(item_index, item[0], item[1],
                                        price=item[2] if len(item) > 2 else None,
                                        description=item[3] if len(item) > 3 else None,
                                        defer_image=False)
        item_index += 1

    if hidden_items:
        more_id = f"finds-more-{anchor}"
        html += '\n  </div>'
        html += f'\n  <div class="aos-more" id="{more_id}" style="display:none;">'
        html += '\n    <div class="aos-category-items">'
        for item in hidden_items:
            html += "\n" + render_item_html(item_index, item[0], item[1],
                                            price=item[2] if len(item) > 2 else None,
                                            description=item[3] if len(item) > 3 else None,
                                            defer_image=True)
            item_index += 1
        html += '\n    </div>'
        html += '\n  </div>'
        html += make_show_more_button(anchor, subcategory, len(hidden_items))
    else:
        html += '\n  </div>'

    html += '\n  <div style="text-align: center; margin-top: 15px;">'
    html += '\n    <a href="#toc" style="color: #810e0e; text-decoration: underline; font-weight: bold;">Back to TOP</a>'
    html += '\n  </div>'
    return html


def build_page_html(items: list[tuple], brand: str) -> str:
    """Build the full WordPress page HTML, grouped by KIE subcategory with a
    table of contents. `items` is [(name, img_url)] or
    [(name, img_url, price, description, subcategory)]. Items with a null or
    unrecognized subcategory land in "Other"."""
    groups: dict[str, list[tuple]] = {}
    for item in items:
        sub = item[4] if len(item) > 4 and item[4] else "Other"
        if sub not in SUBCATEGORIES:
            sub = "Other"
        groups.setdefault(sub, []).append(item)

    ordered = [s for s in SUBCATEGORIES if s in groups]
    if "Other" in groups:
        ordered.append("Other")

    total = len(items)
    html = '<div id="top" class="aos-finds">\n'
    html += f'  <h3 style="text-align: center;">{escape(brand)} Food Finds</h3>\n\n'
    html += f'  <p style="text-align: center; font-style: italic;">{total} products found</p>\n\n'
    html += build_toc(ordered)
    html += "\n\n"
    for sub in ordered:
        html += build_subcategory_section(sub, groups[sub]) + "\n"
    html += '</div>'
    return html


def publish_brand(parent_job_id: str, brand: str, brand_slug: str, page_id: str,
                  publish_target: str, output_root: str = "output",
                  status: str = "draft") -> tuple[bool, str]:
    """Publish one brand's food images from a completed scrape job's output.
    Returns (success, summary message). Called by run_brand_job.py (the
    per-brand sub-workflow) and by this file's CLI."""
    target = PUBLISH_TARGETS.get(publish_target) or PUBLISH_TARGETS["retailshout"]
    wp_url = os.environ.get(target["env"][0])
    wp_username = os.environ.get(target["env"][1])
    wp_password = os.environ.get(target["env"][2])
    if not all([wp_url, wp_username, wp_password]):
        return False, (f"Missing WordPress credentials for {target['site_name']} — set "
                       f"{' / '.join(target['env'])} in .env")

    brand_dir = Path(output_root) / parent_job_id / brand_slug / "food"
    images, analysis = load_food_images(brand_dir)

    non_food_dir = brand_dir.parent / "non_food" / "images"
    if non_food_dir.exists():
        excluded = sorted(p for p in non_food_dir.iterdir() if p.is_file())
        if excluded:
            print(f"🚫 {brand}: {len(excluded)} non-food image(s) excluded from publishing "
                  f"(in {non_food_dir})")

    if not images:
        return False, f"No food images found for {brand} under {brand_dir} — nothing to publish."
    print(f"📋 {brand}: {len(images)} food image(s) from job {parent_job_id[:8]}...")

    publisher = WordPressPublisher(wp_url, wp_username, wp_password)
    if not publisher.test_connection():
        return False, f"WordPress connection failed for {target['site_name']}"

    print(f"\n📤 Uploading images to {target['site_name']}...")
    items: list[tuple[str, str]] = []
    for i, img_path in enumerate(images, start=1):
        entry = analysis.get(img_path.name) or {}
        scraped = entry.get("scraped") or {}
        name = entry.get("product_name") or scraped.get("name") or img_path.stem
        title = name
        media_id = publisher.upload_image(img_path, title=title)
        if not media_id:
            print(f"  [{i}] ⚠️  Upload failed for {img_path.name} — skipping")
            continue
        try:
            resp = requests.get(f"{publisher.api_base}/media/{media_id}", auth=publisher.auth, timeout=15)
            source_url = resp.json().get("source_url") if resp.status_code == 200 else None
        except Exception as e:
            print(f"  ⚠️  Could not resolve URL for media {media_id}: {e}")
            source_url = None
        if source_url:
            items.append((name, source_url, scraped.get("price"), scraped.get("description"),
                          entry.get("subcategory")))
            price_note = f" ({scraped.get('price')})" if scraped.get("price") else ""
            print(f"  [{i}] ✅ Uploaded '{title}'{price_note}")

    if not items:
        return False, "No images uploaded successfully — nothing to publish."

    print(f"\n🎨 Building page HTML ({len(items)} item(s))...")
    html = build_page_html(items, brand)

    print(f"\n📤 Updating WordPress page {page_id} as {status.upper()}...")
    success = publisher.update_page(
        page_id=int(page_id),
        content=html,
        status=status,
        try_page_first=True,
        update_date=(status == "publish"),
    )
    if not success:
        return False, f"WordPress page {page_id} update failed."

    summary = (f"Brand {brand} → {target['site_name']} page {page_id}: "
               f"{len(items)} product(s) published as {status.upper()}")
    print("\n" + "=" * 60)
    print("✅ PUBLISHED SUCCESSFULLY!")
    print(f"   Brand    : {brand}")
    print(f"   Target   : {target['site_name']}")
    print(f"   Page ID  : {page_id}")
    print(f"   Products : {len(items)}")
    print(f"   Status   : {status.upper()}")
    print("=" * 60)
    return True, summary


def main():
    parser = argparse.ArgumentParser(description="Publish one brand's food images to WordPress")
    parser.add_argument("--parent-job-id", required=True, help="Scrape job id whose output/<id>/ holds the images")
    parser.add_argument("--brand", required=True, help="Canonical brand name (e.g. 'ALDI')")
    parser.add_argument("--brand-slug", required=True, help="Output folder slug (e.g. 'aldi')")
    parser.add_argument("--page-id", required=True, help="WordPress page/post ID to update")
    parser.add_argument("--publish-target", default="retailshout", choices=sorted(PUBLISH_TARGETS))
    parser.add_argument("--output-root", default="output")
    status_group = parser.add_mutually_exclusive_group()
    status_group.add_argument("--publish", action="store_true", help="Publish live")
    status_group.add_argument("--draft", action="store_true", help="Keep as draft (default)")
    args = parser.parse_args()

    status = "publish" if args.publish else "draft"
    ok, message = publish_brand(
        parent_job_id=args.parent_job_id,
        brand=args.brand,
        brand_slug=args.brand_slug,
        page_id=args.page_id,
        publish_target=args.publish_target,
        output_root=args.output_root,
        status=status,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
