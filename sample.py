#!/usr/bin/env python3
"""
Publish Winn-Dixie Weekly Ad to WordPress (retailshout.com)

Creates a single page containing:
1. Title (banner line1 + date range)
2. Feature image
3. Table of Contents with all unique category names
4. Weekend Sale section: simple containers with deal title + product image
5. Other deals sections: category name + flyer image + product list (bold deal + description)
6. Show more/less for categories with >20 items

- Auto-detects the latest output folder (output/YYYY-MM-DD/)
- Uses CSS classes compatible with existing publishers
- Uploads images with caching
- Updates WordPress page on retailshout.com

Usage:
  python publisher.py --draft
  python publisher.py --publish
  python publisher.py --date 2026-08-25 --publish
"""

import re
import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

# Add parent directory to path to import wordpress_publisher
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load .env file explicitly
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except Exception:
    pass

from wordpress_publisher import WordPressPublisher

# Constants
OUTPUT_DIR = Path(__file__).parent.parent / "scraping_data" / "winndixiebogo"
PAGE_MAPPING_FILE = Path(__file__).parent.parent / "page_mapping.json"
ITEMS_PER_CATEGORY_LIMIT = 20


def find_latest_output_folder(date_hint: Optional[str] = None) -> Optional[Path]:
    """Find the latest output/YYYY-MM-DD/ folder or a specific one by date."""
    if not OUTPUT_DIR.exists():
        print(f"❌ Output directory not found: {OUTPUT_DIR}")
        return None

    folders = [d for d in OUTPUT_DIR.iterdir() if d.is_dir()]

    if not folders:
        print("❌ No output folders found")
        return None

    if date_hint:
        for folder in folders:
            if date_hint in folder.name:
                return folder
        print(f"❌ No folder found matching date: {date_hint}")
        return None

    folders.sort(key=lambda f: f.name, reverse=True)
    return folders[0]


def load_json(folder: Path, filename: str) -> list:
    """Load a JSON file from the folder."""
    filepath = folder / filename
    if not filepath.exists():
        print(f"⚠️  File not found: {filepath}")
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_date_range(list_name: str) -> str:
    """Extract date range from list_name like 'Winn-Dixie Ad & Coupons: 8/19-8/25'."""
    match = re.search(r"(\d+/\d+)\s*[-–]\s*(\d+/\d+)", list_name)
    if match:
        return f"{match.group(1)} - {match.group(2)}"
    return ""


def get_page_id_from_mapping() -> Optional[int]:
    """Read Winn-Dixie page ID from page_mapping.json."""
    if not PAGE_MAPPING_FILE.exists():
        return None
    try:
        with open(PAGE_MAPPING_FILE, "r") as f:
            data = json.load(f)
            return data.get("page_mappings", {}).get("WinnDixieBogo")
    except Exception as e:
        print(f"⚠️  Error reading page mapping: {e}")
        return None


def upload_and_get_url(publisher: WordPressPublisher, img_path: Path, title: str) -> Optional[str]:
    """Upload image and return its URL."""
    if not img_path.exists():
        return None

    media_id = publisher.upload_image(img_path, title=title)
    if not media_id:
        return None

    import requests
    try:
        resp = requests.get(f"{publisher.api_base}/media/{media_id}", auth=publisher.auth)
        if resp.status_code == 200:
            return resp.json().get("source_url")
    except Exception:
        pass

    return None


def slugify(text: str) -> str:
    """Create a URL-safe anchor from category name."""
    return text.lower().replace(" ", "-").replace("&", "and").replace("/", "-").replace("--", "-")


def build_toc(weekend_categories: List[str], other_categories: List[str]) -> str:
    """Build table of contents HTML with all unique category names."""
    toc_items = []

    for category in weekend_categories:
        anchor = slugify(category)
        toc_items.append(f'<li><a href="#finds-cat-{anchor}">{category}</a></li>')

    for category in other_categories:
        anchor = slugify(category)
        toc_items.append(f'<li><a href="#finds-cat-{anchor}">{category}</a></li>')

    if not toc_items:
        return ""

    return f"""  <div id="toc" class="toc">
    <h3 class="toc-title">Jump to Category</h3>
    <ul class="toc-list">
      {"".join(toc_items)}
    </ul>
  </div>"""


