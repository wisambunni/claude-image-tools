# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = [
#     "mcp>=1.10,<2",
#     "pillow>=11.0",
#     "pillow-heif>=0.18",
#     "numpy>=1.26",
# ]
# ///
"""Image tools MCP server: lets Claude inspect, crop, zoom, resize and convert images.

Every tool that changes pixels can return a preview image, so Claude can *see*
the result instead of guessing. Run with: uv run --script image_tools.py
"""

from __future__ import annotations

import base64
import glob
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
from pydantic import Field
from PIL import ExifTags, Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:  # pragma: no cover - optional
    HEIF_SUPPORTED = False

INSTRUCTIONS = """\
Tools for seeing and editing images. Every tool's `path` accepts a file path or one of these sources:
  "chat" (the latest image the user attached in this conversation; "chat:2" is the one before it),
  "clipboard" (the image on the clipboard, e.g. one the user just pasted), "screenshot" (newest screenshot).
When the user shares an image in chat and asks to edit or inspect it, use path="chat" - don't ask them to save it.
If "chat" is unavailable (Claude Desktop chat), try "clipboard", then find_images to locate the file on disk.
Results from chat/clipboard images are saved to the output folder; copy_to_clipboard puts a result on the clipboard.

Use these tools on your own initiative, not only when asked. You see images downscaled (to 1568-2576px on the
long edge depending on the model), so in large or dense images - blueprints, schematics, charts, maps, scanned
documents, dashboards, long screenshots - small text, axis labels, legends and annotations can be illegible
or subtly misread. Before answering about such details:
  1. find_detail_regions shows where dense detail sits (text blocks, title blocks, legends, labels).
  2. zoom_image on the regions relevant to the question (it crops the full-resolution original),
     or split_tiles to read everything. Read values from the zoomed view, never guess from the overview.
If you're even slightly unsure of a number or word, zoom tighter and confirm before answering.
Coordinates: you don't see images at their original size, so positions you estimate by eye are NOT original
pixels. Use region=N from find_detail_regions, pixel numbers from tool output, or units="fraction".
To crop out "the important parts", decide what matters for the user's purpose from the overview, locate it with
find_detail_regions or grid_overlay (labels are original-image pixels), crop_image each part, and check each preview.
- Originals are never modified; outputs are written next to the source with a suffix. Only use overwrite=true
  when the user asked to replace a file.
"""

mcp = FastMCP("image-tools", instructions=INSTRUCTIONS)

# Claude 4.7+ models see up to 2576px on the long edge (older ones 1568px), but once a conversation holds
# more than 20 images the API rejects any image over 2000px. 2000 is the most detail that's always safe.
PREVIEW_MAX_EDGE = 2000
# Stay well under the ~5MB per-image limit once base64-encoded.
PREVIEW_MAX_BYTES = 3_500_000

FORMAT_ALIASES = {
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "png": "PNG",
    "webp": "WEBP",
    "gif": "GIF",
    "bmp": "BMP",
    "tif": "TIFF",
    "tiff": "TIFF",
    "ico": "ICO",
    "avif": "AVIF",
    "heic": "HEIF",
    "heif": "HEIF",
    "pdf": "PDF",
}
EXTENSIONS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "GIF": ".gif",
    "BMP": ".bmp",
    "TIFF": ".tiff",
    "ICO": ".ico",
    "AVIF": ".avif",
    "HEIF": ".heic",
    "PDF": ".pdf",
}
# Formats that cannot store an alpha channel.
NO_ALPHA = {"JPEG", "BMP", "PDF"}

RESAMPLE = {
    "lanczos": Image.Resampling.LANCZOS,
    "bicubic": Image.Resampling.BICUBIC,
    "bilinear": Image.Resampling.BILINEAR,
    "nearest": Image.Resampling.NEAREST,
}

Content = list[TextContent | ImageContent]

PathArg = Annotated[
    str,
    Field(
        description=(
            'Image to use: an absolute file path, or "chat" (latest image the user attached in this conversation; '
            '"chat:2" = second latest), "clipboard" (image currently on the clipboard), or "screenshot" (newest screenshot).'
        )
    ),
]

# Accept the spellings models tend to reach for, so a guess doesn't cost a failed call.
Units = Literal["px", "pixels", "pixel", "fraction", "fractions", "relative", "normalized"]

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif", ".ico"}


def _cache_dir() -> Path:
    base = os.environ.get("CLAUDE_PLUGIN_DATA")
    if base:
        d = Path(base) / "images"
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Caches" / "claude-image-tools"
    elif sys.platform == "win32":
        d = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "claude-image-tools" / "cache"
    else:
        d = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "claude-image-tools"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _output_dir() -> Path:
    """Where results go when the source isn't a normal file (chat or clipboard images)."""
    d = Path(os.path.expanduser(os.environ.get("IMAGE_TOOLS_OUTPUT_DIR") or "~/Downloads/Claude Images"))
    d.mkdir(parents=True, exist_ok=True)
    return d


CACHE_DIR = _cache_dir()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _resolve(path: str) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve()


# --- image sources: chat attachments, clipboard, screenshots ---------------------------


def _claude_config_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))


def _session_transcript() -> Path | None:
    """Find the current Claude Code session transcript, which holds images the user attached."""
    projects = _claude_config_dir() / "projects"
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if session_id:
        hits = glob.glob(str(projects / "*" / f"{glob.escape(session_id)}.jsonl"))
        if hits:
            return Path(max(hits, key=os.path.getmtime))
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        mangled = re.sub(r"[^A-Za-z0-9]", "-", project_dir)
        candidates = glob.glob(str(projects / mangled / "*.jsonl"))
        if candidates:
            return Path(max(candidates, key=os.path.getmtime))
    return None


def _chat_images() -> list[tuple[str, bytes, str]]:
    """Images the user attached in this session, oldest first, as (media_type, bytes, timestamp)."""
    transcript = _session_transcript()
    if transcript is None:
        return []
    found: list[tuple[str, bytes, str]] = []
    seen: set[str] = set()
    with open(transcript, encoding="utf-8") as fh:
        for line in fh:
            if '"image"' not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = entry.get("message") or {}
            if entry.get("type") != "user" or msg.get("role") != "user" or entry.get("isSidechain"):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            # Only images in the user's own message, not images returned by tools.
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "image":
                    continue
                src = block.get("source") or {}
                if src.get("type") != "base64" or not src.get("data"):
                    continue
                digest = hashlib.sha256(src["data"][:4096].encode() + str(len(src["data"])).encode()).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                found.append((src.get("media_type", "image/png"), base64.b64decode(src["data"]), entry.get("timestamp", "")))
    return found


