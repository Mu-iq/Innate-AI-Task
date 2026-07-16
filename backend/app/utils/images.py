"""Image helpers: loading, saving, sizing, and postcode-free metadata.

Kept deliberately thin. Pillow does the work; this module just gives the
services a typed surface and one place to handle the "Street View returned a
grey 'no imagery' tile with HTTP 200" problem.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from PIL import Image, ImageStat

from app.config import BLANK_IMAGE_STDDEV_THRESHOLD

# UK postcode, loose enough for the real formatting in Places addresses.
_POSTCODE_RE = re.compile(
    r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b",
    re.IGNORECASE,
)


def extract_postcode(address: str) -> str:
    """Pull a UK postcode out of a formatted address, normalised to 'E1 6AN'."""
    m = _POSTCODE_RE.search(address or "")
    if not m:
        return ""
    return f"{m.group(1).upper()} {m.group(2).upper()}"


def image_size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as im:
        return im.size


def save_image(data: bytes, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def load_image(path: Path) -> Image.Image:
    """Load into memory and close the file handle. The google-genai SDK accepts
    PIL Images directly, and leaving handles open across a slow API call is how
    you end up unable to delete the cache on Windows."""
    with Image.open(path) as im:
        return im.convert("RGB").copy()


def to_png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def is_blank_or_placeholder(
    data: bytes,
    stddev_threshold: float = BLANK_IMAGE_STDDEV_THRESHOLD,
) -> bool:
    """True if the image carries essentially no detail.

    The Street View Static API answers 200 OK with a flat grey "Sorry, we have no
    imagery here" tile rather than an error status. That tile would otherwise
    sail into the assess stage and burn a vision call to be told there is no
    entrance. A near-zero standard deviation across the greyscale image catches
    it, along with any other blank frame.
    """
    try:
        with Image.open(io.BytesIO(data)) as im:
            # ImageStat does this in C. Pulling getdata() into a Python list and
            # looping costs ~2M boxed ints on a full-size Places photo, for a
            # number Pillow already computes.
            stddev = ImageStat.Stat(im.convert("L")).stddev
    except Exception:
        # Undecodable bytes are not a usable frontage either.
        return True

    if not stddev:
        return True
    return stddev[0] < stddev_threshold


def crop_to_bbox(
    im: Image.Image,
    bbox: list[float],
    pad_frac: float = 0.04,
) -> Image.Image:
    """Crop to [x1, y1, x2, y2] with a little padding, clamped to the image.

    Used to cut the product out of its lifestyle reference photo.
    """
    w, h = im.size
    x1, y1, x2, y2 = bbox
    pad_x, pad_y = (x2 - x1) * pad_frac, (y2 - y1) * pad_frac

    left = max(0, int(x1 - pad_x))
    top = max(0, int(y1 - pad_y))
    right = min(w, int(x2 + pad_x))
    bottom = min(h, int(y2 + pad_y))

    if right <= left or bottom <= top:
        return im
    return im.crop((left, top, right, bottom))