def render_weekend_sale_item(
    item_index: int,
    deal: dict,
    folder: Path,
    publisher: WordPressPublisher,
    defer_image: bool = False
) -> str:
    """Render a single Weekend Sale deal: title + product image."""
    deal_text = deal.get("deal", "")
    product_image = deal.get("product_image", "")
    deal_id = deal.get("deal_id", "")

    img_attr = "data-src" if defer_image else "src"
    lazy_class = "lazy-load" if defer_image else ""

    html = f'    <div class="finds-item">'
    html += f'<div class="finds-item-header">'
    html += f'<span class="finds-item-title">{item_index}) {deal_text}</span>'
    html += '</div>'

    if product_image:
        img_path = Path(product_image)
        img_url = upload_and_get_url(publisher, img_path, deal_text)
        if img_url:
            img_id = f"img_{deal_id}_{item_index}"
            html += f'<div style="text-align: center; margin-top: 10px; padding: 10px;">'
            html += f'<img id="{img_id}" {img_attr}="{img_url}" alt="{deal_text}" style="max-width: 300px; max-height: 300px; width: auto; height: auto; border-radius: 4px;" class="{lazy_class}" />'
            html += '</div>'

    html += '</div>'
    return html


def build_weekend_sale_section(
    weekend_data: list,
    folder: Path,
    publisher: WordPressPublisher
) -> str:
    """Build the Weekend Sale section with simple containers."""
    html = ""

    for category in weekend_data:
        cat_name = category.get("category_name", "Weekend Sale")
        products = category.get("products", [])
        anchor = slugify(cat_name)

        html += f'\n  <h3 id="finds-cat-{anchor}" class="aos-category-title">{cat_name}</h3>'
        html += f'\n  <div class="aos-category-items">'

        visible_items = products[:ITEMS_PER_CATEGORY_LIMIT]
        hidden_items = products[ITEMS_PER_CATEGORY_LIMIT:] if len(products) > ITEMS_PER_CATEGORY_LIMIT else []

        item_index = 1
        for deal in visible_items:
            html += "\n" + render_weekend_sale_item(item_index, deal, folder, publisher, defer_image=False)
            item_index += 1

        if hidden_items:
            more_id = f"finds-more-{anchor}"
            html += f'\n  </div>'
            html += f'\n  <div class="aos-more" id="{more_id}" style="display:none;">'
            html += f'\n    <div class="aos-category-items">'

            for deal in hidden_items:
                html += "\n" + render_weekend_sale_item(item_index, deal, folder, publisher, defer_image=True)
                item_index += 1

            html += f'\n    </div>'
            html += f'\n  </div>'
            html += make_show_more_button(anchor, cat_name, len(hidden_items))
        else:
            html += '\n  </div>'

        html += f'\n  <div style="text-align: center; margin-top: 15px;">'
        html += f'\n    <a href="#toc" style="color: #810e0e; text-decoration: underline; font-weight: bold;">Back to TOP</a>'
        html += f'\n  </div>'

    return html


def render_other_deal_item(
    item_index: int,
    deal: dict,
    folder: Path,
    publisher: WordPressPublisher,
    defer_image: bool = False
) -> str:
    """Render a single other deal item: bold deal text + description + product image."""
    deal_text = deal.get("deal", "")
    description = deal.get("description", "")
    product_image = deal.get("product_image", "")
    deal_id = deal.get("deal_id", "")

    img_attr = "data-src" if defer_image else "src"
    lazy_class = "lazy-load" if defer_image else ""

    html = f'    <div class="finds-item">'
    html += f'<div style="padding: 8px 10px;">'
    html += f'<span style="font-weight: bold; font-size: 1em; color: #333;">{item_index}) {deal_text}</span>'

    if description:
        desc_id = f"desc_{deal_id}_{item_index}"
        desc_lines = [l.strip() for l in description.split("\n") if l.strip()]
        desc_html = "".join(f'<div>{line}</div>' for line in desc_lines)

        is_long = len(desc_lines) > 2 or any(len(l) > 110 for l in desc_lines)

        clamp_style = ("display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; "
                       "overflow: hidden;")

        if not is_long:
            html += f'<div style="margin-top: 4px; color: #666; font-size: 0.9em; line-height: 1.4;">{desc_html}</div>'
        else:
            html += f'<div style="margin-top: 4px; color: #666; font-size: 0.9em; line-height: 1.4;">'
            html += f'<div id="{desc_id}" style="{clamp_style}">{desc_html}</div>'
            html += (f' <a href="javascript:void(0)" onclick="var d=document.getElementById(\'{desc_id}\');'
                     f'if(d.style.webkitLineClamp===\'2\'||d.style.webkitLineClamp===\'\')'
                     f'{{d.style.webkitLineClamp=\'unset\';d.style.display=\'block\';this.textContent=\' Show less\'}}'
                     f'else{{d.style.webkitLineClamp=\'2\';d.style.display=\'-webkit-box\';this.textContent=\' Show more\'}}" '
                     f'style="color: #0071ce; font-size: 0.85em; cursor: pointer; text-decoration: underline;"> Show more</a>')
            html += '</div>'

    if product_image:
        img_path = Path(product_image)
        img_url = upload_and_get_url(publisher, img_path, deal_text)
        if img_url:
            img_id = f"img_{deal_id}_{item_index}"
            html += f'<div style="text-align: center; margin-top: 10px; padding: 10px;">'
            html += f'<img id="{img_id}" {img_attr}="{img_url}" alt="{deal_text}" style="max-width: 300px; max-height: 300px; width: auto; height: auto; border-radius: 4px;" class="{lazy_class}" />'
            html += '</div>'

    html += '</div>'
    html += '</div>'
    return html