def _materialize(data: bytes, prefix: str, ext: str) -> Path:
    """Write image bytes to the cache under a content-addressed name, so repeat calls reuse it."""
    digest = hashlib.sha256(data).hexdigest()[:10]
    p = CACHE_DIR / f"{prefix}-{digest}{ext}"
    if not p.exists():
        p.write_bytes(data)
    return p


def _from_chat(index: int, images: list[tuple[str, bytes, str]] | None = None) -> Path:
    if images is None:
        images = _chat_images()
    if not images:
        if not os.environ.get("CLAUDE_CODE_SESSION_ID") and not os.environ.get("CLAUDE_PROJECT_DIR"):
            raise LookupError(
                'Chat attachments are only readable when running in Claude Code (including the desktop app\'s Code tab). '
                'Here, try path="clipboard" (works right after the user pastes or copies an image), '
                "or call find_images to locate the file on disk."
            )
        raise LookupError("No images attached in this conversation yet. Ask the user to attach one, or use a file path.")
    if index < 1 or index > len(images):
        raise LookupError(f"Only {len(images)} image(s) attached in this conversation; chat:{index} doesn't exist.")
    media_type, data, _ = images[-index]
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(media_type, ".png")
    copy = _materialize(data, f"chat-image-{len(images) - index + 1}", ext)
    return _find_original(copy) or copy


# Claude Code shrinks attached images (long edge <= 2000px) before storing them, which destroys
# fine detail. When the user attached a file from disk, find that full-resolution original.
CHAT_COPY_MAX_EDGE = 2000
_ORIGINALS: dict[Path, Path | None] = {}


