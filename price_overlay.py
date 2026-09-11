"""Price overlay — stamps a scraped product price (e.g. "$4.99") onto a food
image as a red rounded badge with white bold text in the top-right corner,
before the image is uploaded to WordPress. Uses Pillow (already a project
dependency via image_pipeline.py)."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BADGE_RED = (212, 0, 0)
BADGE_OUTLINE = (255, 255, 255)
TEXT_COLOR = (255, 255, 255)

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",   # macOS
    "/System/Library/Fonts/Helvetica.ttc",                 # macOS
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Linux
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",                        # Windows
]


def _load_font(size: int):
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)  # Pillow >= 10 supports size
    except TypeError:
        return ImageFont.load_default()


def overlay_price_on_image(image_path: Path, price: str) -> bool:
    """Stamp `price` (e.g. '$4.99') onto the top-right of the image as a red
    rounded badge with white text, saving in place. Returns True on success;
    never raises — a failed overlay just leaves the image unbadged."""
    try:
        img = Image.open(image_path).convert("RGB")
    except Exception as e:
        print(f"  ⚠️  Could not open {image_path.name} for price overlay: {e}")
        return False

    width, height = img.size
    font_size = max(18, width // 12)
    try:
        font = _load_font(font_size)
    except Exception:
        font = ImageFont.load_default()

    draw = ImageDraw.Draw(img)
    text = str(price).strip()
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
    except Exception:
        text_w, text_h = int(font_size * 0.6 * len(text)), font_size

    pad = max(10, width // 40)
    margin = max(12, width // 30)
    box_w = text_w + pad * 2
    box_h = text_h + pad
    x0 = max(0, width - margin - box_w)
    y0 = margin
    x1 = width - margin
    y1 = y0 + box_h

    draw.rounded_rectangle(
        [x0, y0, x1, y1], radius=max(6, (y1 - y0) // 4),
        fill=BADGE_RED, outline=BADGE_OUTLINE, width=max(2, width // 300),
    )
    try:
        draw.text(((x0 + x1) // 2, (y0 + y1) // 2), text,
                  font=font, fill=TEXT_COLOR, anchor="mm")
    except Exception:
        draw.text((x0 + pad, y0 + pad // 2), text, font=font, fill=TEXT_COLOR)

    try:
        img.save(image_path, "JPEG", quality=92)
        return True
    except Exception as e:
        print(f"  ⚠️  Could not save price overlay for {image_path.name}: {e}")
        return False