def build_other_deals_section(
    other_data: list,
    folder: Path,
    publisher: WordPressPublisher
) -> str:
    """Build other deals sections: category name + flyer image + product list."""
    html = ""

    for category in other_data:
        cat_name = category.get("category_name", "")
        flyer_image_path = category.get("flyer_image", "")
        products = category.get("products", [])
        anchor = slugify(cat_name)

        html += f'\n  <h3 id="finds-cat-{anchor}" class="aos-category-title">{cat_name}</h3>'

        # Flyer image
        if flyer_image_path:
            img_path = Path(flyer_image_path)
            flyer_url = upload_and_get_url(publisher, img_path, f"{cat_name} Flyer")
            if flyer_url:
                html += f'\n  <div style="text-align: center; margin: 15px 0;">'
                html += f'<img src="{flyer_url}" alt="{cat_name} Flyer" style="max-width: 100%; height: auto; border-radius: 8px;" />'
                html += f'\n  </div>'

        # Product list
        html += f'\n  <div class="aos-category-items">'

        visible_items = products[:ITEMS_PER_CATEGORY_LIMIT]
        hidden_items = products[ITEMS_PER_CATEGORY_LIMIT:] if len(products) > ITEMS_PER_CATEGORY_LIMIT else []

        item_index = 1
        for deal in visible_items:
            html += "\n" + render_other_deal_item(item_index, deal, folder, publisher, defer_image=False)
            item_index += 1

        if hidden_items:
            more_id = f"finds-more-{anchor}"
            html += f'\n  </div>'
            html += f'\n  <div class="aos-more" id="{more_id}" style="display:none;">'
            html += f'\n    <div class="aos-category-items">'

            for deal in hidden_items:
                html += "\n" + render_other_deal_item(item_index, deal, folder, publisher, defer_image=True)
                item_index += 1

            html += f'\n    </div>'
            html += f'\n  </div>'
            html += make_show_more_button(anchor, cat_name, len(hidden_items))
        else:
            html += '\n  </div>'

        html += f'\n  <div style="text-align: center; margin-top: 15px;">'
        html += f'\n    <a href="#toc" style="color: #810e0e; text-decoration: underline; font-weight: bold;">Back to TOP</a>'
        html += f'\n  </div>'

    return html


def make_show_more_button(anchor: str, label: str, hidden_count: int) -> str:
    """Generate a show more/less button with inline onclick (no external JS needed)."""
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


def generate_html_content(
    folder: Path,
    weekend_data: list,
    other_data: list,
    publisher: WordPressPublisher
) -> str:
    """Generate complete HTML content for the WordPress page."""
    # Extract list_name and date range
    list_name = ""
    if weekend_data and weekend_data[0].get("products"):
        list_name = weekend_data[0]["products"][0].get("list_name", "")
    if not list_name and other_data and other_data[0].get("products"):
        list_name = other_data[0]["products"][0].get("list_name", "")

    date_range = extract_date_range(list_name)
    line1 = "Weekend Sale & BOGO Deals"
    title = f"{line1} {date_range}".strip() if date_range else line1

    # Collect all unique category names for TOC
    weekend_cat_names = [c.get("category_name", "") for c in weekend_data if c.get("category_name")]
    other_cat_names = [c.get("category_name", "") for c in other_data if c.get("category_name")]

    # Total product count
    total_products = sum(len(c.get("products", [])) for c in weekend_data)
    total_products += sum(len(c.get("products", [])) for c in other_data)

    html = f"""<div id="top" class="aos-finds">
"""

    # Title
    html += f'  <h2 style="text-align: center; color: #e31c23; font-weight: bold;">{title}</h2>\n'

    # Feature image
    feature_image_path = folder / "feature_image_text.jpeg"
    feature_url = upload_and_get_url(publisher, feature_image_path, "Winn-Dixie Feature Image")
    if feature_url:
        html += f'  <div style="text-align: center; margin: 20px 0;">\n'
        html += f'    <img src="{feature_url}" alt="{title}" style="max-width: 100%; height: auto; border-radius: 8px;" />\n'
        html += f'  </div>\n'

    # Individual products count
    html += f'  <p style="text-align: center; font-style: italic;">Individual products ({total_products} items)</p>\n'

    # Table of Contents
    html += build_toc(weekend_cat_names, other_cat_names)
    html += "\n\n"

    # Weekend Sale section
    if weekend_data:
        html += build_weekend_sale_section(weekend_data, folder, publisher)
        html += "\n"

    # Other deals sections
    if other_data:
        html += build_other_deals_section(other_data, folder, publisher)
        html += "\n"

    html += """</div>"""

    return html


