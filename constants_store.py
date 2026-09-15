"""Read/rewrite the editable pipeline constants in constants.py from the web
UI's Settings tab. Only keys listed in EDITABLE can be read or written —
everything else in constants.py is untouched. Values are written back as
Python literals on the same line, preserving any trailing comment.

Note: the Flask process imports constants at startup, but the pipeline runs
as the fresh run_facebook.py subprocess that re-imports
constants.py on every launch — so saved changes apply to newly launched jobs
without restarting the app."""

import ast
import re
from pathlib import Path

CONSTANTS_FILE = Path(__file__).parent / "constants.py"

# key -> (python type, section, label, description)
# type is one of: bool, int, float, "int_or_none"
EDITABLE: dict[str, tuple] = {
    # ── Pipeline toggles ──
    "FETCH_COMMENTS": (bool, "Pipeline toggles", "Fetch comments",
                       "Scrape comment text for page/group posts (off = faster, count still kept)"),
    "ANALYZE_IMAGES": (bool, "Pipeline toggles", "KIE image analysis",
                       "Send processed images to KIE for product extraction (off = skip API cost)"),
    "MAX_IMAGES_PER_POST": ("int_or_none", "Pipeline toggles", "Max images per post",
                            "Cap images downloaded/analyzed per post. Empty = no limit (50-image album-walk safety cap still applies)"),
    "IMAGE_WORKERS": (int, "Pipeline toggles", "Image analysis workers",
                      "Concurrent per-post image processing/analysis threads"),
    "IMAGE_DOWNLOAD_WORKERS": (int, "Pipeline toggles", "Image download workers",
                               "Concurrent per-post image download threads"),
    "POSTS_PER_SOURCE_DEFAULT": (int, "Pipeline toggles", "Posts per source",
                                 "Default --posts-per-source limit for page/group fetch"),
    "MIN_IMAGES_FOR_KEEP": (int, "Pipeline toggles", "Min images to keep",
                            "A post needs more than this many images to be kept (brand filter)"),

    # ── Scraper analysis ──
    "SCRAPER_ANALYSIS": (bool, "Scraper analysis", "Scraper analysis",
                         "Look each food image's product up on the brand's own site"),
    "PRICE_ON_IMAGE": (bool, "Scraper analysis", "Price on image",
                       "Stamp the scraped price onto the food image as a red badge"),
    "SCRAPER_WORKERS": (int, "Scraper analysis", "Scraper workers",
                        "Parallel keyword-search threads (one searcher session per thread)"),
    "DEDUPE_PRODUCTS": (bool, "Scraper analysis", "Dedupe products",
                        "Remove duplicate products from image_analysis.json before publishing"),
    "DEDUPE_THRESHOLD": (float, "Scraper analysis", "Dedupe threshold",
                         "Name similarity ratio (0-1) above which two products count as duplicates"),

    # ── AI image generation ──
    "GENERATE_AI_IMAGES": (bool, "AI image generation", "Generate AI images",
                           "AI-regenerate each food image via KIE nano-banana before publishing"),
    "AI_IMAGE_MAX_BYTES": (int, "AI image generation", "AI image size cap (bytes)",
                           "Upload copy and AI result are compressed under this size"),
    "AI_IMAGE_WORKERS": (int, "AI image generation", "AI generation workers",
                         "Parallel AI image-generation threads (overlaps the long poll waits)"),
    "AI_IMAGE_COMPARE": (bool, "AI image generation", "Comparison mode",
                         "Publish a comparison sheet (AI on top, original below, labeled) instead of the clean AI image"),

    # ── Publishing ──
    "SKIP_PRODUCTS_WITHOUT_PRICE": (bool, "Publishing", "Skip products without price",
                                    "Don't upload/publish products whose scrape found no price (off = publish everything)"),

    # ── Image processing ──
    "MAX_PROCESSED_BYTES": (int, "Image processing", "Processed image cap (bytes)",
                            "Size cap for processed (grayscale) images"),
    "MIN_QUALITY": (int, "Image processing", "Min JPEG quality",
                    "Floor for JPEG quality before downscaling starts"),
    "MIN_SCALE": (float, "Image processing", "Min scale factor",
                  "Floor for resolution downscale factor"),

    # ── KIE ──
    "KIE_MAX_REQUESTS_PER_WINDOW": (int, "KIE", "KIE requests per window",
                                    "Stay under KIE's ~20 requests/10s account cap"),
    "KIE_RATE_WINDOW_SECONDS": (int, "KIE", "KIE window (seconds)",
                                "Length of the shared rate-limiter window"),

    # ── Export ──
    "ZIP_MAX_AGE_SECONDS": (int, "Export", "ZIP max age (seconds)",
                            "Export ZIPs older than this are swept on every new build"),
}

