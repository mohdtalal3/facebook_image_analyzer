import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

import kie_vision
from constants import AI_IMAGE_COMPARE, AI_IMAGE_MAX_BYTES
from data_store import load_workspaces
from image_pipeline import compress_under_limit

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
KIE_API_KEY = os.getenv("KIE_API_KEY", "")

MAX_RETRIES = 4

UPLOAD_URL = "https://kieai.redpandaai.co/api/file-stream-upload"
CREATE_TASK_URL = "https://api.kie.ai/api/v1/jobs/createTask"
GET_TASK_URL = "https://api.kie.ai/api/v1/jobs/recordInfo"

HEADERS_AUTH = {"Authorization": f"Bearer {KIE_API_KEY}"}

PROMPT_FILE = os.path.join(os.path.dirname(__file__), "prompt.txt")


def load_prompt(prompt_file: str = None) -> str:
    path = prompt_file or PROMPT_FILE
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def upload_image(file_path: str) -> str:
    """Upload a local image and return its public URL.

    Delegates to kie_vision.upload_image — same KIE file-stream-upload
    endpoint, one shared implementation (retry + shared account-wide rate
    limiting) for both the analysis and AI-generation paths."""
    return kie_vision.upload_image(file_path, upload_path="screenshots", mime="image/png")


def create_task(image_url: str, prompt_file: str = None, prompt: str = None) -> str:
    payload = {
        "model": "nano-banana-2-lite",
        "input": {
            "prompt": prompt if prompt is not None else load_prompt(prompt_file),
            "image_input": [image_url],
            "aspect_ratio": "auto"
        },
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                CREATE_TASK_URL,
                headers={**HEADERS_AUTH, "Content-Type": "application/json"},
                json=payload,
            )
            if response.status_code == 429:
                # KIE rejects (does not queue) requests over the account's
                # 20-per-10s cap — back off a full window, not the generic 3s.
                print(f"  create_task rate-limited (429), attempt {attempt}/{MAX_RETRIES} — waiting 11s")
                if attempt == MAX_RETRIES:
                    response.raise_for_status()
                time.sleep(11)
                continue
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 200:
                raise RuntimeError(f"Task creation failed: {data}")
            return data["data"]["taskId"]
        except Exception as e:
            print(f"  create_task attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt == MAX_RETRIES:
                raise
            time.sleep(3 * attempt)


def poll_task(task_id: str, timeout: int = 800) -> str:
    """Poll until task completes and return the result image URL."""
    start = time.time()
    interval = 15
    while time.time() - start < timeout:
        response = requests.get(
            GET_TASK_URL,
            headers=HEADERS_AUTH,
            params={"taskId": task_id},
        )
        response.raise_for_status()
        resp = response.json()
        app_code = resp.get("code")
        if app_code not in (200, None):
            raise RuntimeError(f"Poll error (code {app_code}): {resp.get('msg')}")
        data = resp.get("data", {})
        state = data.get("state")

        if state == "success":
            result = json.loads(data["resultJson"])
            return result["resultUrls"][0]
        elif state == "fail":
            raise RuntimeError(f"Task failed [{data.get('failCode', '')}]: {data.get('failMsg')}")

        print(f"  [{task_id}] state={state}, waiting {interval}s...")
        time.sleep(interval)
        interval = min(interval + 5, 30)

    raise TimeoutError(f"Task {task_id} timed out after {timeout}s")


def download_image(url: str, output_path: str):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            with open(output_path, "wb") as f:
                f.write(response.content)
            return
        except Exception as e:
            print(f"  download_image attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt == MAX_RETRIES:
                raise
            time.sleep(3 * attempt)


# ── CLI: one-shot generation using a brand's workspace image prompt ──────────

LABEL_BAND_BG = (30, 30, 30)
LABEL_BAND_FG = (255, 255, 255)

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",   # macOS
    "/System/Library/Fonts/Helvetica.ttc",                 # macOS
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Linux
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",                        # Windows
]


def _load_label_font(size: int):
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)  # Pillow >= 10 supports size
    except TypeError:
        return ImageFont.load_default()