def publish_winndixie_page(
    folder: Path,
    page_id: int,
    weekend_data: list,
    other_data: list,
    publisher: WordPressPublisher,
    status: str = "draft"
) -> bool:
    """Publish the Winn-Dixie Weekly Ad page."""

    if not weekend_data and not other_data:
        print("⚠️  No data to publish")
        return False

    total_products = sum(len(c.get("products", [])) for c in weekend_data)
    total_products += sum(len(c.get("products", [])) for c in other_data)

    print(f"✨ {total_products} products across {len(weekend_data)} weekend + {len(other_data)} other categories")

    for cat in weekend_data:
        count = len(cat.get("products", []))
        print(f"   • [Weekend] {cat.get('category_name', '?')}: {count} items")

    for cat in other_data:
        count = len(cat.get("products", []))
        print(f"   • [Other] {cat.get('category_name', '?')}: {count} items")

    print(f"\n🎨 Generating HTML content...")
    html_content = generate_html_content(folder, weekend_data, other_data, publisher)

    print(f"\n📤 Updating WordPress page {page_id} (content only, updating date)...")
    success = publisher.update_page(
        page_id=page_id,
        content=html_content,
        status=status,
        try_page_first=True,
        update_date=True
    )

    return success


def publish_ad(date_hint: Optional[str] = None, status: str = "draft") -> bool:
    """Main function to publish Winn-Dixie Weekly Ad page."""

    wp_url = os.environ.get("WP_URL_RS")
    wp_username = os.environ.get("WP_USERNAME_RS")
    wp_password = os.environ.get("WP_PASSWORD_RS")

    if not all([wp_url, wp_username, wp_password]):
        print("❌ Missing WordPress credentials for retailshout.com!")
        print("   Set WP_URL_RS, WP_USERNAME_RS, and WP_PASSWORD_RS environment variables")
        return False

    publisher = WordPressPublisher(wp_url, wp_username, wp_password)

    if not publisher.test_connection():
        return False

    folder = find_latest_output_folder(date_hint)
    if not folder:
        return False

    print(f"\n📁 Using folder: {folder.name}")

    weekend_data = load_json(folder, "weekend_sale_deals.json")
    other_data = load_json(folder, "other_deals.json")

    if not weekend_data and not other_data:
        print("❌ No deal data found")
        return False

    print(f"📦 Loaded {sum(len(c.get('products', [])) for c in weekend_data)} weekend sale deals")
    print(f"📦 Loaded {sum(len(c.get('products', [])) for c in other_data)} other deals")

    page_id = get_page_id_from_mapping()

    if not page_id:
        print("❌ Missing page ID in page_mapping.json")
        print("   Add 'WinnDixieBogo' entry to page_mappings with the WordPress page ID")
        return False

    print(f"📄 Winn-Dixie Ad Page ID: {page_id}")

    print("\n" + "=" * 60)
    print("Publishing Winn-Dixie Weekly Ad Page")
    print("=" * 60)

    success = publish_winndixie_page(folder, page_id, weekend_data, other_data, publisher, status)

    return success


def main():
    """Entry point."""
    parser = argparse.ArgumentParser(description="Publish Winn-Dixie Weekly Ad to WordPress (retailshout.com)")
    parser.add_argument("--date", help="Date hint (e.g., 2026-08-25)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--publish", action="store_true", help="Publish live")
    group.add_argument("--draft", action="store_true", help="Keep as draft (default)")

    args = parser.parse_args()

    status = "publish" if args.publish else "draft"

    success = publish_ad(date_hint=args.date, status=status)

    if success:
        print("\n✅ Winn-Dixie Weekly Ad published successfully!")
        print("   • Page updated on retailshout.com")
    else:
        print("\n❌ Publishing failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