_LINE_RE = {key: re.compile(
    rf"^(?P<prefix>\s*{re.escape(key)}\s*=\s*)(?P<value>[^\n#]+?)(?P<comment>\s{{2,}}#.*)?$"
) for key in EDITABLE}

# Only these characters are allowed in a value before it is evaluated —
# covers bools, None, numbers, simple arithmetic (500 * 1024) and strings.
_SAFE_CHARS = re.compile(r"^[\w\s.+\-*/()'\",]*$")


def _parse_value(raw: str, vtype):
    raw = raw.strip()
    if not _SAFE_CHARS.match(raw):
        raise ValueError(f"unsupported value expression: {raw!r}")
    if raw in ("True", "False", "None"):
        return {"True": True, "False": False, "None": None}[raw]
    try:
        value = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        # Simple arithmetic like "500 * 1024" — safe under the _SAFE_CHARS
        # whitelist (digits, operators, parens, whitespace only).
        if re.fullmatch(r"[\d\s.+\-*/()]+", raw):
            value = eval(raw, {"__builtins__": {}}, {})
        else:
            raise
    if vtype == "int_or_none":
        return None if value is None else int(value)
    if vtype == bool:
        return bool(value)
    if vtype == int:
        return int(value)
    if vtype == float:
        return float(value)
    return value


def _format_value(value, vtype) -> str:
    if vtype == "int_or_none" and value in ("", None, "None"):
        return "None"
    if vtype in (bool, "int_or_none") or isinstance(value, bool):
        if isinstance(value, bool):
            return "True" if value else "False"
    if vtype == int or vtype == "int_or_none":
        return str(int(value))
    if vtype == float:
        return str(float(value))
    return repr(str(value))


def read_values() -> dict[str, dict]:
    """Parse constants.py and return the editable constants:
    {key: {value, type, section, label, description}}."""
    lines = CONSTANTS_FILE.read_text(encoding="utf-8").splitlines()
    out: dict[str, dict] = {}
    for key, (vtype, section, label, description) in EDITABLE.items():
        entry = {"key": key,
                 "type": vtype if isinstance(vtype, str) else vtype.__name__,
                 "section": section,
                 "label": label, "description": description, "value": None,
                 "raw": None}
        for line in lines:
            m = _LINE_RE[key].match(line)
            if m:
                entry["raw"] = m.group("value").strip()
                try:
                    entry["value"] = _parse_value(entry["raw"], vtype)
                except Exception:
                    entry["value"] = entry["raw"]  # show raw if unparseable
                break
        out[key] = entry
    return out


def write_values(updates: dict) -> list[str]:
    """Write the given {key: value} updates into constants.py, preserving
    comments and everything outside the editable keys. Returns a list of
    error strings (empty on full success)."""
    errors: list[str] = []
    clean: dict[str, str] = {}
    for key, value in (updates or {}).items():
        if key not in EDITABLE:
            errors.append(f"unknown constant: {key}")
            continue
        vtype = EDITABLE[key][0]
        try:
            clean[key] = _format_value(value, vtype)
        except (TypeError, ValueError) as e:
            errors.append(f"{key}: {e}")
    if errors or not clean:
        return errors

    lines = CONSTANTS_FILE.read_text(encoding="utf-8").splitlines()
    remaining = set(clean)
    for i, line in enumerate(lines):
        for key in list(remaining):
            m = _LINE_RE[key].match(line)
            if m:
                comment = m.group("comment") or ""
                spacing = "" if not comment else "  "
                lines[i] = f"{m.group('prefix')}{clean[key]}{spacing}{comment}"
                remaining.discard(key)
                break
    for key in remaining:  # key not found in the file
        errors.append(f"{key} not found in constants.py")
    if not errors:
        CONSTANTS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return errors
