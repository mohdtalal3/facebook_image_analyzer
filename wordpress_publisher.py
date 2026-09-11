import csv
import json
import mimetypes
import os
import sys
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# Load .env automatically if available
try:
    from dotenv import load_dotenv
    # Load from project root (this file's directory)
    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    # Fallback to system environment only
    pass


class WordPressPublisher:
    """Publishes Aldi weekly ads to WordPress."""
    
    def __init__(self, wp_url: str, wp_username: str, wp_password: str, dry_run: bool = False):
        """
        Initialize WordPress publisher.
        
        Args:
            wp_url: WordPress site URL (e.g., 'https://yoursite.com')
            wp_username: WordPress username
            wp_password: WordPress application password
            dry_run: If True, simulate operations without uploading (for testing)
        """
        self.wp_url = wp_url.rstrip('/')
        self.api_base = f"{self.wp_url}/wp-json/wp/v2"
        self.auth = (wp_username, wp_password)
        self.dry_run = dry_run

        if self.dry_run:
            print("🧪 DRY RUN MODE: No uploads will be performed")
        
    def test_connection(self) -> bool:
        """Test WordPress API connection."""
        try:
            response = requests.get(f"{self.api_base}/users/me", auth=self.auth)
            if response.status_code == 200:
                user = response.json()
                print(f"✅ Connected to WordPress as: {user.get('name', 'Unknown')}")
                return True
            else:
                print(f"❌ Authentication failed: {response.status_code}")
                print(f"   Response: {response.text}")
                return False
        except Exception as e:
            print(f"❌ Connection error: {e}")
            return False
    
    def upload_image(self, image_path: Path, title: str = None) -> Optional[int]:
        """
        Upload an image to WordPress media library.
        
        Args:
            image_path: Path to the image file
            title: Optional title for the image
            
        Returns:
            Media ID if successful, None otherwise
        """
        if not image_path.exists():
            print(f"  ⚠️ Image not found: {image_path}")
            return None
        
        # Dry run mode: simulate upload
        if self.dry_run:
            print(f"  🧪 [DRY RUN] Would upload: {image_path.name}")
            return 99999  # Fake media ID for testing
        
        try:
            # Read image file
            with open(image_path, 'rb') as f:
                image_data = f.read()
            
            # Prepare headers
            mime_type = mimetypes.guess_type(str(image_path))[0] or 'image/jpeg'
            headers = {
                'Content-Type': mime_type,
                'Content-Disposition': f'attachment; filename="{image_path.name}"'
            }
            
            # Upload to WordPress
            response = requests.post(
                f"{self.api_base}/media",
                headers=headers,
                data=image_data,
                auth=self.auth
            )
            
            if response.status_code == 201:
                media = response.json()
                media_id = media['id']
                
                # Update title if provided
                if title:
                    requests.post(
                        f"{self.api_base}/media/{media_id}",
                        json={'title': title},
                        auth=self.auth
                    )
                
                print(f"  ✅ Uploaded: {image_path.name} (ID: {media_id})")
                return media_id
            else:
                print(f"  ❌ Failed to upload {image_path.name}: {response.status_code}")
                print(f"     {response.text}")
                return None
                
        except Exception as e:
            print(f"  ❌ Error uploading {image_path.name}: {e}")
            return None
    
    def update_page(
        self,
        page_id: int,
        content: str,
        title: str = None,
        featured_image_id: Optional[int] = None,
        status: str = None,
        try_page_first: bool = False,
        update_date: bool = False
    ) -> bool:
        """
        Update an existing WordPress page or post.
        
        Args:
            page_id: ID of the page/post to update
            content: Page content (HTML)
            title: Optional new title
            featured_image_id: Media ID for featured image
            status: Optional status change ('publish', 'draft', 'pending')
            try_page_first: If True, try as page first then post. If False, try post first then page (default)
            update_date: If True, update the published date to current time (default False)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            page_data = {'content': content}
            
            if title:
                page_data['title'] = title
            
            if featured_image_id:
                page_data['featured_media'] = featured_image_id
            
            if status:
                page_data['status'] = status
            
            if update_date:
                # Set published date to current PST time minus 30 minutes (to ensure immediate publish)
                pst_time = datetime.utcnow() - timedelta(hours=8, minutes=30)
                page_data['date'] = pst_time.strftime('%Y-%m-%dT%H:%M:%S')
            
            # Dry run mode: simulate update
            if self.dry_run:
                print(f"🧪 [DRY RUN] Would update page/post {page_id}")
                print(f"   Title: {title or '(no change)'}")
                print(f"   Content length: {len(content)} characters")
                print(f"   Status: {status or '(no change)'}")
                return True
            
            # Determine which endpoint to try first
            first_type = 'pages' if try_page_first else 'posts'
            second_type = 'posts' if try_page_first else 'pages'
            
            # Try first endpoint type
            response = requests.post(
                f"{self.api_base}/{first_type}/{page_id}",
                json=page_data,
                auth=self.auth
            )
            
            # If first attempt fails with 404, try the other type
            if response.status_code == 404:
                print(f"  ℹ️  {first_type.capitalize()} not found, trying as {second_type}...")
                response = requests.post(
                    f"{self.api_base}/{second_type}/{page_id}",
                    json=page_data,
                    auth=self.auth
                )
            
            response_old = response  # Keep for error reporting
            
            if response.status_code == 200:
                page = response.json()
                page_url = page['link']
                print(f"✅ Page updated successfully!")
                print(f"   Page ID: {page_id}")
                print(f"   URL: {page_url}")
                print(f"   Status: {page.get('status', 'unknown')}")
                return True
            else:
                print(f"❌ Failed to update page: {response.status_code}")
                print(f"   {response.text}")
                return False
                
        except Exception as e:
            print(f"❌ Error updating page: {e}")
            return False
    
    def create_post(
        self,
        title: str,
        content: str,
        featured_image_id: Optional[int] = None,
        categories: List[int] = None,
        tags: List[int] = None,
        status: str = "publish"
    ) -> Optional[int]:
        """
        Create a WordPress post.
        
        Args:
            title: Post title
            content: Post content (HTML)
            featured_image_id: Media ID for featured image
            categories: List of category IDs
            tags: List of tag IDs
            status: Post status ('publish', 'draft', 'pending')
            
        Returns:
            Post ID if successful, None otherwise
        """
        try:
            post_data = {
                'title': title,
                'content': content,
                'status': status
            }
            
            if featured_image_id:
                post_data['featured_media'] = featured_image_id
            
            if categories:
                post_data['categories'] = categories
            
            if tags:
                post_data['tags'] = tags
            
            # Dry run mode: simulate creation
            if self.dry_run:
                print(f"🧪 [DRY RUN] Would create post: {title}")
                print(f"   Content length: {len(content)} characters")
                print(f"   Status: {status}")
                return 99999  # Fake post ID for testing
            
            response = requests.post(
                f"{self.api_base}/posts",
                json=post_data,
                auth=self.auth
            )
            
            if response.status_code == 201:
                post = response.json()
                post_id = post['id']
                post_url = post['link']
                print(f"✅ Post created successfully!")
                print(f"   Post ID: {post_id}")
                print(f"   URL: {post_url}")
                return post_id
            else:
                print(f"❌ Failed to create post: {response.status_code}")
                print(f"   {response.text}")
                return None
                
        except Exception as e:
            print(f"❌ Error creating post: {e}")
            return None
    
    def get_or_create_category(self, name: str) -> Optional[int]:
        """Get or create a category by name."""
        try:
            # Search for existing category
            response = requests.get(
                f"{self.api_base}/categories",
                params={'search': name},
                auth=self.auth
            )
            
            if response.status_code == 200:
                categories = response.json()
                for cat in categories:
                    if cat['name'].lower() == name.lower():
                        return cat['id']
            
            # Create new category if not found
            response = requests.post(
                f"{self.api_base}/categories",
                json={'name': name},
                auth=self.auth
            )
            
            if response.status_code == 201:
                return response.json()['id']
            
        except Exception as e:
            print(f"⚠️ Error with category '{name}': {e}")
        
        return None
    
    def generate_preview_html(
        self, 
        ad_folder: Path,
        output_file: Path = None
    ) -> str:
        """
        Generate local HTML preview without uploading to WordPress.
        
        Args:
            ad_folder: Path to the folder containing scraped data
            output_file: Optional path to save HTML file
            
        Returns:
            HTML content as string
        """
        print(f"\n🎨 Generating preview for: {ad_folder.name}")
        
        # Detect flyer type
        folder_name = ad_folder.name
        if "WeeklyAd" in folder_name:
            flyer_type = "Weekly Ad"
        elif "InstoreAd" in folder_name or "Instore" in folder_name:
            flyer_type = "In-Store Ad"
        else:
            flyer_type = "Ad"
        
        # Read CSV
        csv_files = list(ad_folder.glob("*_products.csv"))
        if not csv_files:
            print(f"❌ No products CSV found in {ad_folder}")
            return None
        
        with open(csv_files[0], 'r', encoding='utf-8') as f:
            products = list(csv.DictReader(f))
        
        if not products:
            print("❌ No products found in CSV")
            return None
        
        # Extract dates
        valid_from = datetime.strptime(products[0]['valid_from'], "%Y-%m-%d")
        valid_to = datetime.strptime(products[0]['valid_to'], "%Y-%m-%d")
        date_range = f"{valid_from.strftime('%B %d')} - {valid_to.strftime('%B %d, %Y')}"
        
        title = f"Aldi {flyer_type} ({date_range})"
        
        # Build HTML with local file paths
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} - Preview</title>
</head>
<body>

<div class="aldi-ad aldi-{flyer_type.lower().replace(' ', '-').replace('-', '')}">
    <h2>Aldi {flyer_type} Valid {date_range}</h2>
    
    <div class="ad-intro">
        <p>Check out this week's amazing deals at Aldi! Browse through {len(products)} featured products with special pricing.</p>
    </div>
"""
        
        # Add flyer images
        page_images = sorted(ad_folder.glob("*_page_*.jpg"))
        if page_images:
            html += '\n    <div class="flyer-pages">\n'
            html += f'        <h3>{flyer_type} Flyer</h3>\n'
            html += '        <div class="flyer-images">\n'
            
            for idx, page_img in enumerate(page_images, 1):
                # Use relative path for local preview
                rel_path = page_img.name
                html += f'            <img src="{rel_path}" alt="Aldi {flyer_type} Page {idx}" class="flyer-page" />\n'
            
            html += '        </div>\n'
            html += '    </div>\n\n'
        
        # Add products
        html += '    <div class="weekly-deals">\n'
        html += '        <h3>Featured Products</h3>\n'
        html += '        <div class="products-grid">\n'
        
        for product in products:
            product_name = product['name']
            price = product['price']
            image_names = product['images'].split(', ')
            
            html += '            <div class="product-item">\n'
            
            if image_names and image_names[0]:
                img_name = image_names[0]
                html += f'                <div class="product-image">\n'
                html += f'                    <img src="{img_name}" alt="{product_name}" />\n'
                html += f'                </div>\n'
            
            html += f'                <div class="product-info">\n'
            html += f'                    <h4 class="product-name">{product_name}</h4>\n'
            html += f'                    <p class="product-price">{price}</p>\n'
            html += f'                </div>\n'
            html += '            </div>\n'
        
        html += '        </div>\n'
        html += '    </div>\n'
        
        # Add timestamp
        html += f'\n    <div class="last-updated">\n'
        html += f'        <p><em>Preview generated: {datetime.now().strftime("%B %d, %Y at %I:%M %p")}</em></p>\n'
        html += '    </div>\n'
        
        # Add CSS
        html += """
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 20px;
            background: #f9f9f9;
        }
        .aldi-ad {
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        .ad-intro {
            background: #f5f5f5;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 30px;
        }
        .flyer-pages {
            margin-bottom: 40px;
        }
        .flyer-images {
            display: flex;
            gap: 20px;
            flex-wrap: wrap;
            justify-content: center;
        }
        .flyer-page {
            max-width: 100%;
            height: auto;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            border-radius: 4px;
        }
        .products-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
            gap: 20px;
            margin-top: 20px;
        }
        .product-item {
            border: 1px solid #ddd;
            border-radius: 8px;
            padding: 15px;
            background: white;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .product-item:hover {
            transform: translateY(-5px);
            box-shadow: 0 6px 12px rgba(0,0,0,0.15);
        }
        .product-image {
            text-align: center;
            margin-bottom: 15px;
        }
        .product-image img {
            max-width: 100%;
            height: 200px;
            object-fit: contain;
        }
        .product-name {
            font-size: 16px;
            font-weight: bold;
            margin: 10px 0;
            color: #333;
        }
        .product-price {
            font-size: 18px;
            color: #e31c23;
            font-weight: bold;
        }
        .last-updated {
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #ddd;
            text-align: center;
            color: #666;
        }
        h2 {
            color: #003366;
            text-align: center;
            margin-bottom: 20px;
        }
        h3 {
            color: #003366;
            margin-top: 30px;
        }
    </style>
</div>

</body>
</html>
"""
        
        # Save to file if specified
        if output_file is None:
            output_file = ad_folder / f"{ad_folder.name}_preview.html"
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(html)
        
        print(f"✅ Preview saved: {output_file}")
        print(f"   Open in browser: file://{output_file.absolute()}")
        
        return html
    
    def publish_aldi_ad(
        self, 
        ad_folder: Path, 
        page_id: Optional[int] = None,
        status: str = "publish"
    ) -> Optional[int]:
        """
        Publish an Aldi ad from a scraped folder.
        
        Args:
            ad_folder: Path to the folder containing scraped data
            page_id: Optional page ID to update (if None, creates new post)
            status: Post/page status ('publish', 'draft', 'pending')
            
        Returns:
            Post/Page ID if successful, None otherwise
        """
        print(f"\n📝 Publishing ad from: {ad_folder.name}")
        
        # Detect flyer type from folder name
        folder_name = ad_folder.name
        if "WeeklyAd" in folder_name:
            flyer_type = "Weekly Ad"
        elif "InstoreAd" in folder_name or "Instore" in folder_name:
            flyer_type = "In-Store Ad"
        else:
            flyer_type = "Ad"
        
        print(f"📋 Flyer Type: {flyer_type}")
        
        # Find CSV file
        csv_files = list(ad_folder.glob("*_products.csv"))
        if not csv_files:
            print(f"❌ No products CSV found in {ad_folder}")
            return None
        
        csv_file = csv_files[0]
        
        # Read products
        products = []
        try:
            with open(csv_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                products = list(reader)
        except Exception as e:
            print(f"❌ Error reading CSV: {e}")
            return None
        
        if not products:
            print("❌ No products found in CSV")
            return None
        
        # Extract dates from first product
        valid_from = datetime.strptime(products[0]['valid_from'], "%Y-%m-%d")
        valid_to = datetime.strptime(products[0]['valid_to'], "%Y-%m-%d")
        date_range = f"{valid_from.strftime('%B %d')} - {valid_to.strftime('%B %d, %Y')}"
        
        # Create title
        title = f"Aldi {flyer_type} ({date_range})"
        
        print(f"📰 Title: {title}")
        print(f"📦 Processing {len(products)} products...")
        
        # Upload flyer page images for featured image
        page_images = sorted(ad_folder.glob("*_page_*.jpg"))
        featured_image_id = None
        
        if page_images:
            print(f"\n📸 Uploading flyer pages...")
            featured_image_id = self.upload_image(
                page_images[0],
                title=f"Aldi {flyer_type} {date_range} - Page 1"
            )
        
        # Build content
        content = f"""
<div class="aldi-ad aldi-{flyer_type.lower().replace(' ', '-').replace('-', '')}">
    <h2>Aldi {flyer_type} Valid {date_range}</h2>
    
    <div class="ad-intro">
        <p>Check out this week's amazing deals at Aldi! Browse through {len(products)} featured products with special pricing.</p>
    </div>
"""
        
        # Add flyer images section
        if page_images:
            content += '\n    <div class="flyer-pages">\n'
            content += f'        <h3>{flyer_type} Flyer</h3>\n'
            content += '        <div class="flyer-images">\n'
            
            for idx, page_img in enumerate(page_images, 1):
                img_id = self.upload_image(
                    page_img,
                    title=f"Aldi {flyer_type} {date_range} - Page {idx}"
                )
                if img_id:
                    # Get image URL
                    resp = requests.get(f"{self.api_base}/media/{img_id}", auth=self.auth)
                    if resp.status_code == 200:
                        img_url = resp.json()['source_url']
                        content += f'            <img src="{img_url}" alt="Aldi {flyer_type} Page {idx}" class="flyer-page" />\n'
            
            content += '        </div>\n'
            content += '    </div>\n\n'
        
        # Add products section
        content += '    <div class="weekly-deals">\n'
        content += '        <h3>Featured Products</h3>\n'
        content += '        <div class="products-grid">\n'
        
        print(f"\n📸 Uploading product images...")
        for idx, product in enumerate(products, 1):
            product_name = product['name']
            price = product['price']
            image_names = product['images'].split(', ')
            
            # Upload product image
            product_img_id = None
            if image_names and image_names[0]:
                img_path = ad_folder / image_names[0]
                product_img_id = self.upload_image(img_path, title=product_name)
            
            # Add product to content
            content += '            <div class="product-item">\n'
            
            if product_img_id:
                resp = requests.get(f"{self.api_base}/media/{product_img_id}", auth=self.auth)
                if resp.status_code == 200:
                    img_url = resp.json()['source_url']
                    content += f'                <div class="product-image">\n'
                    content += f'                    <img src="{img_url}" alt="{product_name}" />\n'
                    content += f'                </div>\n'
            
            content += f'                <div class="product-info">\n'
            content += f'                    <h4 class="product-name">{product_name}</h4>\n'
            content += f'                    <p class="product-price">{price}</p>\n'
            content += f'                </div>\n'
            content += '            </div>\n'
            
            if idx % 5 == 0:
                print(f"    🕓 Processed {idx}/{len(products)} products...")
        
        content += '        </div>\n'
        content += '    </div>\n'
        
        # Add last updated timestamp
        content += f'\n    <div class="last-updated">\n'
        content += f'        <p><em>Last updated: {datetime.now().strftime("%B %d, %Y at %I:%M %p")}</em></p>\n'
        content += '    </div>\n'
        
        # Add CSS for styling
        content += """
    <style>
        .aldi-ad {
            font-family: Arial, sans-serif;
            max-width: 1200px;
            margin: 0 auto;
        }
        .ad-intro {
            background: #f5f5f5;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 30px;
        }
        .flyer-pages {
            margin-bottom: 40px;
        }
        .flyer-images {
            display: flex;
            gap: 20px;
            flex-wrap: wrap;
            justify-content: center;
        }
        .flyer-page {
            max-width: 100%;
            height: auto;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            border-radius: 4px;
        }
        .products-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
            gap: 20px;
            margin-top: 20px;
        }
        .product-item {
            border: 1px solid #ddd;
            border-radius: 8px;
            padding: 15px;
            background: white;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .product-item:hover {
            transform: translateY(-5px);
            box-shadow: 0 6px 12px rgba(0,0,0,0.15);
        }
        .product-image {
            text-align: center;
            margin-bottom: 15px;
        }
        .product-image img {
            max-width: 100%;
            height: 200px;
            object-fit: contain;
        }
        .product-name {
            font-size: 16px;
            font-weight: bold;
            margin: 10px 0;
            color: #333;
        }
        .product-price {
            font-size: 18px;
            color: #e31c23;
            font-weight: bold;
        }
        .last-updated {
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #ddd;
            text-align: center;
            color: #666;
        }
    </style>
</div>
"""
        
        # Update page or create post
        if page_id:
            print(f"\n📤 Updating WordPress page {page_id}...")
            success = self.update_page(
                page_id=page_id,
                content=content,
                title=title,
                featured_image_id=featured_image_id,
                status=status
            )
            return page_id if success else None
        else:
            print("\n📤 Creating new WordPress post...")
            # Get or create categories
            category_id = self.get_or_create_category("Weekly Ads")
            aldi_category_id = self.get_or_create_category("Aldi")
            
            categories = [cat_id for cat_id in [category_id, aldi_category_id] if cat_id]
            
            post_id = self.create_post(
                title=title,
                content=content,
                featured_image_id=featured_image_id,
                categories=categories,
                status=status
            )
            return post_id


def main():
    """Example usage."""
    
    # Get WordPress credentials from environment variables (after auto .env load)
    wp_url = os.environ.get('WP_URL')
    wp_username = os.environ.get('WP_USERNAME')
    wp_password = os.environ.get('WP_PASSWORD')
    
    if not all([wp_url, wp_username, wp_password]):
        print("❌ Missing WordPress credentials!")
        print("   Set WP_URL, WP_USERNAME, and WP_PASSWORD environment variables")
        sys.exit(1)
    
    # Initialize publisher
    publisher = WordPressPublisher(wp_url, wp_username, wp_password)
    
    # Test connection
    if not publisher.test_connection():
        sys.exit(1)
    
    # Find latest ad folder
    script_dir = Path(__file__).parent
    data_dir = script_dir / "scraping_data" / "aldi"
    current_dir = data_dir if data_dir.exists() else script_dir
    ad_folders = [d for d in current_dir.iterdir() if d.is_dir() and "Aldi_" in d.name and "Ad_" in d.name]
    
    if not ad_folders:
        print("❌ No Aldi ad folders found")
        sys.exit(1)
    
    # Sort by modification time (newest first)
    ad_folders.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    latest_folder = ad_folders[0]
    
    # Publish the latest ad
    publisher.publish_aldi_ad(latest_folder)


if __name__ == "__main__":
    main()