def make_comparison_image(ai_path, original_path, out_path) -> bool:
    """Build a comparison sheet: the AI-generated image on TOP and the ORIGINAL
    below it, widths normalized, each preceded by a dark label band
    ("AI GENERATED" / "ORIGINAL"). Saved as JPEG to `out_path`. Returns True
    on success; never raises — a failed build just leaves `out_path` absent."""
    try:
        from PIL import Image, ImageDraw
        ai = Image.open(ai_path).convert("RGB")
        orig = Image.open(original_path).convert("RGB")
    except Exception as e:
        print(f"  ⚠️  Could not build comparison image: {e}")
        return False

    width = max(ai.width, orig.width)

    def _fit(img):
        if img.width != width:
            img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
        return img

    ai, orig = _fit(ai), _fit(orig)
    band_h = max(30, width // 22)
    canvas = Image.new("RGB", (width, ai.height + orig.height + band_h * 2), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    font = _load_label_font(max(16, band_h // 2))

    y = 0
    for label, img in (("AI GENERATED", ai), ("ORIGINAL", orig)):
        draw.rectangle([0, y, width, y + band_h], fill=LABEL_BAND_BG)
        try:
            draw.text((width // 2, y + band_h // 2), label, font=font,
                      fill=LABEL_BAND_FG, anchor="mm")
        except Exception:
            draw.text((10, y + band_h // 3), label, font=font, fill=LABEL_BAND_FG)
        y += band_h
        canvas.paste(img, (0, y))
        y += img.height

    try:
        canvas.save(out_path, "JPEG", quality=92)
        return True
    except Exception as e:
        print(f"  ⚠️  Could not save comparison image: {e}")
        return False

def _render_prompt(template: str, brand: str, product_name: str = "") -> str:
    """Same convention as run_brand_job.render_image_prompt (small helpers are
    duplicated across modules in this codebase): strip '#' comment lines,
    then substitute {store} and {product_name}."""
    body = "\n".join(
        line for line in template.splitlines()
        if not line.lstrip().startswith("#")
    )
    return body.replace("{store}", brand).replace("{product_name}", product_name or "").strip()


def find_brand_prompt(brand: str, workspace: str | None = None) -> str | None:
    """Look up a brand's image_prompt in data/workspaces.json (the same
    per-brand config the brand jobs use). `workspace` optionally narrows the
    search by workspace id or name; otherwise the first workspace that
    configures the brand with a non-empty prompt wins."""
    for ws in load_workspaces():
        if workspace and workspace not in (ws.get("id"), ws.get("name")):
            continue
        for b in ws.get("brands") or []:
            if b.get("brand") == brand and (b.get("image_prompt") or "").strip():
                return b["image_prompt"].strip()
    return None


def generate_image(image_path: str, brand: str, product_name: str = "",
                   output_path: str | None = None) -> str:
    """Generate one AI image for `image_path` using the brand's workspace
    image_prompt. Same pipeline as the brand job's AI stage: compress the
    upload copy under AI_IMAGE_MAX_BYTES → upload → rate-limited createTask
    → poll → download → compress the result. Returns the output path."""
    img_path = Path(image_path)
    if not img_path.exists():
        raise SystemExit(f"❌ Image not found: {img_path}")

    prompt_template = find_brand_prompt(brand)
    if not prompt_template:
        raise SystemExit(
            f"❌ No image_prompt configured for brand '{brand}' in data/workspaces.json "
            f"(set it on the workspace form's brand row)")
    prompt = _render_prompt(prompt_template, brand, product_name)

    out_path = Path(output_path) if output_path else img_path.with_name(f"{img_path.stem}_ai{img_path.suffix or '.jpg'}")

    print(f"Brand   : {brand}")
    print(f"Image   : {img_path}")
    print(f"Output  : {out_path}")
    print(f"Prompt  : {prompt[:120]}{'…' if len(prompt) > 120 else ''}")

    # Upload copy (compressed, never touches the original)
    upload_path = img_path.with_name(f".generate_upload_{img_path.name}")
    try:
        shutil.copyfile(img_path, upload_path)
        compress_under_limit(str(upload_path), AI_IMAGE_MAX_BYTES)
        public_url = upload_image(str(upload_path))
        print(f"Uploaded: {public_url}")

        kie_vision.rate_limiter.acquire()
        task_id = create_task(public_url, prompt=prompt)
        print(f"Task    : {task_id}")
        result_url = poll_task(task_id)
        print(f"Result  : {result_url}")

        if AI_IMAGE_COMPARE:
            # Comparison sheet: AI result on top, original below, both labeled.
            raw_path = img_path.with_name(f".generate_result_{img_path.name}")
            try:
                download_image(result_url, str(raw_path))
                compress_under_limit(str(raw_path), AI_IMAGE_MAX_BYTES)
                if make_comparison_image(raw_path, img_path, out_path):
                    print(f"✅ Comparison image (AI top / original below) saved to {out_path}")
                    return str(out_path)
                print("  falling back to the plain AI image")
            finally:
                raw_path.unlink(missing_ok=True)

        download_image(result_url, str(out_path))
        compress_under_limit(str(out_path), AI_IMAGE_MAX_BYTES)
        print(f"✅ AI image saved to {out_path}")
        return str(out_path)
    finally:
        upload_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(
        description="Generate one AI image via KIE nano-banana, using the brand's "
                    "image_prompt from data/workspaces.json")
    parser.add_argument("--image", required=True, help="Path to the source image")
    parser.add_argument("--brand", required=True, help="Canonical brand name (e.g. 'ALDI') — "
                        "its workspace image_prompt is used as the generation prompt")
    parser.add_argument("--workspace", default=None,
                        help="Optional workspace id or name to disambiguate when several "
                             "workspaces configure the same brand")
    parser.add_argument("--product-name", default="",
                        help="Optional product name substituted into {product_name} in the prompt")
    parser.add_argument("--out", default=None,
                        help="Output path (default: <image>_ai.<ext> next to the source)")
    args = parser.parse_args()

    prompt_template = find_brand_prompt(args.brand, args.workspace)
    if not prompt_template:
        hint = f" in workspace '{args.workspace}'" if args.workspace else ""
        print(f"❌ No image_prompt configured for brand '{args.brand}'{hint} "
              f"in data/workspaces.json — set it on the workspace form's brand row.")
        sys.exit(1)

    generate_image(args.image, args.brand, product_name=args.product_name, output_path=args.out)


if __name__ == "__main__":
    main()