def _find_original(copy: Path) -> Path | None:
    if copy in _ORIGINALS:
        return _ORIGINALS[copy]
    _ORIGINALS[copy] = None
    with Image.open(copy) as im:
        cw, ch = im.size
        if max(cw, ch) < CHAT_COPY_MAX_EDGE * 0.95:
            return None  # small enough that it was probably sent at full size
        small = (max(8, cw // 16), max(8, ch // 16))
        ref = im.convert("L").resize(small, Image.Resampling.BOX)

    candidates: list[Path] = []
    for root in filter(None, (os.environ.get("CLAUDE_PROJECT_DIR"), os.getcwd())):
        for depth_glob in ("*", "*/*", "*/*/*"):
            candidates += [Path(f) for f in glob.glob(os.path.join(glob.escape(root), depth_glob)) if Path(f).suffix.lower() in IMAGE_EXTS]
    candidates += _recent_files(days=30, limit=300)

    seen: set[Path] = set()
    for cand in candidates[:2000]:
        cand = cand.resolve()
        if cand in seen or _is_virtual(cand):
            continue
        seen.add(cand)
        try:
            with Image.open(cand) as im:
                w, h = ImageOps.exif_transpose(im).size if im.format in ("JPEG", "MPO", "TIFF") else im.size
                if max(w, h) <= max(cw, ch) or abs(w / h - cw / ch) > 0.01:
                    continue
                im = ImageOps.exif_transpose(im)
                im.draft("L", (small[0] * 2, small[1] * 2))  # fast JPEG decode at reduced size
                probe = im.convert("L").resize(small, Image.Resampling.BOX)
        except Exception:
            continue
        diff = sum(abs(a - b) for a, b in zip(ref.tobytes(), probe.tobytes())) / (small[0] * small[1])
        if diff < 6:
            _ORIGINALS[copy] = cand
            return cand
    return None


def _from_clipboard() -> Path:
    from PIL import ImageGrab

    try:
        grabbed = ImageGrab.grabclipboard()
    except Exception as exc:  # e.g. no xclip/wl-paste on Linux
        raise LookupError(f"Couldn't read the clipboard: {exc}") from exc
    if isinstance(grabbed, list):  # files copied in Finder / Explorer
        files = [f for f in grabbed if Path(f).suffix.lower() in IMAGE_EXTS]
        if files:
            return Path(files[0])
        grabbed = None
    if grabbed is None:
        raise LookupError("The clipboard doesn't contain an image. Copy the image (or the image file) first.")
    buf = io.BytesIO()
    grabbed.save(buf, format="PNG")
    return _materialize(buf.getvalue(), "clipboard", ".png")


def _screenshot_dirs() -> list[Path]:
    dirs: list[Path] = []
    if sys.platform == "darwin":
        try:
            loc = subprocess.run(
                ["defaults", "read", "com.apple.screencapture", "location"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
            if loc:
                dirs.append(Path(os.path.expanduser(loc)))
        except Exception:
            pass
        dirs.append(Path.home() / "Desktop")
    elif sys.platform == "win32":
        dirs.append(Path.home() / "Pictures" / "Screenshots")
        dirs.append(Path.home() / "OneDrive" / "Pictures" / "Screenshots")
    else:
        dirs.append(Path.home() / "Pictures" / "Screenshots")
        dirs.append(Path.home() / "Pictures")
    return [d for d in dirs if d.is_dir()]


def _from_screenshot() -> Path:
    pattern = re.compile(r"^(screenshot|screen shot|cleanshot|capture)", re.IGNORECASE)
    shots = [
        f for d in _screenshot_dirs() for f in d.iterdir()
        if f.is_file() and f.suffix.lower() in IMAGE_EXTS and pattern.match(f.name)
    ]
    if not shots:
        raise LookupError("No screenshots found in the usual screenshot folders.")
    return max(shots, key=lambda f: f.stat().st_mtime)


def _resolve_input(path: str) -> Path:
    """Resolve a `path` argument, which may be a file or a source like "chat", "chat:2", "clipboard"."""
    key = path.strip().lower()
    m = re.fullmatch(r"(?:chat|attachment|attached)(?::(\d+))?", key)
    if m:
        return _from_chat(int(m.group(1) or 1))
    if key in ("clipboard", "paste", "pasted"):
        return _from_clipboard()
    if key in ("screenshot", "latest-screenshot", "latest screenshot"):
        return _from_screenshot()
    p = _resolve(path)
    if not p.exists():
        raise FileNotFoundError(
            f"No such file: {p}. If the user attached the image in chat, use path=\"chat\" instead."
        )
    return p


def _is_virtual(p: Path) -> bool:
    try:
        p.relative_to(CACHE_DIR)
        return True
    except ValueError:
        return False


def _open(path: str) -> tuple[Image.Image, Path, str]:
    """Open an image with EXIF orientation applied, so coordinates match what is seen."""
    p = _resolve_input(path)
    img = Image.open(p)
    fmt = img.format or "UNKNOWN"
    img.load()
    img = ImageOps.exif_transpose(img)
    return img, p, fmt


def _normalize_format(fmt: str) -> str:
    key = fmt.lower().lstrip(".")
    if key not in FORMAT_ALIASES:
        raise ValueError(f"Unsupported format '{fmt}'. Choose one of: {', '.join(sorted(FORMAT_ALIASES))}")
    pil = FORMAT_ALIASES[key]
    if pil == "HEIF" and not HEIF_SUPPORTED:
        raise ValueError("HEIC/HEIF support is unavailable (pillow-heif failed to load).")
    return pil


def _format_from_path(p: Path, fallback: str) -> str:
    ext = p.suffix.lower().lstrip(".")
    return FORMAT_ALIASES.get(ext, fallback)


def _default_output(src: Path, suffix: str, fmt: str) -> Path:
    """Next to the source for real files; in the output folder for chat/clipboard images."""
    ext = EXTENSIONS.get(fmt, src.suffix or ".png")
    folder = _output_dir() if _is_virtual(src) else src.parent
    stem = re.sub(r"-[0-9a-f]{10}$", "", src.stem)
    name = f"{stem}_{suffix}" if suffix else stem
    candidate = folder / f"{name}{ext}"
    n = 2
    while candidate.exists():
        candidate = folder / f"{name}_{n}{ext}"
        n += 1
    return candidate


def _prepare_for_format(img: Image.Image, fmt: str, background: str = "white") -> Image.Image:
    if fmt in NO_ALPHA:
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, background)
            bg.paste(rgba, mask=rgba.getchannel("A"))
            return bg
        allowed = {"RGB", "L", "CMYK"} if fmt == "JPEG" else {"RGB", "L"}
        if img.mode not in allowed:
            return img.convert("RGB")
    if fmt == "GIF" and img.mode not in ("P", "L"):
        return img.convert("P", palette=Image.Palette.ADAPTIVE)
    if fmt in ("WEBP", "AVIF", "HEIF", "PNG") and img.mode not in ("RGB", "RGBA", "L", "LA", "P", "I;16", "I"):
        return img.convert("RGBA" if "A" in img.getbands() else "RGB")
    return img


def _save(
    img: Image.Image,
    out: Path,
    fmt: str,
    quality: int | None = None,
    overwrite: bool = False,
    background: str = "white",
    source_info: dict | None = None,
) -> Path:
    if out.exists() and not overwrite:
        raise FileExistsError(f"{out} already exists. Pass overwrite=true or choose another output_path.")
    out.parent.mkdir(parents=True, exist_ok=True)
    img = _prepare_for_format(img, fmt, background)
    kwargs: dict = {}
    if quality is not None and fmt in ("JPEG", "WEBP", "AVIF", "HEIF"):
        kwargs["quality"] = max(1, min(100, quality))
    if fmt == "JPEG":
        kwargs.setdefault("quality", 90)
        kwargs["optimize"] = True
    if fmt == "PNG":
        kwargs["optimize"] = True
    if fmt == "TIFF":
        kwargs["compression"] = "tiff_lzw"
    if fmt == "ICO":
        sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256) if s <= max(img.size)]
        kwargs["sizes"] = sizes or [img.size]
    if source_info and "icc_profile" in source_info and fmt in ("JPEG", "PNG", "WEBP", "TIFF", "AVIF", "HEIF"):
        kwargs["icc_profile"] = source_info["icc_profile"]
    img.save(out, format=fmt, **kwargs)
    return out


def _preview(img: Image.Image, max_edge: int = PREVIEW_MAX_EDGE) -> ImageContent:
    """Encode an image for Claude to look at, downscaled to a size it can actually use."""
    view = img.copy()
    if max(view.size) > max_edge:
        view.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    has_alpha = view.mode in ("RGBA", "LA") or (view.mode == "P" and "transparency" in view.info)
    if has_alpha:
        view = view.convert("RGBA")
        buf = io.BytesIO()
        view.save(buf, format="PNG", optimize=True)
        mime = "image/png"
    else:
        view = view.convert("RGB")
        buf = io.BytesIO()
        view.save(buf, format="PNG", optimize=True)
        mime = "image/png"
        if buf.tell() > PREVIEW_MAX_BYTES:
            buf = io.BytesIO()
            view.save(buf, format="JPEG", quality=88)
            mime = "image/jpeg"
    quality = 80
    while buf.tell() > PREVIEW_MAX_BYTES and quality >= 40:
        buf = io.BytesIO()
        view.convert("RGB").save(buf, format="JPEG", quality=quality)
        mime = "image/jpeg"
        quality -= 10
    return ImageContent(type="image", data=base64.b64encode(buf.getvalue()).decode(), mimeType=mime)


def _text(s: str) -> TextContent:
    return TextContent(type="text", text=s)


# Boxes from the last find_detail_regions call per image, so zoom/crop can take `region=N`.
_LAST_REGIONS: dict[Path, list[tuple[int, int, int, int]]] = {}


def _resolve_box(
    img: Image.Image,
    p: Path,
    region: int | None,
    left: float | None,
    top: float | None,
    right: float | None,
    bottom: float | None,
    units: Units,
) -> tuple[int, int, int, int]:
    if region is not None:
        boxes = _LAST_REGIONS.get(p)
        if not boxes:
            raise ValueError("No detail regions for this image yet. Call find_detail_regions first, or pass a box.")
        if not 1 <= region <= len(boxes):
            raise ValueError(f"region must be between 1 and {len(boxes)}.")
        return boxes[region - 1]
    if None in (left, top, right, bottom):
        raise ValueError("Pass region=N from find_detail_regions, or all of left, top, right, bottom.")
    return _box_to_pixels(img, left, top, right, bottom, units)


def _box_to_pixels(
    img: Image.Image,
    left: float,
    top: float,
    right: float,
    bottom: float,
    units: Units,
) -> tuple[int, int, int, int]:
    w, h = img.size
    if units in ("fraction", "fractions", "relative", "normalized"):
        left, right = left * w, right * w
        top, bottom = top * h, bottom * h
    box = (
        max(0, int(round(left))),
        max(0, int(round(top))),
        min(w, int(round(right))),
        min(h, int(round(bottom))),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError(f"Empty crop box {box} for image of size {w}x{h}. right must exceed left and bottom must exceed top.")
    return box


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---------------------------------------------------------------------------
# tools: inspection
# ---------------------------------------------------------------------------


@mcp.tool()
def image_info(path: PathArg) -> str:
    """Report an image's dimensions, format, color mode, file size, frame count and key EXIF data.

    Dimensions are reported after applying EXIF orientation, i.e. the coordinate
    space every other tool in this server uses.
    """
    p = _resolve_input(path)
    with Image.open(p) as raw:
        fmt = raw.format
        raw_size = raw.size
        frames = getattr(raw, "n_frames", 1)
        exif = raw.getexif()
        info_keys = sorted(k for k in raw.info if k not in ("exif", "icc_profile", "xmp"))
        has_icc = "icc_profile" in raw.info
        oriented = ImageOps.exif_transpose(raw)
        w, h = oriented.size
        mode = raw.mode

    lines = [
        f"path: {p}" + (f" (resolved from {path!r})" if not _resolve(path).exists() else ""),
        f"format: {fmt}",
        f"size: {w}x{h} px (width x height)",
        f"aspect ratio: {w / h:.4f}",
        f"megapixels: {w * h / 1e6:.2f}",
        f"mode: {mode}",
        f"file size: {_human_bytes(p.stat().st_size)}",
        f"frames: {frames}",
        f"embedded ICC profile: {'yes' if has_icc else 'no'}",
    ]
    if raw_size != (w, h):
        lines.append(f"stored size before EXIF rotation: {raw_size[0]}x{raw_size[1]}")
    if exif:
        wanted = {"Make", "Model", "DateTime", "DateTimeOriginal", "Orientation", "Software", "LensModel", "ExposureTime", "FNumber", "ISOSpeedRatings", "FocalLength"}
        merged = dict(exif)
        try:
            merged.update(exif.get_ifd(ExifTags.IFD.Exif))
        except Exception:
            pass
        picked = []
        for tag_id, value in merged.items():
            name = ExifTags.TAGS.get(tag_id, str(tag_id))
            if name in wanted:
                picked.append(f"  {name}: {value}")
        if picked:
            lines.append("exif:")
            lines.extend(sorted(picked))
        try:
            if exif.get_ifd(ExifTags.IFD.GPSInfo):
                lines.append("  GPS: present (location embedded)")
        except Exception:
            pass
    if info_keys:
        lines.append(f"other metadata keys: {', '.join(info_keys)}")
    if _is_virtual(p) and max(w, h) >= CHAT_COPY_MAX_EDGE * 0.95:
        lines.append(
            "note: this is the reduced copy stored by the chat app (fine detail is lost). "
            "If the user has the original file, ask for its path or have them copy it and use path='clipboard'."
        )
    elif not _is_virtual(p) and path.strip().lower().startswith(("chat", "attach")):
        lines.append("note: using the full-resolution original of the chat attachment found on disk.")
    if max(w, h) > PREVIEW_MAX_EDGE:
        lines.append(
            f"note: long edge exceeds {PREVIEW_MAX_EDGE}px, so a full view is downscaled; "
            "use zoom_image to inspect fine detail."
        )
    return "\n".join(lines)


@mcp.tool()
def view_image(path: PathArg, max_edge: int = PREVIEW_MAX_EDGE) -> Content:
    """Return the image so you can look at it, downscaled so its long edge is at most max_edge px.

    Works with formats the built-in Read tool may not handle (HEIC, TIFF, BMP, AVIF, ICO, multi-frame GIF).
    """
    img, p, fmt = _open(path)
    max_edge = max(64, min(max_edge, PREVIEW_MAX_EDGE))
    scale = min(1.0, max_edge / max(img.size))
    note = f"{p.name}: {img.size[0]}x{img.size[1]} {fmt}"
    if scale < 1:
        note += f" (shown at {scale:.0%}; multiply preview coordinates by {1 / scale:.3f} for original pixels)"
    return [_text(note), _preview(img, max_edge)]


@mcp.tool()
def grid_overlay(
    path: PathArg,
    rows: int = 8,
    cols: int = 8,
    color: str = "#ff0044",
) -> Content:
    """Show the image with a labeled pixel-coordinate grid drawn over it.

    Use this before crop_image / zoom_image to read off accurate pixel coordinates.
    Labels are in ORIGINAL image pixels even when the preview is downscaled.
    Nothing is written to disk.
    """
    img, p, _ = _open(path)
    rows, cols = max(1, min(rows, 40)), max(1, min(cols, 40))
    w, h = img.size
    view = img.convert("RGB")
    scale = min(1.0, PREVIEW_MAX_EDGE / max(w, h))
    if scale < 1:
        view = view.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)
    vw, vh = view.size
    draw = ImageDraw.Draw(view)
    font = _font(max(11, min(vw, vh) // 60))
    line_w = max(1, min(vw, vh) // 500)

    def label(xy: tuple[float, float], text: str) -> None:
        bbox = draw.textbbox(xy, text, font=font)
        draw.rectangle((bbox[0] - 2, bbox[1] - 1, bbox[2] + 2, bbox[3] + 1), fill="black")
        draw.text(xy, text, fill="white", font=font)

    for i in range(1, cols):
        x = vw * i / cols
        draw.line([(x, 0), (x, vh)], fill=color, width=line_w)
        label((x + 3, 3), str(round(w * i / cols)))
    for j in range(1, rows):
        y = vh * j / rows
        draw.line([(0, y), (vw, y)], fill=color, width=line_w)
        label((3, y + 3), str(round(h * j / rows)))
    size_label = f"{w}x{h}"
    lb = draw.textbbox((0, 0), size_label, font=font)
    label((vw - (lb[2] - lb[0]) - 6, vh - (lb[3] - lb[1]) - 8), size_label)

    return [
        _text(
            f"{p.name}: {w}x{h}px with a {cols}x{rows} grid. Vertical lines labeled with x, horizontal lines with y, "
            "in original-image pixels."
        ),
        _preview(view),
    ]


@mcp.tool()
def zoom_image(
    path: PathArg,
    left: float | None = None,
    top: float | None = None,
    right: float | None = None,
    bottom: float | None = None,
    units: Units = "px",
    region: int | None = None,
    enhance: bool = False,
) -> Content:
    """Look closely at one region of an image without saving anything.

    The region is cropped from the full-resolution original and enlarged (up to 8x), which
    reveals detail lost when the whole image is downscaled: small text, dimensions, axis
    labels, legends, UI elements, distant objects. Use it unprompted whenever an answer
    depends on detail you can't read with certainty. Use units="fraction" for 0-1 coordinates.
    Set enhance=true to boost contrast and sharpness, which helps with faint text.
    To read exact values (numbers, codes, small print), zoom tightly on just those lines: a smaller
    region means more magnification. If text in the result is still small, zoom tighter.
    Give the area as region=N (a box number from find_detail_regions - the most reliable option),
    or as left/top/right/bottom. Pixel values must be ORIGINAL-image pixels, e.g. numbers read from
    find_detail_regions, grid_overlay or image_info. Positions you estimate by eye from your own view
    are in a downscaled frame, so pass those as units="fraction" (0-1) instead.
    """
    img, p, _ = _open(path)
    box = _resolve_box(img, p, region, left, top, right, bottom, units)
    area = img.crop(box)
    rw, rh = area.size
    factor = min(PREVIEW_MAX_EDGE / max(rw, rh), 8.0)
    if factor > 1:
        method = Image.Resampling.LANCZOS if factor < 4 else Image.Resampling.BICUBIC
        area = area.resize((round(rw * factor), round(rh * factor)), method)
    if enhance:
        area = _enhance(area.convert("RGB"), autocontrast=True, sharpness=1.8)
    return [
        _text(f"{p.name} box {box} ({rw}x{rh}px) shown at {factor:.2f}x."),
        _preview(area),
    ]


def _detail_regions(img: Image.Image, max_regions: int, sensitivity: float) -> list[tuple[tuple[int, int, int, int], float]]:
    """Find boxes of fine detail (text, labels, dimensions, small symbols).

    Text and small symbols flip between ink and background many times both across and down,
    while lines, borders and walls flip in only one direction. Scoring each grid cell by the
    smaller of its horizontal and vertical transition counts keeps the first and drops the second.
    """
    import numpy as np

    w, h = img.size
    scale = min(1.0, 2400 / max(w, h))
    work = img.convert("L")
    if scale < 1:
        work = work.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)
    gray = np.asarray(work, dtype=np.int16)
    local_bg = np.asarray(work.filter(ImageFilter.BoxBlur(6)), dtype=np.int16)
    ink = (np.abs(gray - local_bg) > 40).astype(np.int8)
    across = np.abs(np.diff(ink, axis=1))[:-1, :]
    down = np.abs(np.diff(ink, axis=0))[:, :-1]

    cell = max(8, round(max(work.size) / 110))
    gh, gw = across.shape[0] // cell, across.shape[1] // cell
    if gh == 0 or gw == 0:
        return []

    def per_cell(a: "np.ndarray") -> "np.ndarray":
        return a[: gh * cell, : gw * cell].reshape(gh, cell, gw, cell).sum(axis=(1, 3))

    score = np.minimum(per_cell(across), per_cell(down)).astype(float)
    occupied = score[score > 0]
    if occupied.size == 0:
        return []
    # Lines and corners score below ~1 transition per cell row; text scores well above it.
    thresh = max(cell * 1.2, np.percentile(occupied, 90) * 0.35) / sensitivity
    hot = score >= thresh

    # Merge words and lines of text into blocks: dilate by one cell, then flood-fill components.
    grown = hot.copy()
    grown[1:, :] |= hot[:-1, :]
    grown[:-1, :] |= hot[1:, :]
    grown[:, 1:] |= hot[:, :-1]
    grown[:, :-1] |= hot[:, 1:]
    seen = np.zeros_like(grown)
    regions = []
    f = cell / scale
    for y0, x0 in zip(*np.nonzero(grown)):
        if seen[y0, x0]:
            continue
        stack, ys, xs, total = [(y0, x0)], [], [], 0.0
        seen[y0, x0] = True
        while stack:
            y, x = stack.pop()
            ys.append(y)
            xs.append(x)
            if hot[y, x]:
                total += score[y, x]
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < gh and 0 <= nx < gw and grown[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        if total == 0 or sum(hot[y, x] for y, x in zip(ys, xs)) < 2:
            continue  # single specks are noise
        pad = f * 1.0
        box = (
            max(0, int(min(xs) * f - pad)),
            max(0, int(min(ys) * f - pad)),
            min(w, int((max(xs) + 1) * f + pad)),
            min(h, int((max(ys) + 1) * f + pad)),
        )
        regions.append((box, total))
    # Skip regions covering most of the image; they carry no location information.
    regions = [r for r in regions if (r[0][2] - r[0][0]) * (r[0][3] - r[0][1]) < 0.6 * w * h]
    regions.sort(key=lambda r: r[1], reverse=True)
    return regions[:max_regions]


@mcp.tool()
def find_detail_regions(
    path: PathArg,
    max_regions: int = 12,
    sensitivity: float = 1.0,
) -> Content:
    """Locate the areas of an image packed with fine detail - text blocks, labels, dimensions, legends,
    title blocks, small symbols - and show them as numbered boxes with their pixel coordinates.

    Use this first on blueprints, schematics, charts, maps, scanned documents and dense screenshots,
    then zoom_image (to read) or crop_image (to extract) the regions that matter. Detection is visual,
    not semantic: you decide which regions are important. Raise sensitivity (e.g. 1.5) to find
    fainter or smaller details, lower it (0.6) to get only the densest areas.
    """
    img, p, _ = _open(path)
    w, h = img.size
    regions = _detail_regions(img, max(1, min(max_regions, 30)), max(0.2, min(sensitivity, 3.0)))
    _LAST_REGIONS[p] = [box for box, _ in regions]
    if not regions:
        return [_text(f"{p.name}: no dense detail regions found; the image may be mostly flat color or photographic.")]

    view = img.convert("RGB")
    scale = min(1.0, PREVIEW_MAX_EDGE / max(w, h))
    if scale < 1:
        view = view.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(view)
    font = _font(max(14, min(view.size) // 45))
    lw = max(2, min(view.size) // 400)
    lines = [f"{p.name}: {w}x{h}px. Detail regions, densest first (left, top, right, bottom in original pixels):"]
    for n, (box, _) in enumerate(regions, 1):
        vb = [round(v * scale) for v in box]
        draw.rectangle(vb, outline="#ff2d55", width=lw)
        tb = draw.textbbox((vb[0], vb[1]), str(n), font=font)
        draw.rectangle((tb[0] - 3, tb[1] - 2, tb[2] + 3, tb[3] + 2), fill="#ff2d55")
        draw.text((vb[0], vb[1]), str(n), fill="white", font=font)
        lines.append(f"  {n}: {box}  ({box[2] - box[0]}x{box[3] - box[1]}px)")
    lines.append("Next: zoom_image(region=N) to read a box, or crop_image(region=N) to save it.")
    return [_text("\n".join(lines)), _preview(view)]


# ---------------------------------------------------------------------------
# tools: transformations (write files)
# ---------------------------------------------------------------------------


@mcp.tool()
def crop_image(
    path: PathArg,
    left: float | None = None,
    top: float | None = None,
    right: float | None = None,
    bottom: float | None = None,
    units: Units = "px",
    region: int | None = None,
    output_path: str | None = None,
    overwrite: bool = False,
    show_result: bool = True,
) -> Content:
    """Crop an image to a box and save it.

    Give the area as region=N (a box number from find_detail_regions - the most reliable option),
    or as left/top/right/bottom. Pixel values must be ORIGINAL-image pixels, e.g. numbers read from
    find_detail_regions, grid_overlay or image_info. Positions you estimate by eye from your own view
    are in a downscaled frame, so pass those as units="fraction" (0-1) instead.
    Pixel boxes are right/bottom exclusive.
    The box is clamped to the image bounds. Output defaults to <name>_crop.<ext> next to the original;
    the format follows output_path's extension. The original is never modified unless output_path
    points at it and overwrite=true.
    """
    img, p, fmt = _open(path)
    box = _resolve_box(img, p, region, left, top, right, bottom, units)
    cropped = img.crop(box)
    out_fmt = fmt if fmt in EXTENSIONS else "PNG"
    out = _resolve(output_path) if output_path else _default_output(p, "crop", out_fmt)
    out_fmt = _format_from_path(out, out_fmt)
    _save(cropped, out, out_fmt, overwrite=overwrite, source_info=img.info)
    result: Content = [_text(f"Cropped {box} -> {cropped.size[0]}x{cropped.size[1]}px, saved to {out}")]
    if show_result:
        result.append(_preview(cropped))
    return result


@mcp.tool()
def resize_image(
    path: PathArg,
    width: int | None = None,
    height: int | None = None,
    scale: float | None = None,
    mode: Literal["fit", "fill", "stretch", "pad"] = "fit",
    resample: Literal["lanczos", "bicubic", "bilinear", "nearest"] = "lanczos",
    allow_upscale: bool = True,
    pad_color: str = "white",
    output_path: str | None = None,
    overwrite: bool = False,
    show_result: bool = False,
) -> Content:
    """Resize an image and save it.

    Give either scale (e.g. 0.5), or width and/or height. With only one of width/height
    the aspect ratio is preserved. With both, mode decides:
      - fit: shrink/grow to fit inside the box, keeping aspect ratio (result may be smaller on one side)
      - fill: cover the box, keeping aspect ratio, then center-crop to exactly width x height
      - pad: fit inside the box, then pad to exactly width x height with pad_color
      - stretch: force exactly width x height, distorting if needed
    Use resample="nearest" for pixel art. Output defaults to <name>_<w>x<h>.<ext>.
    """
    img, p, fmt = _open(path)
    w0, h0 = img.size
    rs = RESAMPLE[resample]

    if scale is not None:
        if scale <= 0:
            raise ValueError("scale must be positive")
        target = (max(1, round(w0 * scale)), max(1, round(h0 * scale)))
        out_img = img.resize(target, rs)
    elif width and height:
        if mode == "stretch":
            out_img = img.resize((width, height), rs)
        elif mode == "fill":
            out_img = ImageOps.fit(img, (width, height), method=rs)
        elif mode == "pad":
            fill = pad_color
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            out_img = ImageOps.pad(img, (width, height), method=rs, color=fill)
        else:
            r = min(width / w0, height / h0)
            out_img = img.resize((max(1, round(w0 * r)), max(1, round(h0 * r))), rs)
    elif width:
        out_img = img.resize((width, max(1, round(h0 * width / w0))), rs)
    elif height:
        out_img = img.resize((max(1, round(w0 * height / h0)), height), rs)
    else:
        raise ValueError("Provide scale, width, height, or width and height.")

    if not allow_upscale and (out_img.size[0] > w0 or out_img.size[1] > h0) and mode != "pad":
        return [_text(f"Skipped: target {out_img.size[0]}x{out_img.size[1]} is larger than the original {w0}x{h0} and allow_upscale=false.")]

    out_fmt = fmt if fmt in EXTENSIONS else "PNG"
    nw, nh = out_img.size
    out = _resolve(output_path) if output_path else _default_output(p, f"{nw}x{nh}", out_fmt)
    out_fmt = _format_from_path(out, out_fmt)
    _save(out_img, out, out_fmt, overwrite=overwrite, background=pad_color, source_info=img.info)
    result: Content = [_text(f"Resized {w0}x{h0} -> {nw}x{nh}px, saved to {out} ({_human_bytes(out.stat().st_size)})")]
    if show_result:
        result.append(_preview(out_img))
    return result


@mcp.tool()
def convert_image(
    path: PathArg,
    format: str,
    quality: int | None = None,
    background: str = "white",
    output_path: str | None = None,
    overwrite: bool = False,
    all_frames: bool = False,
) -> str:
    """Convert an image to another format and save it.

    Supported targets: png, jpg/jpeg, webp, gif, bmp, tiff, ico, avif, heic, pdf.
    Readable inputs additionally include most formats Pillow understands (e.g. HEIC from iPhones, PSD flattened).
    quality (1-100) applies to jpg, webp, avif and heic. When converting a transparent image to a format
    without alpha (jpg, bmp, pdf), transparency is flattened onto `background`.
    all_frames=true keeps every frame of an animated GIF/WebP when the target supports it.
    Output defaults to the same name with the new extension.
    """
    target = _normalize_format(format)
    p = _resolve_input(path)
    if output_path:
        out = _resolve(output_path)
    elif _is_virtual(p):
        out = _default_output(p, "", target)
    else:
        out = p.with_suffix(EXTENSIONS[target])
    if out == p and not overwrite:
        raise FileExistsError("Output would overwrite the input. Pass a different output_path or overwrite=true.")
    if out.exists() and not overwrite:
        out = _default_output(p, "converted", target)

    before = p.stat().st_size
    with Image.open(p) as src:
        n_frames = getattr(src, "n_frames", 1)
        if all_frames and n_frames > 1 and target in ("GIF", "WEBP", "PNG", "TIFF", "PDF"):
            frames = []
            durations = []
            for i in range(n_frames):
                src.seek(i)
                frames.append(_prepare_for_format(src.convert("RGBA"), target, background))
                durations.append(src.info.get("duration", 100))
            kwargs: dict = {"save_all": True, "append_images": frames[1:]}
            if target in ("GIF", "WEBP", "PNG"):
                kwargs.update(duration=durations, loop=src.info.get("loop", 0))
            if quality is not None and target == "WEBP":
                kwargs["quality"] = quality
            out.parent.mkdir(parents=True, exist_ok=True)
            frames[0].save(out, format=target, **kwargs)
            note = f" ({n_frames} frames)"
        else:
            src.load()
            img = ImageOps.exif_transpose(src)
            _save(img, out, target, quality=quality, overwrite=True, background=background, source_info=src.info)
            note = " (first frame only)" if n_frames > 1 else ""

    after = out.stat().st_size
    return f"Converted {p.name} -> {out}{note}. Size {_human_bytes(before)} -> {_human_bytes(after)}."


@mcp.tool()
def rotate_flip_image(
    path: PathArg,
    rotate: float = 0,
    flip: Literal["none", "horizontal", "vertical"] = "none",
    expand: bool = True,
    fill_color: str = "white",
    output_path: str | None = None,
    overwrite: bool = False,
    show_result: bool = True,
) -> Content:
    """Rotate (degrees, counter-clockwise; use negative for clockwise) and/or mirror an image, then save it.

    Multiples of 90 are lossless. expand=true grows the canvas so nothing is cut off on arbitrary angles,
    filling new corners with fill_color (or transparency for images with alpha).
    """
    img, p, fmt = _open(path)
    out_img = img
    if rotate % 360:
        if rotate % 90 == 0:
            out_img = out_img.rotate(rotate, expand=True)
        else:
            fill = None if "A" in out_img.getbands() else fill_color
            out_img = out_img.rotate(rotate, resample=Image.Resampling.BICUBIC, expand=expand, fillcolor=fill)
    if flip == "horizontal":
        out_img = ImageOps.mirror(out_img)
    elif flip == "vertical":
        out_img = ImageOps.flip(out_img)
    out_fmt = fmt if fmt in EXTENSIONS else "PNG"
    out = _resolve(output_path) if output_path else _default_output(p, "rotated", out_fmt)
    out_fmt = _format_from_path(out, out_fmt)
    _save(out_img, out, out_fmt, overwrite=overwrite, source_info=img.info)
    result: Content = [_text(f"Saved {out_img.size[0]}x{out_img.size[1]}px to {out}")]
    if show_result:
        result.append(_preview(out_img))
    return result


def _enhance(
    img: Image.Image,
    brightness: float = 1.0,
    contrast: float = 1.0,
    sharpness: float = 1.0,
    saturation: float = 1.0,
    autocontrast: bool = False,
    grayscale: bool = False,
) -> Image.Image:
    out = img
    if grayscale:
        out = ImageOps.grayscale(out)
    if autocontrast:
        base = out.convert("RGB") if out.mode not in ("RGB", "L") else out
        out = ImageOps.autocontrast(base, cutoff=1)
    for enhancer, factor in (
        (ImageEnhance.Brightness, brightness),
        (ImageEnhance.Contrast, contrast),
        (ImageEnhance.Color, saturation),
        (ImageEnhance.Sharpness, sharpness),
    ):
        if factor != 1.0:
            if out.mode not in ("RGB", "RGBA", "L"):
                out = out.convert("RGB")
            out = enhancer(out).enhance(factor)
    return out


@mcp.tool()
def adjust_image(
    path: PathArg,
    brightness: float = 1.0,
    contrast: float = 1.0,
    sharpness: float = 1.0,
    saturation: float = 1.0,
    autocontrast: bool = False,
    grayscale: bool = False,
    save: bool = False,
    output_path: str | None = None,
    overwrite: bool = False,
) -> Content:
    """Adjust brightness/contrast/sharpness/saturation (1.0 = unchanged), autocontrast or grayscale.

    By default this only returns a preview (nothing written), which is useful for reading faded
    documents, dark photos or low-contrast screenshots. Set save=true to write the result.
    """
    img, p, fmt = _open(path)
    out_img = _enhance(img, brightness, contrast, sharpness, saturation, autocontrast, grayscale)
    msg = f"Adjusted {p.name}"
    if save or output_path:
        out_fmt = fmt if fmt in EXTENSIONS else "PNG"
        out = _resolve(output_path) if output_path else _default_output(p, "adjusted", out_fmt)
        out_fmt = _format_from_path(out, out_fmt)
        _save(out_img, out, out_fmt, overwrite=overwrite, source_info=img.info)
        msg += f", saved to {out}"
    else:
        msg += " (preview only, not saved)"
    return [_text(msg), _preview(out_img)]


@mcp.tool()
def split_tiles(
    path: PathArg,
    rows: int = 2,
    cols: int = 2,
    overlap: float = 0.05,
    save_dir: str | None = None,
) -> Content:
    """Split an image into a rows x cols grid of overlapping tiles and return each tile at full detail.

    Best for reading large screenshots, documents, maps or dense diagrams that become illegible
    when downscaled as a whole. overlap (0-0.5) is the fraction each tile extends into its neighbors
    so nothing is lost on a seam. Pass save_dir to also write the tiles to disk. Max 16 tiles.
    """
    img, p, fmt = _open(path)
    rows, cols = max(1, rows), max(1, cols)
    if rows * cols > 16:
        raise ValueError("At most 16 tiles per call; use fewer rows/cols or zoom_image on a sub-region.")
    overlap = max(0.0, min(overlap, 0.5))
    w, h = img.size
    tw, th = w / cols, h / rows
    ox, oy = tw * overlap, th * overlap
    out_dir = _resolve(save_dir) if save_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    out_fmt = fmt if fmt in EXTENSIONS else "PNG"

    result: Content = [_text(f"{p.name}: {w}x{h}px split into {rows}x{cols} tiles (row-major, top-left first).")]
    for r in range(rows):
        for c in range(cols):
            box = (
                max(0, round(c * tw - ox)),
                max(0, round(r * th - oy)),
                min(w, round((c + 1) * tw + ox)),
                min(h, round((r + 1) * th + oy)),
            )
            tile = img.crop(box)
            label = f"tile r{r}c{c} box={box}"
            if out_dir:
                tp = out_dir / f"{p.stem}_r{r}c{c}{EXTENSIONS[out_fmt]}"
                _save(tile, tp, out_fmt, overwrite=True, source_info=img.info)
                label += f" saved={tp}"
            result.append(_text(label))
            result.append(_preview(tile))
    return result


# ---------------------------------------------------------------------------
# tools: finding images and handing results back
# ---------------------------------------------------------------------------


def _recent_files(days: float, limit: int) -> list[Path]:
    home = Path.home()
    roots: list[tuple[Path, int]] = [(d, 1) for d in _screenshot_dirs()]
    roots += [(home / "Desktop", 1), (home / "Downloads", 1), (home / "Pictures", 2), (home / "Documents", 1)]
    cutoff = time.time() - days * 86400
    seen: set[Path] = set()
    found: list[tuple[float, Path]] = []
    budget = 20_000  # entries to scan before giving up, so huge folders can't stall the call

    def walk(d: Path, depth: int) -> None:
        nonlocal budget
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            budget -= 1
            if budget <= 0:
                return
            if e.name.startswith(".") or e.name.endswith((".photoslibrary", ".app")):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth > 1:
                        walk(Path(e.path), depth - 1)
                elif Path(e.name).suffix.lower() in IMAGE_EXTS:
                    mtime = e.stat().st_mtime
                    rp = Path(e.path).resolve()
                    if mtime >= cutoff and rp not in seen and not _is_virtual(rp):
                        seen.add(rp)
                        found.append((mtime, rp))
            except OSError:
                continue

    for root, depth in roots:
        if root.is_dir():
            walk(root, depth)
    found.sort(reverse=True)
    return [p for _, p in found[:limit]]


def _contact_sheet(items: list[tuple[str, Path]], thumb: int = 300) -> Image.Image:
    cols = min(4, len(items))
    rows = (len(items) + cols - 1) // cols
    pad, label_h = 12, 30
    sheet = Image.new("RGB", (cols * (thumb + pad) + pad, rows * (thumb + label_h + pad) + pad), "white")
    draw = ImageDraw.Draw(sheet)
    font = _font(20)
    for i, (label, path) in enumerate(items):
        x = pad + (i % cols) * (thumb + pad)
        y = pad + (i // cols) * (thumb + label_h + pad)
        try:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((thumb, thumb))
                tile = im.convert("RGBA")
            sheet.paste(tile, (x + (thumb - tile.width) // 2, y + label_h + (thumb - tile.height) // 2), tile)
        except Exception:
            draw.text((x + 8, y + label_h + 8), "(unreadable)", fill="gray", font=font)
        draw.rectangle((x, y, x + thumb, y + label_h - 4), fill="black")
        draw.text((x + 6, y + 3), label, fill="white", font=font)
    return sheet


@mcp.tool()
def find_images(
    where: Literal["all", "chat", "recent"] = "all",
    days: float = 3,
    limit: int = 12,
) -> Content:
    """List images you can work with, as numbered thumbnails plus their paths.

    - chat: images the user attached in this conversation (use as path="chat:N")
    - recent: image files changed in the last `days` days on the Desktop, Downloads, Pictures,
      Documents and screenshot folders
    Use this when the user shared an image you can't reach with path="chat" (for example in
    Claude Desktop chat): compare the thumbnails with the image in the conversation and use
    the matching file's path.
    """
    limit = max(1, min(limit, 24))
    items: list[tuple[str, Path]] = []
    lines: list[str] = []
    if where in ("all", "chat"):
        try:
            chats = _chat_images()
        except Exception:
            chats = []
        for idx in range(1, min(len(chats), limit) + 1):
            p = _from_chat(idx, chats)
            items.append((f"chat:{idx}", p))
            with Image.open(p) as im:
                lines.append(f"chat:{idx}  attached image {len(chats) - idx + 1} of {len(chats)}, {im.size[0]}x{im.size[1]}")
        if not chats and where == "chat":
            lines.append("No chat attachments found (they are only readable from Claude Code sessions).")
    if where in ("all", "recent"):
        for i, p in enumerate(_recent_files(days, limit - len(items) if where == "all" else limit), 1):
            label = f"#{i}"
            items.append((label, p))
            age_h = (time.time() - p.stat().st_mtime) / 3600
            age = f"{age_h * 60:.0f} min ago" if age_h < 1 else f"{age_h:.0f} h ago" if age_h < 48 else f"{age_h / 24:.0f} days ago"
            lines.append(f"{label}  {p}  ({age})")
    if not items:
        return [_text(f"No images found (searched chat attachments and files changed in the last {days} days).")]
    return [_text("\n".join(lines)), _preview(_contact_sheet(items))]


def _copy_png_to_clipboard(png: Path) -> None:
    if sys.platform == "darwin":
        escaped = str(png).replace("\\", "\\\\").replace('"', '\\"')
        script = f'set the clipboard to (read (POSIX file "{escaped}") as «class PNGf»)'
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=15)
    elif sys.platform == "win32":
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
            f"[System.Windows.Forms.Clipboard]::SetImage([System.Drawing.Image]::FromFile('{str(png).replace(chr(39), chr(39) * 2)}'))"
        )
        subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], check=True, capture_output=True, timeout=15)
    elif shutil.which("wl-copy"):
        with open(png, "rb") as fh:
            subprocess.run(["wl-copy", "--type", "image/png"], stdin=fh, check=True, timeout=15)
    elif shutil.which("xclip"):
        subprocess.run(["xclip", "-selection", "clipboard", "-t", "image/png", "-i", str(png)], check=True, timeout=15)
    else:
        raise RuntimeError("No clipboard tool found (install wl-clipboard or xclip).")


@mcp.tool()
def copy_to_clipboard(path: PathArg) -> str:
    """Put an image on the system clipboard so the user can paste it anywhere (chat, docs, Slack...).

    Use after editing an image the user shared in chat, so they can grab the result without opening a folder.
    """
    img, p, fmt = _open(path)
    if fmt == "PNG" and not _is_virtual(p):
        png = p
    else:
        buf = io.BytesIO()
        (img if img.mode in ("RGB", "RGBA", "L", "LA") else img.convert("RGBA")).save(buf, format="PNG")
        png = _materialize(buf.getvalue(), "clipboard-out", ".png")
    _copy_png_to_clipboard(png)
    return f"Copied {p.name} ({img.size[0]}x{img.size[1]}) to the clipboard."


if __name__ == "__main__":
    mcp.run()
